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
    if parsed:
        return raw, parsed[0], parsed[1], parsed[2]
    lo, hi = _num(attrs.get("minsalary")), _num(attrs.get("maxsalary"))
    if lo and hi:
        return f"{lo / 1000:g}-{hi / 1000:g}K", lo / 1000, hi / 1000, None
    return raw, None, None, None


def _num(text):
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_page(body: str) -> list[dict]:
    """把搜索结果页 HTML 解析成职位记录列表（纯函数，便于离线测试）。"""
    marks = list(_ITEM_RE.finditer(body))
    records = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        block = body[start:end]
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        rec = _record(attrs, block)
        if rec:
            records.append(rec)
    return records


def _district(city: str, block: str) -> str:
    """镇区藏在“城市-镇区”那枚标签里。"""
    for raw in _TAG_RE.findall(block):
        parts = _clean(raw).split("-", 1)
        if len(parts) == 2 and parts[1] and (not city or parts[0] == city):
            return parts[1]
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
        "company_scale": "",
        "skills": [],
        "welfare": welfare,
        "url": f"{SITE}/{city_en}/job_{key}" if city_en else "",
    }


def collect(query: str, city: str = "东莞", max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    daily_remaining = check_daily_cap()
    all_jobs: dict[str, dict] = {}

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
        page_jobs = parse_page(body)
        if not page_jobs:
            print("  本页没解析出职位（无结果，或页面结构变了），停止翻页。")
            break
        before = len(all_jobs)
        for rec in page_jobs:
            all_jobs.setdefault(rec["platform_job_id"], rec)
        if len(all_jobs) == before:
            print("  本页没有新增职位（可能已翻到头），停止翻页。")
            break
        if pno < max_pages:
            polite_sleep()

    records = list(all_jobs.values())[:daily_remaining]
    print(f"[zhitong] 本次去重后采集 {len(records)} 条")
    return records
