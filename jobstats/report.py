"""统计报告：读取本地库，产出单文件 HTML（ECharts 图表）。"""
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from html import escape as _esc
from pathlib import Path

from . import db
from . import crossref
from . import skills as tag_rules
from .config import REPORT_DIR, STALE_AFTER_DAYS

PLATFORM_NAMES = {"boss": "Boss直聘", "job51": "前程无忧", "zhitong": "智通直聘"}


def _embed_json(obj) -> str:
    """内嵌进 <script> 的 JSON。

    采集到的文本不可信，须转义 < > &，否则形如 </script> 的内容会提前闭合脚本标签；
    U+2028/2029 在 JS 字符串字面量里是非法换行，也要转义。
    """
    return (json.dumps(obj, ensure_ascii=False)
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _is_http(url) -> bool:
    """职位链接只接受 http(s)，避免抓取到的 javascript:/data: 值变成可点执行的锚点。"""
    return bool(re.match(r"https?://", str(url or ""), re.I))


def _mid(d):
    if d.get("salary_min") and d.get("salary_max"):
        return (d["salary_min"] + d["salary_max"]) / 2
    return None


def _skill_caption(records: list[dict]) -> str:
    """技能需求那张图的口径说明：词从哪儿来的、有多少条其实没数据。

    不写这句就会读成"这些职位没技能要求"——智通的列表页根本没有技能这一列，
    它的技能词只能从详情页关键词来（抓过正文才有），见 skills.NO_LIST_SKILLS。
    """
    def src(r: dict) -> str:
        return r.get("skill_src") or ("list" if r.get("skills") else "")
    n_list = sum(1 for r in records if src(r) == "list")
    n_jd = sum(1 for r in records if src(r) == "jd")
    n_none = sum(1 for r in records if not r.get("skills"))
    no_list = "、".join(PLATFORM_NAMES.get(p, p) for p in tag_rules.NO_LIST_SKILLS)
    if not no_list:
        return f"{n_list} 条带技能标签，{n_none} 条没带。"
    return (f"{len(records)} 条里 {n_list + n_jd} 条带技能词：{n_list} 条来自列表页标签，"
            f"{n_jd} 条来自详情页关键词；{n_none} 条两处都没取到。"
            f"注意 {no_list} 的列表页没有技能这一列，它那 {sum(1 for r in records if r['platform'] in tag_rules.NO_LIST_SKILLS)} 条"
            f"只有抓过正文才可能有技能词——没词不等于没要求。")


def build_stats(records: list[dict]) -> dict:
    total = len(records)
    by_platform = Counter(r["platform"] for r in records)
    mids = [m for r in records if (m := _mid(r))]
    cities = Counter(r["city"] for r in records if r.get("city") and r["city"] != "全国")
    # 这两张图跟筛选台同口径：先归档再数，否则 47 种经验写法会把图切成 47 个扇区
    experiences = Counter(tag_rules.exp_bucket(r.get("experience")) for r in records)
    educations = Counter(tag_rules.edu_bucket(r.get("education")) for r in records)
    # 清洗是幂等的：库里读出来已经统一过口径，演示数据和外部传入的原文再走一遍
    skills = Counter(s for r in records for s in tag_rules.clean(r.get("skills")))
    welfare = Counter(w for r in records for w in tag_rules.welfare(r.get("welfare")))
    industries = Counter(r["industry"] for r in records if r.get("industry"))

    # 薪资直方图（千元/月）。最后一档不设上限：
    # 有上限的话，一条 12 万的岗位哪个档都不落，柱子加起来比总数小，读起来像"高薪岗更少"。
    bins = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 60),
            (60, 100), (100, None)]
    hist = [0] * len(bins)
    for m in mids:
        for i, (lo, hi) in enumerate(bins):
            if lo <= m and (hi is None or m < hi):
                hist[i] += 1
                break

    # 分平台薪资中位数
    plat_salary = {}
    for plat in by_platform:
        vals = [m for r in records if r["platform"] == plat and (m := _mid(r))]
        if vals:
            plat_salary[PLATFORM_NAMES.get(plat, plat)] = round(statistics.median(vals), 1)

    # 分平台薪资区间（用于箱线感的 min/median/max 展示）
    plat_range = {}
    for plat in by_platform:
        vals = sorted(m for r in records if r["platform"] == plat and (m := _mid(r)))
        if vals:
            plat_range[PLATFORM_NAMES.get(plat, plat)] = [
                round(vals[0], 1), round(statistics.median(vals), 1), round(vals[-1], 1)]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "platform_counts": {PLATFORM_NAMES.get(k, k): v for k, v in by_platform.most_common()},
        "salary_median": round(statistics.median(mids), 1) if mids else None,
        "salary_hist_labels": [f"{lo}-{hi}k" if hi else f"{lo}k以上" for lo, hi in bins],
        "salary_hist_values": hist,
        "top_cities": cities.most_common(12),
        # 档位是固定那几种，全给出去，别按"前 8 名"截掉尾巴
        "experiences": experiences.most_common(len(tag_rules.EXP_BUCKETS)),
        "educations": educations.most_common(len(tag_rules.EDU_BUCKETS)),
        "top_skills": skills.most_common(15),
        "skill_caption": _skill_caption(records),
        "top_welfare": welfare.most_common(10),
        "top_industries": industries.most_common(10),
        "platform_salary": plat_salary,
        "platform_range": plat_range,
        "latest": [
            {k: r.get(k, "") for k in ("platform", "title", "company", "salary_raw",
                                        "city", "url", "fetched_at")}
            for r in records[:50]
        ],
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>职位统计报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  :root { --bg:#f6f7f9; --card:#ffffff; --text:#1f2329; --muted:#646a73; --accent:#3370ff; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
         font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; padding:24px; }
  h1 { font-size:22px; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:var(--card); border-radius:10px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .card .num { font-size:26px; font-weight:700; color:var(--accent); }
  .card .lbl { font-size:12px; color:var(--muted); margin-top:4px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .panel { background:var(--card); border-radius:10px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .panel h3 { font-size:15px; margin-bottom:10px; }
  .cap { color:var(--muted); font-size:12px; margin:-6px 0 10px; line-height:1.6; }
  .chart { width:100%; height:320px; }
  .chart-tall { height:380px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid #eceef1; }
  th { color:var(--muted); font-weight:500; background:#fafbfc; }
  td a { color:var(--accent); text-decoration:none; }
  .full { grid-column:1 / -1; }
  .tag { display:inline-block; background:#eef3ff; color:var(--accent); border-radius:4px;
         padding:2px 8px; font-size:12px; margin:2px; }
  @media (max-width:900px){ .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<h1>职位统计报告</h1>
<div class="sub">生成时间 __GENERATED_AT__ · 数据来源：本地采集（仅公开职位信息）</div>

<div class="cards">
  <div class="card"><div class="num">__TOTAL__</div><div class="lbl">职位总数</div></div>
  <div class="card"><div class="num">__PLAT_N__</div><div class="lbl">覆盖平台数</div></div>
  <div class="card"><div class="num">__MEDIAN__</div><div class="lbl">月薪中位数（K）</div></div>
  <div class="card"><div class="num">__CITY_N__</div><div class="lbl">覆盖城市数</div></div>
</div>

<div class="grid">
  <div class="panel"><h3>薪资分布（千元/月）</h3><div id="hist" class="chart"></div></div>
  <div class="panel"><h3>各平台职位数量</h3><div id="plat" class="chart"></div></div>
  <div class="panel"><h3>各平台薪资中位数对比（K）</h3><div id="psal" class="chart"></div></div>
  <div class="panel"><h3>城市 Top 12</h3><div id="city" class="chart"></div></div>
  <div class="panel"><h3>技能需求 Top 15</h3><div class="cap">__SKILL_CAP__</div>
    <div id="skill" class="chart chart-tall"></div></div>
  <div class="panel"><h3>学历要求分布</h3><div id="edu" class="chart"></div></div>
  <div class="panel"><h3>经验要求分布</h3><div id="exp" class="chart"></div></div>
  <div class="panel"><h3>福利标签 Top 10</h3><div id="wel" class="chart"></div></div>
  <div class="panel full"><h3>最近采集的职位（50 条）</h3><table>
    <tr><th>平台</th><th>职位</th><th>公司</th><th>薪资</th><th>城市</th><th>采集时间</th></tr>
    __TABLE_ROWS__
  </table></div>
</div>

<script>
const C = window.__STATS__;
if (!window.echarts) {
  // 图表库是从 cdn.jsdelivr.net 拉下来的：断网、被拦、或者把这份 HTML 拷给别人
  // 对方没网时，下面整个 script 会直接挂在这里，八张图的位置只剩一片空白，
  // 看的人只会以为是数据出了问题。空着不写不如说清楚。
  document.querySelectorAll('.chart').forEach(el => {
    el.innerHTML = '<div style="padding:24px 12px;color:#646a73;font-size:13px;'
      + 'line-height:1.8">图没画出来：这一页的图表库要从 cdn.jsdelivr.net 加载，'
      + '现在连不上。<br>上面的总数、中位数和下面的职位表格都是本地算的，不受影响。'
      '联网后刷新这一页就有图了。</div>';
  });
} else {
const mk = (id, opt) => echarts.init(document.getElementById(id)).setOption(opt);
const axis = { axisLabel:{color:'#646a73'}, axisLine:{lineStyle:{color:'#d8dae0'}} };

if (C.salary_hist_values) mk('hist', {
  tooltip:{}, grid:{left:50,right:20,top:20,bottom:40},
  xAxis:{type:'category',data:C.salary_hist_labels,...axis},
  yAxis:{type:'value',...axis},
  series:[{type:'bar',data:C.salary_hist_values,itemStyle:{color:'#3370ff',borderRadius:[4,4,0,0]}}]
});
if (C.platform_counts) mk('plat', {
  tooltip:{},
  series:[{type:'pie',radius:['38%','65%'],
    data:Object.entries(C.platform_counts).map(([k,v])=>({name:k,value:v})),
    label:{color:'#1f2329'}, emphasis:{itemStyle:{shadowBlur:10,shadowColor:'rgba(0,0,0,.2)'}}}]
});
if (C.platform_salary) mk('psal', {
  tooltip:{}, grid:{left:50,right:20,top:20,bottom:40},
  xAxis:{type:'category',data:Object.keys(C.platform_salary),...axis},
  yAxis:{type:'value',...axis},
  series:[{type:'bar',data:Object.values(C.platform_salary),barMaxWidth:60,
           itemStyle:{color:'#00b578',borderRadius:[4,4,0,0]}}]
});
if (C.top_cities.length) mk('city', {
  tooltip:{}, grid:{left:70,right:30,top:10,bottom:30},
  yAxis:{type:'category',data:C.top_cities.map(x=>x[0]).reverse(),...axis},
  xAxis:{type:'value',...axis},
  series:[{type:'bar',data:C.top_cities.map(x=>x[1]).reverse(),barMaxWidth:18,
           itemStyle:{color:'#ff8f1f',borderRadius:[0,4,4,0]}}]
});
if (C.top_skills.length) mk('skill', {
  tooltip:{}, grid:{left:90,right:30,top:10,bottom:30},
  yAxis:{type:'category',data:C.top_skills.map(x=>x[0]).reverse(),...axis},
  xAxis:{type:'value',...axis},
  series:[{type:'bar',data:C.top_skills.map(x=>x[1]).reverse(),barMaxWidth:16,
           itemStyle:{color:'#7f61f0',borderRadius:[0,4,4,0]}}]
});
const donut = (id, data) => { if (!data.length) { document.getElementById(id).parentElement.style.display='none'; return; }
  mk(id, { tooltip:{}, series:[{type:'pie',radius:['40%','68%'],
    data:data.map(([k,v])=>({name:k,value:v})), label:{color:'#1f2329'} }] }); };
donut('edu', C.educations); donut('exp', C.experiences); donut('wel', C.top_welfare);
}
</script>
</body>
</html>"""


def render(stats: dict) -> str:
    rows = []
    for r in stats["latest"]:
        plat = PLATFORM_NAMES.get(r["platform"], r["platform"])
        title = _esc(r["title"], quote=False)
        url = r.get("url") or ""
        link = (f'<a href="{_esc(url, quote=True)}" target="_blank" rel="noopener">'
                f"{title}</a>") if _is_http(url) else title
        rows.append(
            f"<tr><td>{_esc(plat, quote=False)}</td><td>{link}</td>"
            f"<td>{_esc(r['company'] or '', quote=False)}</td>"
            f"<td>{_esc(r['salary_raw'] or '', quote=False)}</td>"
            f"<td>{_esc(r['city'] or '', quote=False)}</td><td>{_esc(r['fetched_at'] or '', quote=False)}</td></tr>"
        )
    html = HTML_TEMPLATE
    html = html.replace("__GENERATED_AT__", stats["generated_at"])
    html = html.replace("__TOTAL__", str(stats["total"]))
    html = html.replace("__PLAT_N__", str(len(stats["platform_counts"])))
    html = html.replace("__MEDIAN__", str(stats["salary_median"] or "—"))
    html = html.replace("__CITY_N__", str(len(stats["top_cities"])))
    html = html.replace("__SKILL_CAP__", _esc(stats["skill_caption"], quote=False))
    html = html.replace("__TABLE_ROWS__", "\n".join(rows))
    # 注入数据
    html = html.replace("window.__STATS__", _embed_json(stats))
    return html


def generate(out_path: Path | None = None, demo: bool = False) -> Path:
    if demo:
        records = _demo_records()
    else:
        records = db.fetch_all()
        if not records:
            raise SystemExit("本地库为空：请先运行 collect 采集数据，或用 --demo 看演示报告。")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stats = build_stats(records)
    out = out_path or (REPORT_DIR / "report.html")
    out.write_text(render(stats), encoding="utf-8")
    return out


def _demo_records() -> list[dict]:
    """演示数据：仅用于查看报告样式，不写入数据库。"""
    rnd_titles = [("Python开发工程师", "boss"), ("Python后端", "job51"), ("数据分析专员", "boss"),
                  ("算法工程师", "job51"), ("自动化测试", "zhitong"), ("运维工程师", "boss"),
                  ("Python爬虫工程师", "job51"), ("AI应用开发", "zhitong")]
    rnd_companies = ["深圳星河科技", "广州云途网络", "东莞智造集团", "杭州数澜信息",
                     "长沙星城软件", "武汉光谷数据", "南京紫金科技", "成都天府互联"]
    rnd_cities = ["深圳", "广州", "东莞", "杭州", "长沙", "武汉", "南京", "成都"]
    rnd_skills = [["python", "django", "mysql"], ["python", "fastapi", "docker"],
                  ["python", "sql", "excel"], ["pytorch", "python", "linux"],
                  ["python", "selenium", "pytest"], ["linux", "k8s", "shell"],
                  ["python", "scrapy", "redis"], ["python", "llm", "langchain"]]
    rnd_edu = ["本科", "大专", "本科", "硕士", "本科", "大专", "本科", "本科"]
    rnd_exp = ["3-5年", "1-3年", "3-5年", "5-10年", "1-3年", "3-5年", "1-3年", "3-5年"]
    out = []
    for i in range(60):
        k = i % 8
        lo = 8 + (i * 7) % 25
        out.append({
            "platform": rnd_titles[k][1], "platform_job_id": f"demo-{i}",
            "title": rnd_titles[k][0], "company": rnd_companies[(i * 3) % 8],
            "salary_raw": f"{lo}-{lo + 10}K", "salary_min": float(lo),
            "salary_max": float(lo + 10), "salary_months": 12 + i % 3,
            "city": rnd_cities[(i * 5) % 8], "district": "",
            "experience": rnd_exp[k], "education": rnd_edu[k],
            "industry": "", "company_scale": "",
            "skills": rnd_skills[k], "welfare": ["五险一金", "带薪年假", "节日福利"][:1 + i % 3],
            "url": "", "fetched_at": f"2026-09-16 {10 + i % 12:02d}:{i % 60:02d}:00",
            # 每 5 条给一个"一个多月前最后见过"，让界面预览里两种状态都看得到
            "last_seen_at": (f"2026-09-16 {10 + i % 12:02d}:{i % 60:02d}:00" if i % 5
                             else f"2026-08-05 {10 + i % 12:02d}:{i % 60:02d}:00"),
        })
    return [tag_rules.add_buckets(r) for r in out]


# ---------------------------------------------------------------------------
# 职位筛选台模板。静态版（cli.py browse 产出的单文件）与服务版（cli.py serve）
# 共用同一套 UI；采集面板、轮询脚本只在服务版注入，改筛选界面只需改这里一处。
#
# 槽位：__SUB_LINE__ __GENERATED_AT__ __N__ __DATA__ __TOKEN__ __IS_WEB__ __STATUS_CONSTS__
#       __COLLECT_PANEL__ __COLLECT_SCRIPT__ __DETAIL_SCRIPT__ __DETAIL_TH__
#       __STATUS_SCRIPT__ __BOOT_HOOK__ __CITY_OPTIONS__ __NO_DETAIL__
#       __DELETE_TH__ __DELETE_BAR__ __DELETE_SCRIPT__
# ---------------------------------------------------------------------------
FILTER_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>职位筛选台</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1f2329; --muted:#646a73;
          --accent:#3370ff; --border:#e5e7eb; --tag-bg:#eef3ff; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); padding:20px 24px 60px;
         font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; font-size:14px; }
  h1 { font-size:20px; }
  .sub { color:var(--muted); font-size:12px; margin:4px 0 16px; }
  .card { background:var(--card); border-radius:10px; padding:14px 16px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); margin-bottom:14px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:14px; }
  .num { font-size:22px; font-weight:700; color:var(--accent); }
  .lbl { font-size:12px; color:var(--muted); margin-top:2px; }
  .row { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:10px; }
  .row:last-child { margin-bottom:0; }
  .grp-lbl { color:var(--muted); font-size:12px; min-width:44px; }
  button, select, input[type=text], input[type=number] {
      font-size:13px; border:1px solid var(--border); border-radius:6px;
      background:#fff; color:var(--text); padding:5px 10px; outline:none; }
  input[type=number] { width:70px; }
  input[type=text].kw { width:240px; }
  button:hover { border-color:var(--accent); color:var(--accent); cursor:pointer; }
  button.on { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.chip { border-radius:14px; padding:3px 12px; white-space:nowrap; }
  button.ghost { background:#fff; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  .hint { color:var(--muted); font-size:12px; }
  .stale { color:#d83b01; }
  .tcol { white-space:nowrap; }   /* 日期别在中间断行："2026-10-" / "13" 读起来很费劲 */
  .ftag { display:inline-flex; align-items:center; gap:5px; background:#eef3ff;
          color:var(--accent); border-radius:14px; padding:3px 10px; font-size:12px; }
  .ftag b { cursor:pointer; font-weight:400; opacity:.6; padding:0 2px; }
  .ftag b:hover { opacity:1; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid #eceef1; vertical-align:top; }
  th { color:var(--muted); font-weight:500; background:#fafbfc; position:sticky; top:0;
       white-space:nowrap; }   /* 表头换行会被挤成竖排（"匹/配/度"），列宽让给内容就好 */
  td a { color:var(--accent); text-decoration:none; }
  .plat-badge { display:inline-block; padding:1px 8px; border-radius:4px; font-size:12px; }
  .p-boss { background:#e8f7ee; color:#00b578; }
  .p-job51 { background:#fff4e5; color:#ff8f1f; }
  .p-zhitong { background:#f0ebff; color:#7f61f0; }
  .tag { display:inline-block; background:var(--tag-bg); color:var(--accent);
         border-radius:4px; padding:1px 6px; font-size:11px; margin:1px 2px 1px 0; }
  .pager { display:flex; align-items:center; gap:10px; justify-content:flex-end; margin-top:12px; }
  .pbar { display:flex; height:10px; border-radius:5px; overflow:hidden; max-width:320px; margin-top:6px; }
  .pbar div { height:100%; }
  .legend { font-size:11px; color:var(--muted); display:flex; gap:14px; margin-top:6px; }
  .legend i { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:4px; }
  #c-log { font-family:Consolas,"Courier New",monospace; }
  tr.det-row td { background:#fcfdfe; }
  .det-wrap { background:#fafbfc; border:1px solid var(--border); border-radius:8px;
              padding:10px 12px; max-height:420px; overflow:auto; }
  .det-wrap pre { white-space:pre-wrap; word-break:break-word; font-family:inherit;
                  font-size:13px; line-height:1.75; margin-top:6px; }
  select.st { width:92px; }
  input.st-note { width:150px; margin-top:4px; display:block; }
  .st-saved { border-color:#00b578; }
  /* 跨平台对照的小标签：跟"疑似已下架"一样是提示，不占一整列 */
  .xref { display:inline-block; font-size:11px; color:#7f61f0; background:#f0ebff;
          border:1px solid #ddd2ff; border-radius:10px; padding:1px 8px; margin-top:4px; }
  button.xref { cursor:pointer; }
  button.xref:hover { border-color:#7f61f0; color:#7f61f0; }
  .xref-item { display:flex; flex-wrap:wrap; align-items:center; gap:10px;
               padding:6px 0; border-bottom:1px dashed #eceef1; }
  .xref-item:last-child { border-bottom:none; }
  .xref-det { width:100%; }
  /* 技能列的来源标记：这几个词不是列表页的技能标签，是从详情页关键词兜过来的 */
  .srcjd { display:inline-block; font-size:11px; color:#8a6a00; background:#fff8e1;
           border:1px solid #f3e0a8; border-radius:10px; padding:1px 8px; }
</style>
</head>
<body>
<h1>职位筛选台</h1>
<div class="sub">__SUB_LINE__</div>

<div class="cards">
  <div class="card"><div class="num" id="s-count">0</div><div class="lbl">筛选结果（条）</div></div>
  <div class="card"><div class="num" id="s-median">—</div><div class="lbl">月薪中位数（K）</div></div>
  <div class="card"><div class="num" id="s-range">—</div><div class="lbl">薪资区间（K）</div></div>
  <div class="card"><div class="num" id="s-city">0</div><div class="lbl">覆盖城市</div></div>
</div>
__COLLECT_PANEL__
<div class="card">
  <div class="row">
    <span class="grp-lbl">平台</span>
    <span id="plat-btns"></span>
    <span class="grp-lbl" style="margin-left:16px">关键词</span>
    <input type="text" id="f-kw" class="kw" placeholder="职位名 / 公司 / 技能">
  </div>
  <div class="row">
    <span class="grp-lbl">城市</span>
    <select id="f-city"></select>
    <span class="grp-lbl" style="margin-left:16px">月薪(K)</span>
    <input type="number" id="f-smin" placeholder="最低" min="0"> -
    <input type="number" id="f-smax" placeholder="最高" min="0">
    <span id="salary-presets"></span>
  </div>
  <div class="row">
    <span class="grp-lbl">行业</span>
    <select id="f-industry"></select>
    <span class="hint">按平台给的一级行业筛；前程无忧列表页没这字段，那批在"未标注"里。"生产制造"里也混着视觉/机器人开发岗，批量删之前看一眼确认框</span>
  </div>
  <div class="row">
    <span class="grp-lbl" title="平台原文有十种写法（`中专`/`中专/中技`/`中技/中专` 是同一种），这里并成 6 档。悬停每个档位看它合并了哪些写法；导出 CSV 里仍是原文。">学历</span>
    <span id="edu-chips"></span>
    <span class="hint">10 种写法并成 6 档，可多选</span>
  </div>
  <div class="row">
    <span class="grp-lbl" title="按最低要求归档：`3年及以上`、`经验3-10年`、`3-5年` 都进 3-5年——能不能投只看门槛，上限不参与（`经验5-8年` 和 `经验5年及以上` 算同一类）。">经验</span>
    <span id="exp-chips"></span>
    <span class="hint">47 种写法并成 7 档，按最低要求归档</span>
  </div>
  <div class="row">
    <span class="grp-lbl">状态</span>
    <select id="f-status"></select>
    <span class="hint">状态是你自己标的投递进度，只写本地，不会去平台查任何东西</span>
  </div>
  <div class="row">
    <span class="grp-lbl">还在招吗</span>
    <select id="f-seen">
      <option value="">全部</option>
      <option value="fresh">最近 __STALE_DAYS__ 天见过的</option>
      <option value="stale">疑似已下架（超过 __STALE_DAYS__ 天没再采到）</option>
    </select>
    <span class="hint">平台不会告诉我们职位下架了，这里只能按"最后一次采到它是什么时候"推断，
    仅供参考；长期不投的关键词本来就不会再见到</span>
  </div>
  <div class="row">
    <span class="grp-lbl">排序</span>
    <select id="f-sort">
      <option value="latest">最新采集</option>
      <option value="seen_desc">最近还见到的</option>
      <option value="score_desc">匹配度从高到低</option>
      <option value="salary_desc">薪资从高到低</option>
      <option value="salary_asc">薪资从低到高</option>
    </select>
    <button class="ghost" id="btn-reset">重置筛选</button>
    <button class="ghost" id="btn-export">导出当前结果 CSV</button>
    <span class="hint" id="filter-hint"></span>
  </div>
__DELETE_BAR__
  <div class="row" id="active-tags" style="margin-bottom:0"></div>
</div>

<div class="card">
  <div class="pbar" id="pbar"></div>
  <div class="legend" id="plegend"></div>
  <div class="legend" id="skill-note"></div>
</div>

<div class="card">
  <table>
    <thead><tr><th>平台</th><th>职位</th><th>公司</th><th>薪资</th><th>城市</th>
    <th>经验</th><th>学历</th><th>技能标签</th><th>首次采集</th><th>最近见到</th><th>状态</th><th>匹配度</th>__DETAIL_TH____DELETE_TH__</tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="pager">
    <button class="ghost" id="pg-prev">上一页</button>
    <span class="hint" id="pg-info"></span>
    <button class="ghost" id="pg-next">下一页</button>
  </div>
</div>

<script>
let DATA = __DATA__;
const WEB = __IS_WEB__;
__STATUS_CONSTS__
const PNAME = {boss:'Boss直聘', job51:'前程无忧', zhitong:'智通直聘'};
// 哪些平台的正文拿不到、为什么：服务版给出原因，静态版是空表（那一版没有正文按钮）
const NO_DETAIL = __NO_DETAIL__;
// 哪些平台的列表页压根没有技能列：那几行的技能只能来自详情页的职位关键词（抓过正文才有）
const NO_LIST_SKILLS = __NO_LIST_SKILLS__;
const PAGE_SIZE = 20;
const TOKEN = '__TOKEN__';
const STALE_DAYS = __STALE_DAYS__;
const PRESETS = [['不限',null,null],['≤10K',null,10],['10-20K',10,20],['20-30K',20,30],['≥30K',30,null]];
const st = { plat:new Set(), city:'', industry:'', edu:new Set(), exp:new Set(), kw:'',
             smin:null, smax:null, sort:'latest', status:'', seen:'', page:1, filtered:[] };

const uniq = a => [...new Set(a.filter(Boolean))];
const esc = s => String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const safeUrl = u => { const s = String(u ?? '').toLowerCase();
  return s.startsWith('http://') || s.startsWith('https://') ? u : ''; };
const mid = r => (r.salary_min && r.salary_max) ? (r.salary_min + r.salary_max)/2 : null;
// 行业取一级：智通是"生产制造-模具"两段式，Boss 只有一段，前程无忧列表页压根没这字段
const indKey = r => String(r.industry || '').split('-')[0].trim();
// 对照标签只列平台名（同一平台多条也合成一个）， details 留在展开的面板里
const xrNames = cs => [...new Set(cs.map(c => PNAME[c.platform] || c.platform))].join('、');
// "最近见到"：last_seen_at 每次重新采到都会往后推，fetched_at 是第一次采到的时刻。
// 老库里 last_seen_at 是补列回填的，取不到就退回 fetched_at。
const seenAt = r => r.last_seen_at || r.fetched_at || '';
const daysUnseen = r => {
  const t = Date.parse(String(seenAt(r)).replace(' ', 'T'));
  return isNaN(t) ? null : (Date.now() - t) / 86400000;
};
const isStale = r => { const d = daysUnseen(r); return d !== null && d > STALE_DAYS; };

function toggle(set, v){ set.has(v) ? set.delete(v) : set.add(v); }

// ---- 构建筛选控件 ----
function buildUI(){
  // 平台按钮
  const counts = {};
  DATA.forEach(r => counts[r.platform] = (counts[r.platform]||0)+1);
  document.getElementById('plat-btns').innerHTML = Object.keys(PNAME)
    .filter(p => counts[p])
    .map(p => `<button class="chip" data-plat="${p}">${PNAME[p]} (${counts[p]})</button>`).join('');
  document.querySelectorAll('[data-plat]').forEach(b =>
    b.onclick = () => { toggle(st.plat, b.dataset.plat); b.classList.toggle('on'); st.page=1; apply(); });

  // 城市
  const cities = uniq(DATA.map(r => r.city)).sort((a,b)=>a.localeCompare(b,'zh'));
  document.getElementById('f-city').innerHTML =
    `<option value="">全部城市</option>` + cities.map(c=>`<option>${esc(c)}</option>`).join('');
  document.getElementById('f-city').onchange = e => { st.city=e.target.value; st.page=1; apply(); };

  // 行业（一级），带上条数；没给行业的那批单独归到"未标注"，不然它们谁也筛不到
  const indCnt = {};
  DATA.forEach(r => { const k = indKey(r); indCnt[k] = (indCnt[k]||0)+1; });
  // 条数多的排前面：噪音行业本来就碎，按字面排序等于让人在一堆 (1) 里找
  const inds = Object.keys(indCnt).sort((a,b) =>
    (a?0:1)-(b?0:1) || indCnt[b]-indCnt[a] || a.localeCompare(b,'zh'));
  document.getElementById('f-industry').innerHTML =
    `<option value="">全部行业</option>` + inds.map(k =>
      `<option value="${esc(k || '__none__')}">${esc(k || '未标注')} (${indCnt[k]})</option>`).join('');
  document.getElementById('f-industry').onchange =
    e => { st.industry=e.target.value; st.page=1; apply(); };

  // 投递状态（"未标注"= 还没标过的，用来挑下一步投哪些）
  document.getElementById('f-status').innerHTML =
    `<option value="">全部状态</option><option value="__none__">未标注</option>` +
    STATUS.filter(Boolean).map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  document.getElementById('f-status').onchange = e => { st.status=e.target.value; st.page=1; apply(); };

  // 还在不在招：只有"最后一次采到它"这一个间接证据，界面措辞别写成平台给的状态
  document.getElementById('f-seen').onchange = e => { st.seen=e.target.value; st.page=1; apply(); };

  // 学历/经验 chips：档位由 Python 侧归一（skills.exp_bucket / edu_bucket），这里按固定
  // 档序排、不按条数排——换一批数据顺序不该变。chip 仍然多选（本科+大专一起看很常用）。
  const chip = (bkey, rawKey, key, box, attr, order) => {
    const cnt = {}, raws = {};
    DATA.forEach(r => {
      const b = r[bkey] || '未标注';
      cnt[b] = (cnt[b] || 0) + 1;
      (raws[b] = raws[b] || new Set()).add(r[rawKey] || '（平台没写）');
    });
    const rank = v => { const i = order.indexOf(v); return i < 0 ? 99 : i; };
    document.getElementById(box).innerHTML = Object.keys(cnt)
      .sort((a, b) => rank(a) - rank(b) || cnt[b] - cnt[a])
      .map(v => `<button class="chip" data-${attr}="${esc(v)}" `
        + `title="这一档合并了平台的这些写法：${esc([...raws[v]].join('、'))}（${cnt[v]} 条）">`
        + `${esc(v)}</button>`).join('');
    document.querySelectorAll(`[data-${attr}]`).forEach(b =>
      b.onclick = () => { toggle(st[key], b.dataset[attr]);
        b.classList.toggle('on'); st.page=1; apply(); });
  };
  chip('education_bucket', 'education', 'edu', 'edu-chips', 'edu', EDU_ORDER);
  chip('experience_bucket', 'experience', 'exp', 'exp-chips', 'exp', EXP_ORDER);

  // 薪资预设（选中高亮；手动改输入框时自动取消高亮）
  const syncPresets = () => {
    document.querySelectorAll('#salary-presets button').forEach(b => {
      const [,lo,hi] = PRESETS[+b.dataset.i];
      b.classList.toggle('on', st.smin===lo && st.smax===hi);
    });
  };
  document.getElementById('salary-presets').innerHTML =
    PRESETS.map((p,i)=>`<button class="chip" data-i="${i}">${p[0]}</button>`).join('');
  document.querySelectorAll('#salary-presets button').forEach(b => b.onclick = () => {
    const [,lo,hi] = PRESETS[+b.dataset.i];
    st.smin=lo; st.smax=hi;
    document.getElementById('f-smin').value = lo??'';
    document.getElementById('f-smax').value = hi??'';
    syncPresets(); st.page=1; apply();
  });

  document.getElementById('f-smin').oninput = e => { st.smin=e.target.value===''?null:+e.target.value; syncPresets(); st.page=1; apply(); };
  document.getElementById('f-smax').oninput = e => { st.smax=e.target.value===''?null:+e.target.value; syncPresets(); st.page=1; apply(); };
  document.getElementById('f-kw').oninput = e => { st.kw=e.target.value.trim().toLowerCase(); st.page=1; apply(); };
  document.getElementById('f-sort').onchange = e => { st.sort=e.target.value; st.page=1; apply(); };
  document.getElementById('btn-reset').onclick = () => location.reload();
  document.getElementById('btn-export').onclick = exportCSV;
  document.getElementById('pg-prev').onclick = () => { if(st.page>1){st.page--; renderTable();} };
  document.getElementById('pg-next').onclick = () => {
    const max=Math.max(1,Math.ceil(st.filtered.length/PAGE_SIZE));
    if(st.page<max){st.page++; renderTable();} };

  syncUI();
}

// 把 st 状态回填到控件（刷新数据重建控件后仍保持已选筛选）
function syncUI(){
  document.querySelectorAll('[data-plat]').forEach(b => b.classList.toggle('on', st.plat.has(b.dataset.plat)));
  document.getElementById('f-city').value = st.city;
  document.getElementById('f-industry').value = st.industry;
  document.getElementById('f-status').value = st.status;
  document.getElementById('f-seen').value = st.seen;
  document.querySelectorAll('[data-edu]').forEach(b => b.classList.toggle('on', st.edu.has(b.dataset.edu)));
  document.querySelectorAll('[data-exp]').forEach(b => b.classList.toggle('on', st.exp.has(b.dataset.exp)));
  document.getElementById('f-smin').value = st.smin??'';
  document.getElementById('f-smax').value = st.smax??'';
  document.getElementById('f-kw').value = st.kw;
  document.getElementById('f-sort').value = st.sort;
  document.querySelectorAll('#salary-presets button').forEach(b => {
    const [,lo,hi] = PRESETS[+b.dataset.i];
    b.classList.toggle('on', st.smin===lo && st.smax===hi);
  });
}

// ---- 筛选 + 排序 ----
function apply(){
  let rs = DATA.filter(r => {
    if (st.plat.size && !st.plat.has(r.platform)) return false;
    if (st.city && r.city !== st.city) return false;
    if (st.industry){
      const k = indKey(r);
      if (st.industry === '__none__' ? k : k !== st.industry) return false;
    }
    if (st.edu.size && !st.edu.has(r.education_bucket)) return false;
    if (st.exp.size && !st.exp.has(r.experience_bucket)) return false;
    if (st.status && (r.status || '') !== (st.status === '__none__' ? '' : st.status)) return false;
    if (st.seen && (isStale(r) !== (st.seen === 'stale'))) return false;
    if (st.smin!==null || st.smax!==null){
      const m = mid(r);
      if (m===null) return false;
      if (st.smin!==null && m < st.smin) return false;
      if (st.smax!==null && m > st.smax) return false;
    }
    if (st.kw){
      const hay = (r.title+' '+r.company+' '+(r.skills||[]).join(' ')).toLowerCase();
      if (!hay.includes(st.kw)) return false;
    }
    return true;
  });
  // 没跑过模型的排最后；分数相同的按采集时间，免得同分时顺序每次都变
  if (st.sort==='score_desc') rs.sort((a,b) => (b.score??-1)-(a.score??-1)
    || String(b.fetched_at).localeCompare(String(a.fetched_at)));
  else if (st.sort==='seen_desc') rs.sort((a,b)=>String(seenAt(b)).localeCompare(String(seenAt(a))));
  else if (st.sort==='salary_desc') rs.sort((a,b)=>(mid(b)??-1)-(mid(a)??-1));
  else if (st.sort==='salary_asc') rs.sort((a,b)=>(mid(a)??1e9)-(mid(b)??1e9));
  else rs.sort((a,b)=>String(b.fetched_at).localeCompare(String(a.fetched_at)));
  st.filtered = rs;
  renderStats(); renderTable(); renderPlatformBar(); renderActiveTags();
}

// ---- 当前生效筛选的标签行（点 ✕ 单独移除） ----
function renderActiveTags(){
  const box = document.getElementById('active-tags');
  const tags = [];
  st.plat.forEach(v => tags.push({k:'plat', v, t:PNAME[v]}));
  if (st.city) tags.push({k:'city', v:st.city, t:'城市: '+st.city});
  if (st.industry) tags.push({k:'industry', v:st.industry,
                              t:'行业: ' + (st.industry==='__none__'?'未标注':st.industry)});
  st.edu.forEach(v => tags.push({k:'edu', v, t:'学历: '+v}));
  st.exp.forEach(v => tags.push({k:'exp', v, t:'经验: '+v}));
  if (st.status) tags.push({k:'status', v:'', t:'状态: ' + (st.status==='__none__'?'未标注':st.status)});
  if (st.seen) tags.push({k:'seen', v:'', t: st.seen==='stale' ? '只看疑似已下架' : '只看最近见过的'});
  if (st.smin!==null || st.smax!==null){
    const t = '月薪: ' + (st.smin??'') + ' ~ ' + (st.smax??'') + 'K';
    tags.push({k:'salary', v:'', t});
  }
  if (st.kw) tags.push({k:'kw', v:'', t:'关键词: '+st.kw});
  box.innerHTML = tags.length
    ? `<span class="grp-lbl">生效中</span>` + tags.map((x,i) =>
        `<span class="ftag">${esc(x.t)}<b data-k="${x.k}" data-v="${esc(x.v)}" title="移除">✕</b></span>`).join('')
    : '';
  box.querySelectorAll('b').forEach(b => b.onclick = () => removeFilter(b.dataset.k, b.dataset.v));
}

function removeFilter(k, v){
  if (k==='plat'){ st.plat.delete(v); const el=document.querySelector(`[data-plat="${v}"]`); el&&el.classList.remove('on'); }
  else if (k==='city'){ st.city=''; document.getElementById('f-city').value=''; }
  else if (k==='industry'){ st.industry=''; document.getElementById('f-industry').value=''; }
  else if (k==='status'){ st.status=''; document.getElementById('f-status').value=''; }
  else if (k==='edu'){ st.edu.delete(v); const el=document.querySelector(`[data-edu="${CSS.escape(v)}"]`); el&&el.classList.remove('on'); }
  else if (k==='exp'){ st.exp.delete(v); const el=document.querySelector(`[data-exp="${CSS.escape(v)}"]`); el&&el.classList.remove('on'); }
  else if (k==='salary'){ st.smin=st.smax=null;
    document.getElementById('f-smin').value=''; document.getElementById('f-smax').value='';
    document.querySelectorAll('#salary-presets button').forEach(x=>x.classList.remove('on')); }
  else if (k==='kw'){ st.kw=''; document.getElementById('f-kw').value=''; }
  st.page=1; apply();
}

function median(sorted){
  // 偶数条取中间两个的平均，跟 Python 侧 statistics.median 对齐：
  // 只取上中位的话，同一批职位在报告和筛选台上能差出 1K，而这两处是拿来对照的。
  const n = sorted.length;
  if (!n) return null;
  return n % 2 ? sorted[(n-1)/2] : (sorted[n/2-1] + sorted[n/2]) / 2;
}

function renderStats(){
  const rs = st.filtered, mids = rs.map(mid).filter(m=>m!==null);
  document.getElementById('s-count').textContent = rs.length;
  const med = median(mids.slice().sort((a,b)=>a-b));
  document.getElementById('s-median').textContent = med===null ? '—' : med.toFixed(1);
  document.getElementById('s-range').textContent = mids.length
    ? Math.round(Math.min(...mids)) + ' ~ ' + Math.round(Math.max(...mids)) : '—';
  document.getElementById('s-city').textContent = uniq(rs.map(r=>r.city)).length;
  const active = st.plat.size + (st.city?1:0) + (st.industry?1:0) + st.edu.size + st.exp.size
    + (st.status?1:0)
    + ((st.smin!==null||st.smax!==null)?1:0) + (st.kw?1:0);
  document.getElementById('filter-hint').textContent =
    active ? `已启用 ${active} 个筛选条件` : '未启用筛选，显示全部数据';
  renderSkillNote(rs);
}

// 技能列有两个来源，混在一起数会把"平台没给这一列"看成"这些职位没技能要求"，
// 所以按当前筛选结果把来源和缺口都摆出来（NO_LIST_SKILLS 是 skills.py 里那张能力表）。
function renderSkillNote(rs){
  const el = document.getElementById('skill-note');
  if (!el) return;
  const jd = rs.filter(r => r.skill_src === 'jd').length;
  const none = rs.filter(r => !(r.skills || []).length).length;
  const noList = uniq(rs.filter(r => NO_LIST_SKILLS[r.platform]).map(r => r.platform));
  if (!jd && !none) { el.innerHTML = ''; return; }
  el.innerHTML = `<span>技能列：${rs.length - none} 条有词，其中 ${jd} 条来自详情页关键词`
    + (none ? `，${none} 条列表页和正文里都没取到` : '')
    + (noList.length ? `<span class="hint" title="${esc(noList.map(p =>
          NO_LIST_SKILLS[p]).join('\\n'))}">`
        + `（${esc(noList.map(p => PNAME[p] || p).join('、'))} 的列表页没有技能这一列）</span>` : '')
    + '</span>';
}

const PCOLOR = {boss:'#00b578', job51:'#ff8f1f', zhitong:'#7f61f0'};
function renderPlatformBar(){
  const cnt = {};
  st.filtered.forEach(r => cnt[r.platform]=(cnt[r.platform]||0)+1);
  const total = st.filtered.length || 1;
  document.getElementById('pbar').innerHTML = Object.keys(PNAME)
    .filter(p=>cnt[p])
    .map(p=>`<div style="width:${(cnt[p]/total*100).toFixed(2)}%;background:${PCOLOR[p]}"></div>`).join('');
  document.getElementById('plegend').innerHTML = Object.keys(PNAME)
    .filter(p=>cnt[p])
    .map(p=>`<span><i style="background:${PCOLOR[p]}"></i>${PNAME[p]} ${cnt[p]} 条</span>`).join('');
}

// 表里显示归一后的档位，平台原文进悬停提示——归档是为了能筛，不是改掉平台写过的话
const bucketTd = (b, raw) => `<td title="平台原文：${esc(raw || '（平台没写）')}">`
  + `${esc(b || '未标注')}</td>`;

function renderTable(){
  const rs = st.filtered;
  const max = Math.max(1, Math.ceil(rs.length/PAGE_SIZE));
  st.page = Math.min(st.page, max);
  const start = (st.page-1)*PAGE_SIZE;
  const rows = rs.slice(start, start+PAGE_SIZE).map(r => {
    const m = mid(r);
    const sal = m!==null ? `${esc(r.salary_raw)} <span class="hint">≈${m.toFixed(1)}K/月</span>` : esc(r.salary_raw||'—');
    const u = safeUrl(r.url);
    // 行号（库里的 id）原来只藏在删除/标注按钮的属性里，屏幕上看不见，
    // 而 status 和 README 都让人去敲 `detail --ids 行号`——那条路等于没有。挂在标题上一悬就出。
    const tip = ` title="本机行号 ${esc(r.id)}；只给这条抓正文：python cli.py detail --ids ${esc(r.id)}"`;
    const title = u ? `<a href="${esc(u)}" target="_blank" rel="noopener"${tip}>${esc(r.title)}</a>`
                    : `<span${tip}>${esc(r.title)}</span>`;
    // 跨平台对照：判据是归一后的公司名+职位名，只会漏不会认错，所以措辞是"另见"而不是"重复"
    const xrs = r.cross || [];
    const xtip = '按归一后的公司名和职位名对上的另一条。只能说明别处也发了，'
      + '对不上不代表别处一定没有（代招换了主体名、标题写法不同都会漏）。';
    const xr = xrs.length
      ? (WEB ? `<button class="xref" data-xref="${esc(r.id)}" title="${esc(xtip)}">另见 ${xrNames(xrs)}</button>`
             : `<span class="xref" title="${esc(xtip)}（静态页只能看提示，服务版里点开可对照）">另见 ${xrNames(xrs)}</span>`)
      : '';
    const tags = (r.skills||[]).slice(0,6).map(s=>`<span class="tag">${esc(s)}</span>`).join('');
    // 技能列有两个来源：列表页标签，和列表页不给这一列的平台从详情页关键词兜过来的
    const nl = NO_LIST_SKILLS[r.platform] || '';
    const skillTd = tags
      ? `<td>${tags}`
        + `${r.skill_src === 'jd' ? `<div><span class="srcjd" title="这几个词是从详情页的职位关键词里取的`
            + `——这个平台的列表页没有技能标签那一列（悬停看详细说明）">来自详情页</span></div>` : ''}`
        + `</td>`
      : `<td>${nl ? `<span class="hint" title="${esc(nl)}">没采到技能</span>` : '—'}</td>`;
    const nd = NO_DETAIL[r.platform] || '';
    const det = WEB ? `<td><button class="chip" data-det="1" data-id="${esc(r.id)}"
      data-key="${esc(detKey(r))}"${nd ? ` data-nd="${esc(nd)}" title="${esc(nd)}"` : ''}>`
      + `${nd ? '不抓正文' : (DET.has(detKey(r)) ? '看正文' : '抓正文')}</button></td>` : '';
    const stCell = WEB ? `<td>
      <select class="st" data-st="${esc(r.id)}">${STATUS.map(s =>
        `<option value="${esc(s)}"${(r.status||'')===s?' selected':''}>${esc(s||'未标注')}</option>`
      ).join('')}</select>
      <input type="text" class="st-note" data-note="${esc(r.id)}" placeholder="记一句"
        maxlength="${NOTE_MAX}" value="${esc(r.note||'')}"></td>`
      : `<td>${esc(r.status||'—')}</td>`;
    // 匹配度是本地模型算完回写的，没跑过就是空；理由和缺口放悬停提示，免得把表格挤宽
    const why = [r.reasons, (r.gaps || []).length ? '缺口 ' + r.gaps.join(' / ') : '',
                 r.model ? '· ' + r.model : ''].filter(Boolean).join(' ');
    const scoreCell = (r.score === null || r.score === undefined)
      ? '<td class="hint">—</td>'
      : `<td title="${esc(why)}">${Math.round(r.score)}</td>`;
    const delCell = WEB ? `<td><button class="chip" data-del="${esc(r.id)}"
      title="从本地库删掉这一条（可以撤销）">删</button></td>` : '';
    // "最近见到"只到日：这一列是"最后一次采到它"，超过阈值就是疑似下架
    const seen = String(seenAt(r)).slice(0, 10) || '—';
    const seenTd = isStale(r)
      ? `<td class="hint tcol"><span class="stale" title="已经 ${Math.floor(daysUnseen(r))} 天没再采到，`
        + `平台那边大概已经下架了（这是我推断的，不是平台给的状态）">⚠ ${esc(seen)}</span></td>`
      : `<td class="hint tcol">${esc(seen)}</td>`;
    // 两列时间都只到日，完整时刻放悬停：带时分秒的话 1100px 宽的窗口会横向溢出
    const fetchedTd = `<td class="hint tcol" title="首次采到的完整时刻：${esc(r.fetched_at)}">`
      + `${esc(String(r.fetched_at).slice(0, 10))}</td>`;
    return `<tr>
      <td><span class="plat-badge p-${r.platform}">${PNAME[r.platform]||r.platform}</span></td>
      <td>${title}${xr ? `<div>${xr}</div>` : ''}</td><td>${esc(r.company)}</td><td>${sal}</td>
      <td>${esc([r.city,r.district].filter(Boolean).join('·'))}</td>
      ${bucketTd(r.experience_bucket, r.experience)}${bucketTd(r.education_bucket, r.education)}
      ${skillTd}${fetchedTd}${seenTd}${stCell}${scoreCell}${det}${delCell}</tr>`;
  }).join('');
  document.getElementById('tbody').innerHTML = rows
    || `<tr><td colspan="${WEB?14:12}" class="hint" style="text-align:center;padding:30px">没有符合筛选条件的职位，试试放宽条件</td></tr>`;
  if (WEB) { bindDetails(); bindStatus(); bindDelete(); }
  document.getElementById('pg-info').textContent = rs.length
    ? `第 ${st.page}/${max} 页 · 共 ${rs.length} 条` : '';
  const delBtn = document.getElementById('btn-del-filtered');
  if (delBtn) delBtn.textContent = `删除当前筛选出的 ${rs.length} 条`;
}

function exportCSV(){
  const head = ['平台','职位','公司','薪资原文','月薪下限K','月薪上限K','发薪月数','年薪估算万',
    '城市','经验','学历','技能','福利','状态','备注','链接','首次采集','最近见到'];
  const lines = [head.join(',')];
  st.filtered.forEach(r => {
    const q = v => `"${String(v??'').replace(/"/g,'""')}"`;
    const m = mid(r);
    lines.push([PNAME[r.platform]||r.platform, r.title, r.company, r.salary_raw,
      r.salary_min??'', r.salary_max??'', r.salary_months??'',
      m!==null ? (m*(r.salary_months||12)/10).toFixed(1) : '',
      [r.city,r.district].filter(Boolean).join('·'),
      r.experience, r.education, (r.skills||[]).join(' / '), (r.welfare||[]).join(' / '),
      r.status||'', r.note||'', r.url, r.fetched_at, seenAt(r)].map(q).join(','));
  });
  const blob = new Blob(['\\ufeff'+lines.join('\\n')], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'filtered-jobs.csv';
  a.click();
}

__COLLECT_SCRIPT____DETAIL_SCRIPT____STATUS_SCRIPT____DELETE_SCRIPT__buildUI();
apply();__BOOT_HOOK__
</script>
</body>
</html>"""

STATIC_SUB_LINE = "__GENERATED_AT__ · 共载入 __N__ 条职位（本地数据，仅公开职位信息） · 筛选实时生效"
WEB_SUB_LINE = ("__GENERATED_AT__ · 已连接本地服务，共载入 __N__ 条职位（仅公开职位信息）"
                " · 采集与筛选都在本页完成")
WEB_COLLECT_PANEL = """
<div class="card">
  <div class="row" style="margin-bottom:0">
    <span class="grp-lbl">采集</span>
    <select id="c-platform">
      <option value="job51">前程无忧</option>
      <option value="boss">Boss直聘</option>
      <option value="zhitong">智通直聘</option>
    </select>
    <select id="c-city">__CITY_OPTIONS__</select>
    <input type="text" id="c-kw" class="kw" placeholder="关键词，用长词（python开发）">
    <span class="hint">页数</span>
    <input type="number" id="c-pages" min="1" max="10" value="3">
    <button id="btn-collect">开始采集</button>
    <button class="ghost" id="btn-refresh">刷新数据</button>
    <span class="hint" id="c-hint"></span>
  </div>
  <div id="c-log" class="hint" style="display:none;margin-top:10px;font-size:12px;line-height:1.8;max-height:190px;overflow:auto;background:#fafbfc;border:1px solid var(--border);border-radius:6px;padding:8px 10px"></div>
</div>
"""
WEB_COLLECT_SCRIPT = """// ---- 采集新数据（走本地服务；完成后自动刷新表格） ----
let collectTaskId = null, collectTimer = null, __logCount = 0;
const cBox = () => document.getElementById('c-log');

function cLog(line){
  const box = cBox();
  box.style.display = 'block';
  const d = document.createElement('div');
  d.textContent = line;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function setCollecting(on){
  const btn = document.getElementById('btn-collect');
  btn.disabled = on;
  btn.textContent = on ? '采集中…' : '开始采集';
}

async function api(path, opts={}){
  opts.headers = Object.assign({'X-Token': TOKEN}, opts.headers||{});
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch(e){}
  if (res.status === 403) throw new Error('本地服务安全校验失败，请重启筛选台');
  if (res.status === 409 || res.status === 400) throw new Error(data.error || '请求被拒绝');
  if (!res.ok) throw new Error('请求失败 ' + res.status);
  return data;
}

document.getElementById('btn-collect').onclick = async () => {
  if (collectTimer) return;
  const platform = document.getElementById('c-platform').value;
  const city = document.getElementById('c-city').value;
  const query = document.getElementById('c-kw').value.trim();
  const pages = Math.max(1, Math.min(10, +document.getElementById('c-pages').value || 3));
  const pname = PNAME[platform] || platform;
  cLog('[' + new Date().toLocaleTimeString() + '] 发起采集：' + pname + ' / ' + query + ' / ' + (city||'全国') + ' / ' + pages + ' 页');
  if (!query) { cLog('[提示] 请先输入关键词，如 python'); return; }
  setCollecting(true);
  document.getElementById('c-hint').textContent = '';
  cBox().innerHTML = ''; __logCount = 0;
  try {
    const r = await api('/api/collect', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({platform, query, city, pages})});
    collectTaskId = r.task.id;
    collectTimer = setInterval(pollCollect, 900);
  } catch (e) {
    cLog('[失败] ' + e.message);
    setCollecting(false);
  }
};

async function pollCollect(){
  try {
    const r = await api('/api/collect/' + collectTaskId);
    const t = r.task || {};
    const logs = t.logs || [];
    const box = cBox();
    while (__logCount < logs.length) {
      const [ts, line] = logs[__logCount++];
      const d = document.createElement('div');
      d.textContent = '[' + ts + '] ' + line;
      box.appendChild(d);
      box.scrollTop = box.scrollHeight;
    }
    if (t.status === 'running') {
      document.getElementById('c-hint').textContent = '采集中：已去重 ' + t.collected + ' 条 / 本批新增 ' + t.added + ' 条';
      return;
    }
    clearInterval(collectTimer); collectTimer = null;
    setCollecting(false);
    if (t.status === 'done') {
      document.getElementById('c-hint').textContent = '完成：去重 ' + t.collected + ' 条，本次新增入库 ' + t.added + ' 条';
      await refreshData();
    } else {
      document.getElementById('c-hint').textContent = '采集失败';
    }
  } catch (e) {
    clearInterval(collectTimer); collectTimer = null;
    setCollecting(false);
    cLog('[失败] ' + e.message);
  }
}

async function refreshData(){
  try {
    const r = await api('/api/jobs');
    DATA = r.jobs || [];
    buildUI(); apply();
    document.getElementById('c-hint').textContent = '已刷新：当前共 ' + DATA.length + ' 条数据';
  } catch (e) {
    cLog('[失败] ' + e.message);
  }
}

document.getElementById('btn-refresh').onclick = () => refreshData();

async function resumeTask(){
  try {
    const r = await api('/api/collect/current');
    if (r.task && r.task.status === 'running') {
      collectTaskId = r.task.id;
      collectTimer = setInterval(pollCollect, 900);
      setCollecting(true);
      cLog('检测到本机正在进行的采集任务，已恢复进度显示。');
    }
  } catch (e) {}
}

"""

DETAIL_TH = "<th>正文</th>"
DELETE_TH = "<th>删</th>"

# 批量删除放在筛选条下面：要删的永远是"刚筛出来的这一批"，条件也只用说一次
DELETE_BAR = """  <div class="row">
    <button class="ghost" id="btn-del-filtered">删除当前筛选出的职位</button>
    <span class="hint">删的只是本机库里的记录，不会访问任何平台，也不影响你已经投出去的申请</span>
  </div>
  <div class="row" id="undo-bar" style="display:none">
    <span class="hint" id="undo-text"></span>
    <button class="ghost" id="btn-undo">撤销这次删除</button>
  </div>"""

STATUS_JS = """// ---- 投递状态：只是把"我投没投、面得怎么样"记在本地库，不发任何平台请求 ----
function bindStatus(){
  document.querySelectorAll('[data-st]').forEach(sel =>
    sel.onchange = () => saveStatus(sel.dataset.st, 'status', sel.value));
  // 备注用 change（失焦或回车才存），避免每敲一个字就写一次库
  document.querySelectorAll('[data-note]').forEach(inp =>
    inp.onchange = () => saveStatus(inp.dataset.note, 'note', inp.value));
}

async function saveStatus(id, key, value){
  const el = document.querySelector(`[data-${key === 'status' ? 'st' : 'note'}="${id}"]`);
  el.classList.add('st-saved');
  try {
    const r = await api('/api/status', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: +id, [key]: value})});
    const row = DATA.find(x => String(x.id) === String(id));
    if (row) row[key] = r[key] === undefined ? '' : r[key];   // 用服务端裁过的长度回填
    el.title = '';
  } catch (e) {
    el.title = '没存上：' + e.message;
  } finally {
    setTimeout(() => el.classList.remove('st-saved'), 1200);
  }
}

"""

DETAIL_JS = """// ---- 职位正文（JD）：逐条打开详情页，走本地服务，受每日正文额度约束 ----
const DET = new Set();        // 已有正文的职位键：platform:job_id
const DET_CACHE = {};         // 键 -> 正文对象，展开过就不重复请求
let detBusy = false;          // 一次只放一条详情请求出去

const detKey = r => r.platform + ':' + r.platform_job_id;

async function loadDetailKeys(){
  try {
    const r = await api('/api/details');
    (r.keys || []).forEach(k => DET.add(k));
    renderTable();
  } catch (e) {}
}

function bindDetails(){
  document.querySelectorAll('[data-det]').forEach(b => {
    b.onclick = () => toggleDetail(b);
  });
  document.querySelectorAll('[data-xref]').forEach(b => { b.onclick = () => toggleXref(b); });
  bindXdet(document);
}

// 对照面板里的那颗正文按钮：只在这一项下面就地展开，不再插整行（列数不变）。
// 面板是点标签之后才建出来的，所以绑定时机得跟着走，不能只绑一次 renderTable。
function bindXdet(scope){
  scope.querySelectorAll('[data-xdet]').forEach(b => {
    b.onclick = () => {
      const box = b.closest('.xref-item').querySelector('.xref-det');
      if (box.childElementCount) { box.innerHTML = ''; b.textContent = '看正文'; return; }
      showDetail(b, box);
    };
  });
}

function renderDetail(cell, d, note){
  cell.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'det-wrap';
  const head = document.createElement('div');
  head.className = 'hint';
  head.textContent = (d.fetched_at ? '正文抓取于 ' + d.fetched_at : '')
    + (note ? ' · ' + note : '');
  box.appendChild(head);
  if ((d.keyword_tags || []).length) {
    const line = document.createElement('div');
    d.keyword_tags.forEach(t => {
      const s = document.createElement('span');
      s.className = 'tag'; s.textContent = t; line.appendChild(s);
    });
    box.appendChild(line);
  }
  const pre = document.createElement('pre');
  pre.textContent = d.description || '（这条没解析出正文）';
  box.appendChild(pre);
  cell.appendChild(box);
}

async function toggleDetail(btn){
  const tr = btn.closest('tr');
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('det-row')) { next.remove(); return; }
  document.querySelectorAll('tr.det-row').forEach(x => x.remove());
  const row = document.createElement('tr');
  row.className = 'det-row';
  const cell = document.createElement('td');
  cell.colSpan = 14;
  row.appendChild(cell);
  tr.after(row);
  // 这条通道拿不到正文：把原因就地说明白，不发请求、不占今日正文额度
  if (btn.dataset.nd) { cell.textContent = btn.dataset.nd; return; }
  await showDetail(btn, cell);
}

// 对照面板：这一行的数据早就在 DATA 里，纯本地拼出来，一个请求都不发
function toggleXref(btn){
  const tr = btn.closest('tr');
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('det-row')) { next.remove(); return; }
  document.querySelectorAll('tr.det-row').forEach(x => x.remove());
  const row = document.createElement('tr');
  row.className = 'det-row';
  const cell = document.createElement('td');
  cell.colSpan = 14;
  cell.appendChild(renderXref(+btn.dataset.xref));
  row.appendChild(cell);
  tr.after(row);
}

function renderXref(id){
  const r = DATA.find(x => x.id === id);
  const box = document.createElement('div');
  box.className = 'det-wrap';
  box.innerHTML = `<div class="hint">按归一后的公司名和职位名对上的另一些职位。`
    + `对不上不代表别处没有；投哪条看的是这两条各自写的薪资和要求，不是"哪个平台更正规"。</div>`;
  (r.cross || []).forEach(x => {
    const item = document.createElement('div');
    item.className = 'xref-item';
    const u = safeUrl(x.url);
    const key = x.platform + ':' + x.platform_job_id;
    const nd = NO_DETAIL[x.platform] || '';
    item.innerHTML = `<span class="plat-badge p-${esc(x.platform)}">${esc(PNAME[x.platform]||x.platform)}</span>`
      + `<span>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener">在原页看</a>` : '（没留链接）'}</span>`
      + `<span>薪资 ${esc(x.salary_raw||'—')}</span><span>${esc(x.city||'没写城市')}</span>`
      + `<span class="hint">最近见到 ${esc(x.seen||'—')}</span>`
      + (nd ? `<span class="hint">${esc(nd)}</span>`
            : `<button class="chip" data-xdet="1" data-id="${esc(x.id)}" data-key="${esc(key)}">`
              + `${(x.has_detail || DET.has(key)) ? '看正文' : '抓正文'}</button>`)
      + `<div class="xref-det"></div>`;
    box.appendChild(item);
  });
  bindXdet(box);
  return box;
}

async function showDetail(btn, cell){
  const key = btn.dataset.key;
  if (DET_CACHE[key]) { renderDetail(cell, DET_CACHE[key]); return; }
  if (detBusy) { cell.textContent = '上一条正文还在处理，稍等几秒再点。'; return; }
  detBusy = true;
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '正文抓取中…';
  cell.textContent = DET.has(key) ? '正在读取本地正文…' : '正在打开详情页抓正文，约几秒…';
  try {
    const r = await api('/api/detail', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: +btn.dataset.id})});
    if (!r.ok || !r.detail) throw new Error(r.error || '这条没拿到正文');
    DET_CACHE[key] = r.detail; DET.add(key);
    renderDetail(cell, r.detail, r.note || '');
    btn.textContent = '收起正文';
  } catch (e) {
    cell.textContent = e.message;
    btn.textContent = label;
  } finally {
    detBusy = false; btn.disabled = false;
  }
}

"""

DELETE_JS = """// ---- 删除：只动本机库里的记录，不发任何平台请求，也不撤销投递 ----
let undoBatch = null;

function sayUndo(text, canUndo){
  const bar = document.getElementById('undo-bar');
  document.getElementById('undo-text').textContent = text;
  bar.style.display = 'flex';
  document.getElementById('btn-undo').style.display = canUndo ? '' : 'none';
}

function describe(rs){
  const names = rs.slice(0, 10).map(r =>
    '· ' + (r.title || '（没标题）') + '｜' + (r.company || '没写公司') + '｜' + (r.city || '没写城市'));
  return names.join('\\n') + (rs.length > 10 ? '\\n… 一共 ' + rs.length + ' 条' : '');
}

function bindDelete(){
  document.querySelectorAll('[data-del]').forEach(b =>
    b.onclick = () => askDelete([b.dataset.del]));
  document.getElementById('btn-del-filtered').onclick =
    () => askDelete(st.filtered.map(r => String(r.id)));
  document.getElementById('btn-undo').onclick = undoDelete;
}

async function askDelete(ids){
  if (!ids.length) { sayUndo('当前筛选结果里没有职位。', false); return; }
  if (ids.length > 500) { sayUndo('一次最多删 500 条，请收紧筛选条件分批来。', false); return; }
  const rs = ids.map(i => DATA.find(r => String(r.id) === String(i))).filter(Boolean);
  const head = ids.length === 1 ? '从本地库删掉这一条？'
                                 : '删掉当前筛选出的 ' + ids.length + ' 条？';
  // 确认框里必须点名是哪几条：批量删最容易出的错是"我以为我筛对了"
  if (!confirm(head + '\\n\\n' + describe(rs) + '\\n\\n'
        + '这只删本机 data/jobs.db 里的记录，不会访问任何招聘平台，'
        + '也不影响你已经投出去的申请。\\n正文和匹配度会一起清掉，删完可以在本页撤销一次。')) return;
  try {
    const r = await api('/api/delete', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids: ids.map(Number)})});
    const gone = new Set(ids.map(String));
    DATA = DATA.filter(x => !gone.has(String(x.id)));
    undoBatch = r.batch || null;
    apply();
    sayUndo('已删除 ' + r.deleted + ' 条'
      + (r.details ? '（连带 ' + r.details + ' 条正文）' : '')
      + (r.analysis ? '、' + r.analysis + ' 条匹配度' : '') + '。', !!undoBatch);
  } catch (e) {
    sayUndo('删除失败：' + e.message, false);
  }
}

async function undoDelete(){
  if (!undoBatch) return;
  try {
    const r = await api('/api/restore', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({batch: undoBatch})});
    undoBatch = null;
    await refreshData();
    sayUndo('已撤销，放回 ' + r.jobs + ' 条职位'
      + (r.details ? '、' + r.details + ' 条正文' : '')
      + (r.analysis ? '、' + r.analysis + ' 条匹配度' : '') + '。', false);
  } catch (e) {
    sayUndo('撤销失败：' + e.message, true);
  }
}

// 页面重开了也还能撤销：批次记在服务端的 data/trash 里，不靠浏览器存数据
async function loadTrash(){
  try {
    const r = await api('/api/trash');
    const b = (r.batches || [])[0];
    if (!b) return;
    undoBatch = b.batch;
    sayUndo('上次删掉了 ' + b.jobs + ' 条（' + b.deleted_at + '），这一批还能撤销。', true);
  } catch (e) {}
}

"""

SLOT_RE = re.compile("|".join(map(re.escape, (
    "__COLLECT_PANEL__", "__COLLECT_SCRIPT__", "__DETAIL_SCRIPT__", "__STATUS_SCRIPT__",
    "__DELETE_TH__", "__DELETE_BAR__", "__DELETE_SCRIPT__",
    "__STATUS_CONSTS__", "__CITY_OPTIONS__", "__STALE_DAYS__", "__NO_DETAIL__",
    "__NO_LIST_SKILLS__",
    "__GENERATED_AT__", "__SUB_LINE__", "__BOOT_HOOK__", "__TOKEN__", "__DATA__",
    "__N__", "__IS_WEB__", "__DETAIL_TH__"))))


def _filter_html(sub_line: str, records: list[dict], *, web: bool,
                 token: str = "", city_opts_html: str = "",
                 no_detail: dict | None = None) -> str:
    n = str(len(records))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 跨平台对照是读取侧算出来的：库里没这一列，导出 CSV 也不带，只有页面用
    crossref.attach(records)
    slots = {
        "__SUB_LINE__": sub_line.replace("__GENERATED_AT__", ts).replace("__N__", n),
        "__GENERATED_AT__": ts,
        "__N__": n,
        "__DATA__": _embed_json(records),
        "__TOKEN__": token,
        "__CITY_OPTIONS__": city_opts_html,
        "__COLLECT_PANEL__": WEB_COLLECT_PANEL.replace("__CITY_OPTIONS__", city_opts_html) if web else "",
        "__COLLECT_SCRIPT__": WEB_COLLECT_SCRIPT if web else "",
        "__DETAIL_SCRIPT__": DETAIL_JS if web else "",
        "__DETAIL_TH__": DETAIL_TH if web else "",
        "__DELETE_SCRIPT__": DELETE_JS if web else "",
        "__DELETE_TH__": DELETE_TH if web else "",
        "__DELETE_BAR__": DELETE_BAR if web else "",
        "__STATUS_SCRIPT__": STATUS_JS if web else "",
        "__STATUS_CONSTS__": ("const STATUS = {}; const NOTE_MAX = {}; const EXP_ORDER = {}; "
                              "const EDU_ORDER = {};").format(
            _embed_json(list(db.STATUS_LABELS)), db.NOTE_MAX,
            _embed_json(list(tag_rules.EXP_BUCKETS)), _embed_json(list(tag_rules.EDU_BUCKETS))),
        "__STALE_DAYS__": str(STALE_AFTER_DAYS),
        # 正文能力是逐平台的：服务版给出"为什么拿不到"，静态版没有正文按钮，给空表就行
        "__NO_DETAIL__": _embed_json(no_detail or {}),
        # 技能列的来源同样逐平台：这一份两版都要用，静态版也得说清谁的列表页没有技能
        "__NO_LIST_SKILLS__": _embed_json(tag_rules.NO_LIST_SKILLS),
        "__IS_WEB__": "true" if web else "false",
        "__BOOT_HOOK__": "\nresumeTask(); loadDetailKeys(); loadTrash();" if web else "",
    }
    # 单次扫描替换：值里若含占位符字面量也不会被二次展开
    return re.sub(SLOT_RE, lambda m: slots[m.group(0)], FILTER_TEMPLATE)


def render_web(records: list[dict], token: str, city_opts_html: str = "",
               no_detail: dict | None = None) -> str:
    return _filter_html(WEB_SUB_LINE, records, web=True, token=token,
                        city_opts_html=city_opts_html, no_detail=no_detail)


def generate_browser(out_path: Path | None = None, demo: bool = False) -> Path:
    if demo:
        records = _demo_records()
    else:
        records = db.fetch_all()
        if not records:
            raise SystemExit("本地库为空：请先运行 collect 采集数据，或用 --demo 看效果。")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    html = _filter_html(STATIC_SUB_LINE, records, web=False)
    out = out_path or (REPORT_DIR / "jobs-browser.html")
    out.write_text(html, encoding="utf-8")
    return out
