"""统计报告：读取本地库，产出单文件 HTML（ECharts 图表）。"""
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from html import escape as _esc
from pathlib import Path

from . import db
from . import skills as tag_rules
from .config import REPORT_DIR

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


def build_stats(records: list[dict]) -> dict:
    total = len(records)
    by_platform = Counter(r["platform"] for r in records)
    mids = [m for r in records if (m := _mid(r))]
    cities = Counter(r["city"] for r in records if r.get("city") and r["city"] != "全国")
    experiences = Counter(r["experience"] for r in records if r.get("experience"))
    educations = Counter(r["education"] for r in records if r.get("education"))
    # 清洗是幂等的：库里读出来已经统一过口径，演示数据和外部传入的原文再走一遍
    skills = Counter(s for r in records for s in tag_rules.clean(r.get("skills")))
    welfare = Counter(w for r in records for w in tag_rules.welfare(r.get("welfare")))
    industries = Counter(r["industry"] for r in records if r.get("industry"))

    # 薪资直方图（千元/月）
    bins = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 60), (60, 100)]
    hist = [0] * len(bins)
    for m in mids:
        for i, (lo, hi) in enumerate(bins):
            if lo <= m < hi:
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
        "salary_hist_labels": [f"{lo}-{hi}k" for lo, hi in bins],
        "salary_hist_values": hist,
        "top_cities": cities.most_common(12),
        "experiences": experiences.most_common(8),
        "educations": educations.most_common(8),
        "top_skills": skills.most_common(15),
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
  <div class="panel"><h3>技能需求 Top 15</h3><div id="skill" class="chart chart-tall"></div></div>
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
        })
    return out


# ---------------------------------------------------------------------------
# 职位筛选台模板。静态版（cli.py browse 产出的单文件）与服务版（cli.py serve）
# 共用同一套 UI；采集面板、轮询脚本只在服务版注入，改筛选界面只需改这里一处。
#
# 槽位：__SUB_LINE__ __GENERATED_AT__ __N__ __DATA__ __TOKEN__ __IS_WEB__ __STATUS_CONSTS__
#       __COLLECT_PANEL__ __COLLECT_SCRIPT__ __DETAIL_SCRIPT__ __DETAIL_TH__
#       __STATUS_SCRIPT__ __BOOT_HOOK__ __CITY_OPTIONS__
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
  button.chip { border-radius:14px; padding:3px 12px; }
  button.ghost { background:#fff; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  .hint { color:var(--muted); font-size:12px; }
  .ftag { display:inline-flex; align-items:center; gap:5px; background:#eef3ff;
          color:var(--accent); border-radius:14px; padding:3px 10px; font-size:12px; }
  .ftag b { cursor:pointer; font-weight:400; opacity:.6; padding:0 2px; }
  .ftag b:hover { opacity:1; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid #eceef1; vertical-align:top; }
  th { color:var(--muted); font-weight:500; background:#fafbfc; position:sticky; top:0; }
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
    <span class="grp-lbl">学历</span>
    <span id="edu-chips"></span>
  </div>
  <div class="row">
    <span class="grp-lbl">经验</span>
    <span id="exp-chips"></span>
  </div>
  <div class="row">
    <span class="grp-lbl">状态</span>
    <select id="f-status"></select>
    <span class="hint">状态是你自己标的投递进度，只写本地，不会去平台查任何东西</span>
  </div>
  <div class="row">
    <span class="grp-lbl">排序</span>
    <select id="f-sort">
      <option value="latest">最新采集</option>
      <option value="salary_desc">薪资从高到低</option>
      <option value="salary_asc">薪资从低到高</option>
    </select>
    <button class="ghost" id="btn-reset">重置筛选</button>
    <button class="ghost" id="btn-export">导出当前结果 CSV</button>
    <span class="hint" id="filter-hint"></span>
  </div>
  <div class="row" id="active-tags" style="margin-bottom:0"></div>
</div>

<div class="card">
  <div class="pbar" id="pbar"></div>
  <div class="legend" id="plegend"></div>
</div>

<div class="card">
  <table>
    <thead><tr><th>平台</th><th>职位</th><th>公司</th><th>薪资</th><th>城市</th>
    <th>经验</th><th>学历</th><th>技能标签</th><th>采集时间</th><th>状态</th>__DETAIL_TH__</tr></thead>
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
const PAGE_SIZE = 20;
const TOKEN = '__TOKEN__';
const PRESETS = [['不限',null,null],['≤10K',null,10],['10-20K',10,20],['20-30K',20,30],['≥30K',30,null]];
const st = { plat:new Set(), city:'', edu:new Set(), exp:new Set(), kw:'',
             smin:null, smax:null, sort:'latest', status:'', page:1, filtered:[] };

const uniq = a => [...new Set(a.filter(Boolean))];
const esc = s => String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const safeUrl = u => { const s = String(u ?? '').toLowerCase();
  return s.startsWith('http://') || s.startsWith('https://') ? u : ''; };
const mid = r => (r.salary_min && r.salary_max) ? (r.salary_min + r.salary_max)/2 : null;

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

  // 投递状态（"未标注"= 还没标过的，用来挑下一步投哪些）
  document.getElementById('f-status').innerHTML =
    `<option value="">全部状态</option><option value="__none__">未标注</option>` +
    STATUS.filter(Boolean).map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  document.getElementById('f-status').onchange = e => { st.status=e.target.value; st.page=1; apply(); };

  // 学历/经验 chips（按数量排序，可多选）
  const chip = (arr, key, box, attr) => {
    const cnt = {}; arr.forEach(v => cnt[v]=(cnt[v]||0)+1);
    document.getElementById(box).innerHTML = Object.entries(cnt)
      .sort((a,b)=>b[1]-a[1]).map(([v]) => `<button class="chip" data-${attr}="${esc(v)}">${esc(v)}</button>`).join('');
    document.querySelectorAll(`[data-${attr}]`).forEach(b =>
      b.onclick = () => { toggle(st[key], b.dataset[attr]);
        b.classList.toggle('on'); st.page=1; apply(); });
  };
  chip(uniq(DATA.map(r=>r.education)), 'edu', 'edu-chips', 'edu');
  chip(uniq(DATA.map(r=>r.experience)), 'exp', 'exp-chips', 'exp');

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
  document.getElementById('f-status').value = st.status;
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
    if (st.edu.size && !st.edu.has(r.education)) return false;
    if (st.exp.size && !st.exp.has(r.experience)) return false;
    if (st.status && (r.status || '') !== (st.status === '__none__' ? '' : st.status)) return false;
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
  if (st.sort==='salary_desc') rs.sort((a,b)=>(mid(b)??-1)-(mid(a)??-1));
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
  st.edu.forEach(v => tags.push({k:'edu', v, t:'学历: '+v}));
  st.exp.forEach(v => tags.push({k:'exp', v, t:'经验: '+v}));
  if (st.status) tags.push({k:'status', v:'', t:'状态: ' + (st.status==='__none__'?'未标注':st.status)});
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
  else if (k==='status'){ st.status=''; document.getElementById('f-status').value=''; }
  else if (k==='edu'){ st.edu.delete(v); const el=document.querySelector(`[data-edu="${CSS.escape(v)}"]`); el&&el.classList.remove('on'); }
  else if (k==='exp'){ st.exp.delete(v); const el=document.querySelector(`[data-exp="${CSS.escape(v)}"]`); el&&el.classList.remove('on'); }
  else if (k==='salary'){ st.smin=st.smax=null;
    document.getElementById('f-smin').value=''; document.getElementById('f-smax').value='';
    document.querySelectorAll('#salary-presets button').forEach(x=>x.classList.remove('on')); }
  else if (k==='kw'){ st.kw=''; document.getElementById('f-kw').value=''; }
  st.page=1; apply();
}

function renderStats(){
  const rs = st.filtered, mids = rs.map(mid).filter(m=>m!==null);
  document.getElementById('s-count').textContent = rs.length;
  document.getElementById('s-median').textContent = mids.length
    ? (mids.slice().sort((a,b)=>a-b)[Math.floor(mids.length/2)]).toFixed(1) : '—';
  document.getElementById('s-range').textContent = mids.length
    ? Math.round(Math.min(...mids)) + ' ~ ' + Math.round(Math.max(...mids)) : '—';
  document.getElementById('s-city').textContent = uniq(rs.map(r=>r.city)).length;
  const active = st.plat.size + (st.city?1:0) + st.edu.size + st.exp.size + (st.status?1:0)
    + ((st.smin!==null||st.smax!==null)?1:0) + (st.kw?1:0);
  document.getElementById('filter-hint').textContent =
    active ? `已启用 ${active} 个筛选条件` : '未启用筛选，显示全部数据';
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

function renderTable(){
  const rs = st.filtered;
  const max = Math.max(1, Math.ceil(rs.length/PAGE_SIZE));
  st.page = Math.min(st.page, max);
  const start = (st.page-1)*PAGE_SIZE;
  const rows = rs.slice(start, start+PAGE_SIZE).map(r => {
    const m = mid(r);
    const sal = m!==null ? `${esc(r.salary_raw)} <span class="hint">≈${m.toFixed(1)}K/月</span>` : esc(r.salary_raw||'—');
    const u = safeUrl(r.url);
    const title = u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(r.title)}</a>` : esc(r.title);
    const tags = (r.skills||[]).slice(0,6).map(s=>`<span class="tag">${esc(s)}</span>`).join('');
    const det = WEB ? `<td><button class="chip" data-det="1" data-id="${esc(r.id)}"
      data-key="${esc(detKey(r))}">${DET.has(detKey(r)) ? '看正文' : '抓正文'}</button></td>` : '';
    const stCell = WEB ? `<td>
      <select class="st" data-st="${esc(r.id)}">${STATUS.map(s =>
        `<option value="${esc(s)}"${(r.status||'')===s?' selected':''}>${esc(s||'未标注')}</option>`
      ).join('')}</select>
      <input type="text" class="st-note" data-note="${esc(r.id)}" placeholder="记一句"
        maxlength="${NOTE_MAX}" value="${esc(r.note||'')}"></td>`
      : `<td>${esc(r.status||'—')}</td>`;
    return `<tr>
      <td><span class="plat-badge p-${r.platform}">${PNAME[r.platform]||r.platform}</span></td>
      <td>${title}</td><td>${esc(r.company)}</td><td>${sal}</td>
      <td>${esc([r.city,r.district].filter(Boolean).join('·'))}</td>
      <td>${esc(r.experience||'—')}</td><td>${esc(r.education||'—')}</td>
      <td>${tags||'—'}</td><td class="hint">${esc(r.fetched_at)}</td>${stCell}${det}</tr>`;
  }).join('');
  document.getElementById('tbody').innerHTML = rows
    || `<tr><td colspan="${WEB?11:10}" class="hint" style="text-align:center;padding:30px">没有符合筛选条件的职位，试试放宽条件</td></tr>`;
  if (WEB) { bindDetails(); bindStatus(); }
  document.getElementById('pg-info').textContent = rs.length
    ? `第 ${st.page}/${max} 页 · 共 ${rs.length} 条` : '';
}

function exportCSV(){
  const head = ['平台','职位','公司','薪资原文','月薪下限K','月薪上限K','发薪月数','年薪估算万',
    '城市','经验','学历','技能','福利','状态','备注','链接','采集时间'];
  const lines = [head.join(',')];
  st.filtered.forEach(r => {
    const q = v => `"${String(v??'').replace(/"/g,'""')}"`;
    const m = mid(r);
    lines.push([PNAME[r.platform]||r.platform, r.title, r.company, r.salary_raw,
      r.salary_min??'', r.salary_max??'', r.salary_months??'',
      m!==null ? (m*(r.salary_months||12)/10).toFixed(1) : '',
      [r.city,r.district].filter(Boolean).join('·'),
      r.experience, r.education, (r.skills||[]).join(' / '), (r.welfare||[]).join(' / '),
      r.status||'', r.note||'', r.url, r.fetched_at].map(q).join(','));
  });
  const blob = new Blob(['\\ufeff'+lines.join('\\n')], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'filtered-jobs.csv';
  a.click();
}

__COLLECT_SCRIPT____DETAIL_SCRIPT____STATUS_SCRIPT__buildUI();
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
    <input type="text" id="c-kw" class="kw" placeholder="关键词，如 python">
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
  cell.colSpan = 11;
  row.appendChild(cell);
  tr.after(row);
  await showDetail(btn, cell);
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

SLOT_RE = re.compile("|".join(map(re.escape, (
    "__COLLECT_PANEL__", "__COLLECT_SCRIPT__", "__DETAIL_SCRIPT__", "__STATUS_SCRIPT__",
    "__STATUS_CONSTS__", "__CITY_OPTIONS__",
    "__GENERATED_AT__", "__SUB_LINE__", "__BOOT_HOOK__", "__TOKEN__", "__DATA__",
    "__N__", "__IS_WEB__", "__DETAIL_TH__"))))


def _filter_html(sub_line: str, records: list[dict], *, web: bool,
                 token: str = "", city_opts_html: str = "") -> str:
    n = str(len(records))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
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
        "__STATUS_SCRIPT__": STATUS_JS if web else "",
        "__STATUS_CONSTS__": "const STATUS = {}; const NOTE_MAX = {};".format(
            _embed_json(list(db.STATUS_LABELS)), db.NOTE_MAX),
        "__IS_WEB__": "true" if web else "false",
        "__BOOT_HOOK__": "\nresumeTask(); loadDetailKeys();" if web else "",
    }
    # 单次扫描替换：值里若含占位符字面量也不会被二次展开
    return re.sub(SLOT_RE, lambda m: slots[m.group(0)], FILTER_TEMPLATE)


def render_web(records: list[dict], token: str, city_opts_html: str = "") -> str:
    return _filter_html(WEB_SUB_LINE, records, web=True, token=token,
                        city_opts_html=city_opts_html)


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
