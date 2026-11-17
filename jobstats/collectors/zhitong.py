"""智通直聘 (job5156.com) 采集器：无需登录、无需浏览器。

2026-09-23 实测：搜索结果页是服务端渲染的 HTML，每条职位是一个
`<li class="position pos-item PosName" data-...>`，字段全在 data-* 属性里；
翻页参数是 `pn`。站点的 WAF 会先发 CT6T cookie 再 302，带 cookie jar 重跟即可，
所以直接走 HTTP，不再用浏览器嗅探 XHR（旧实现选 `.job-card` 一直选空，才落得"实验性"）。

合规说明：仅采集公开职位列表字段；随机限速；每日额度熔断。
"""
import html
import http.cookiejar
import re
import urllib.parse
import urllib.request

from .. import db
from .. import html_text
from .. import relevance
from ..browser import polite_sleep, check_daily_cap
from ..config import MAX_PAGES_DEFAULT, REQUEST_TIMEOUT_MS

SITE = "https://www.job5156.com"
SEARCH_URL = f"{SITE}/s/search/"
# 城市直接用中文名传参（实测有效）；“全国”留空即不传
NO_CITY = {"", "全国"}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 职位条目：<li ... class="position pos-item PosName" ...>
_ITEM_RE = re.compile(r'<li\b([^>]*class="[^"]*pos-item[^"]*"[^>]*)>')
_ATTR_RE = re.compile(r'data-([a-z0-9-]+)="([^"]*)"')
_SALARY_RE = re.compile(r'class="salary"[^>]*>(.*?)</', re.S)
_TAG_RE = re.compile(r'class="tag-list-item"[^>]*>(.*?)</', re.S)
_WELFARE_RE = re.compile(r'class="pos-welfare"[^>]*title="([^"]*)"')
# 公司规模也在这串标签里，且只有这一种长得像"数字+人"（镇区是"城市-镇区"，学历经验不带人字）
_SCALE_RE = re.compile(r"^\d[\d,]*(?:-\d[\d,]*)?(?:以上)?人$|^\d[\d,]+人以上$")
_BLANK_RE = re.compile(r"\s+")

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))


def login():
    print("[zhitong] 智通直聘搜索列表无需登录，跳过。")


def _clean(text: str) -> str:
    return _BLANK_RE.sub(" ", html.unescape(text or "")).strip()


def _search_url(query: str, city: str, page: int) -> str:
    params = {"keyword": query, "searchKeywordFrom": "1", "dataVersion": ""}
    if city and city not in NO_CITY:
        params["city"] = city
    if page > 1:
        params["pn"] = str(page)
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"{SITE}/",
    })
    with _opener.open(req, timeout=REQUEST_TIMEOUT_MS / 1000) as resp:
        return resp.read().decode("utf-8", "replace")


def _salary(attrs: dict, block: str):
    """优先用页面展示的薪资文本，展示位缺失时退回条目上的数字属性。"""
    m = _SALARY_RE.search(block)
    raw = _clean(m.group(1)) if m else ""
    parsed = db.parse_salary(raw)
    if parsed and parsed[0] != parsed[1]:
        return raw, parsed[0], parsed[1], parsed[2]
    # 展示位是 '10K' 这种单值时，条目属性里的 minsalary/maxsalary 才是真实区间
    lo, hi = _num(attrs.get("minsalary")), _num(attrs.get("maxsalary"))
    if lo and hi:
        return f"{lo / 1000:g}-{hi / 1000:g}K", lo / 1000, hi / 1000, None
    if parsed:
        return raw, parsed[0], parsed[1], parsed[2]
    return raw, None, None, None


def _num(text):
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_page(body: str) -> list[dict]:
    """把搜索结果页 HTML 解析成职位记录列表（纯函数，便于离线测试）。"""
    return _parse_page(body)[0]


def _parse_page(body: str) -> tuple[list[dict], int, int]:
    """同上，但额外带回页面上看到的条目总数和置顶数——0 条时靠它们分清
    「平台改版了」和「真的没结果」，否则话术只能一概推给风控，人就照着错的方向排查。"""
    marks = list(_ITEM_RE.finditer(body))
    records = []
    pinned = 0
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        block = body[start:end]
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        if attrs.get("top") == "true":
            # 平台把付费置顶的岗位塞在同一个列表里，它们不是关键词命中的结果
            pinned += 1
            continue
        rec = _record(attrs, block)
        if rec:
            records.append(rec)
    if pinned:
        print(f"  跳过平台置顶推广位 {pinned} 条")
    return records, len(marks), pinned


def _district(city: str, block: str) -> str:
    """镇区藏在“城市-镇区”那枚标签里。"""
    for raw in _TAG_RE.findall(block):
        parts = _clean(raw).split("-", 1)
        if len(parts) == 2 and parts[1] and (not city or parts[0] == city):
            return parts[1]
    return ""


def _scale(block: str) -> str:
    """公司规模同样在这串标签里：`1-100人`、`200-500人`、`2000以上人`（最后一种是平台自己的写法）。

    实测（2026-10-18）智通列表页的标签只有六类——城市-镇区/学历/经验/公司性质/公司规模/行业，
    技能一列是没有的，所以以前这个字段一直写死空串，白瞎了页面上现成的信息。
    """
    for raw in _TAG_RE.findall(block):
        t = _clean(raw)
        if _SCALE_RE.match(t):
            return t
    return ""


def _record(attrs: dict, block: str) -> dict | None:
    jid = attrs.get("id") or attrs.get("positionkey")
    title = _clean(attrs.get("posname"))
    if not jid or not title:
        return None
    key = attrs.get("positionkey") or jid
    city_en = attrs.get("urlcityen") or ""
    city = _clean(attrs.get("cityname"))
    salary_raw, lo, hi, months = _salary(attrs, block)
    district = _clean(attrs.get("townname")) or _district(city, block)
    industry = "-".join(x for x in (_clean(attrs.get("first")), _clean(attrs.get("second"))) if x)
    wm = _WELFARE_RE.search(block)
    welfare = [w.strip() for w in _clean(wm.group(1)).split(",")] if wm else []
    return {
        "platform": "zhitong",
        "platform_job_id": str(jid),
        "title": title,
        "company": _clean(attrs.get("comname")),
        "salary_raw": salary_raw,
        "salary_min": lo, "salary_max": hi, "salary_months": months,
        "city": city,
        "district": district,
        "experience": _clean(attrs.get("reqworkyear")),
        "education": _clean(attrs.get("reqdegreestr")),
        "industry": industry,
        "company_scale": _scale(block),
        "skills": [],
        "welfare": welfare,
        "url": f"{SITE}/{city_en}/job_{key}" if city_en else "",
    }


# 这条通道支持每页一存（collect 的 on_page）；调用方据此决定还要不要在轮末再存一遍。
# Boss 那条通道没这个标记，仍然是整轮跑完存一次——那个文件不在本轮改动范围内。
PER_PAGE_SAVE = True
# 这条通道自己在 collect 里过了相关性闸门，公共落库口不用再判一遍。
SELF_GATES = True
# 智通的 data-first/data-second 是「一级行业-二级职类」，第二段是职能（锅炉操作工标的是
# 生产制造-技工）。闸门拿它判方向。Boss 的 industry 是公司行业，同一个判据会把
# "园区弱电IT运维工程师"（标环保）误杀，所以那条通道不开这个标记。
FAMILY_GATE = True


def collect(query: str, city: str = "东莞", max_pages: int = MAX_PAGES_DEFAULT,
            on_page=None, on_drop=None) -> list[dict]:
    """翻页采集。给了 `on_page` 就每采完一页立刻入库一次。

    整轮跑完才写库的话，第 4 页崩了就前面 3 页白采——限时段里跑一晚上什么痕迹都不留。
    `on_drop` 每页收一次全量累计的被拦明细（原因 → 标题），见 job51.py 里同名的说明。
    """
    daily_remaining = check_daily_cap()
    all_jobs: dict[str, dict] = {}
    dropped: dict[str, list[str]] = {}
    seen: set[str] = set()      # 本轮见过的所有职位编号，闸门拦掉的也算
    flushed = 0                 # 已经交给 on_page 落库的条数

    for pno in range(1, max_pages + 1):
        if len(all_jobs) >= daily_remaining:
            print(f"  已达今日剩余额度 {daily_remaining} 条，停止。")
            break
        print(f"[zhitong] 第 {pno}/{max_pages} 页: {query} @ {city}")
        try:
            body = _fetch(_search_url(query, city, pno))
        except Exception as e:
            print(f"  页面获取失败: {str(e)[:90]}，停止翻页。")
            break
        page_jobs, cards, pinned = _parse_page(body)
        if not page_jobs:
            if cards and cards == pinned:
                print(f"  本页 {cards} 条全是平台置顶推广位，没有关键词命中的结果，停止翻页。")
            elif cards:
                print(f"  本页看到 {cards} 个职位条目却一条都没解析出来："
                      "是平台改了类名或 data-* 字段（结构变了），不是没结果也不是风控，"
                      "停止翻页——要改的是 zhitong.py 的选择器，不是你的账号。")
            else:
                print("  本页没有职位条目（无结果或已翻到头），停止翻页。")
            break
        # 按"这页有没有新编号"判断是否翻到头。整页都被闸门拦下不等于没结果，
        # 下一页可能全是相关的——所以闸门拦掉的也算"见过"，但不算"新"。
        page_ids = {rec["platform_job_id"] for rec in page_jobs}
        fresh = bool(page_ids - seen)
        seen |= page_ids
        kept, why = relevance.gate(page_jobs, query, city, family=FAMILY_GATE)
        relevance.merge(why, dropped)
        for rec in kept:
            all_jobs.setdefault(rec["platform_job_id"], rec)
        if on_drop:
            on_drop(dropped)
        if on_page:
            room = daily_remaining - flushed
            batch = list(all_jobs.values())[flushed:flushed + room]
            if batch:
                on_page(batch)
                flushed += len(batch)
        if not fresh:
            print("  本页没有新增职位（可能已翻到头），停止翻页。")
            break
        if pno < max_pages:
            polite_sleep()

    records = list(all_jobs.values())[:daily_remaining]
    print(f"[zhitong] 本次去重后采集 {len(records)} 条")
    if dropped:
        print(f"  相关性闸门另剔掉 {relevance.count(dropped)} 条：{relevance.summarize(dropped)}"
              "（想放宽就别传具体城市，或用更长的关键词）")
        print(f"  剔掉的：{relevance.titles(dropped)}")
    return records


# ---- 职位正文 ----
# 详情页同样是服务端渲染：正文在 <pre class="common-text-desc pos-desc"> 里原样带着换行，
# 公司简介用的是 com-desc 类名，靠 pos-desc 区分开。
# 2026-11-16 实测平台把标签属性拆成了多行（<pre\n class="…"\n>），旧正则只认
# class 紧跟标签名的单行写法，正文会静默解析成 0 字；[^>]* 本来就吃换行，
# 把"标签名与 class 之间"放宽成任意属性即可，两种写法都认。
_DESC_RE = re.compile(r'<pre\b[^>]*\bclass="[^"]*\bpos-desc\b[^"]*"[^>]*>(.*?)</pre>', re.S)
_KEYWORDS_RE = re.compile(r'<ul\b[^>]*\bclass="keyword-tags[^"]*"[^>]*>(.*?)</ul>', re.S)


def parse_detail(body: str) -> dict:
    """把详情页 HTML 解析成正文 + 关键词（纯函数，便于离线测试）。"""
    m = _DESC_RE.search(body)
    km = _KEYWORDS_RE.search(body)
    return {"description": html_text.to_lines(m.group(1)) if m else "",
            "keyword_tags": html_text.list_items(km.group(1)) if km else []}


def fetch_detail(job: dict) -> dict | None:
    url = job.get("url") or ""
    if not url:
        print("  这条没有详情页链接，跳过。")
        return None
    try:
        body = _fetch(url)
    except Exception as e:
        print(f"  详情页获取失败: {str(e)[:90]}")
        return None
    parsed = parse_detail(body)
    if not parsed["description"]:
        print("  详情页没解析出正文（职位可能已下架，或页面结构变了）。")
        return None
    return dict(parsed, platform=job["platform"],
                platform_job_id=job["platform_job_id"], url=url)
