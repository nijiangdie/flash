"""智通直聘 (job5156.com) 采集器【实验性】。

搜索结果由前端动态加载、接口不固定，这里采用两层策略：
1. XHR 通用嗅探：拦截所有 JSON 响应，自动识别包含职位数组的结构；
2. DOM 兜底：解析 job-card 系列卡片。
接口变化时可能采不到数据——主力平台建议用 boss / job51。
"""
import json
import time

from .. import db
from ..browser import polite_sleep, check_daily_cap, launch
from ..config import MAX_PAGES_DEFAULT

SITE = "https://www.job5156.com"

# 常用城市拼音前缀（来自站点城市列表）
CITY_CODES = {
    "全国": "", "东莞": "dongguan", "深圳": "shenzhen", "广州": "guangzhou",
    "佛山": "foshan", "中山": "zhongshan", "惠州": "huizhou", "珠海": "zhuhai",
    "长沙": "changsha", "武汉": "wuhan", "南京": "nanjing", "宁波": "ningbo",
    "南昌": "nanchang", "湘潭": "xiangtan", "泉州": "quanzhou",
}

# XHR JSON 中识别职位数组用的字段名候选
_TITLE_KEYS = ["posName", "jobName", "positionName", "zwName", "name"]
_COMPANY_KEYS = ["coName", "companyName", "comName", "corpName"]
_SALARY_KEYS = ["salary", "zwSalary", "salaryStr", "salaryDesc", "pay"]
_CITY_KEYS = ["site", "cityName", "areaName", "workPlace", "address"]
_EXP_KEYS = ["workYear", "exp", "experience", "yearReq"]
_EDU_KEYS = ["degreeStr", "education", "eduReq", "degree"]


def login():
    print("[zhitong] 智通直聘搜索列表无需登录，跳过。")


def _find_job_lists(obj, out):
    """递归查找 JSON 里『看起来像职位列表』的数组。"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            sample = obj[0]
            if any(k in sample for k in _TITLE_KEYS) and any(k in sample for k in _SALARY_KEYS + _CITY_KEYS):
                out.extend(obj)
        for x in obj:
            _find_job_lists(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_job_lists(v, out)


def _map_item(d: dict) -> dict | None:
    def g(keys):
        for k in keys:
            if d.get(k):
                return str(d[k])
        return ""
    jid = str(d.get("jobId") or d.get("posId") or d.get("id") or "")
    title = g(_TITLE_KEYS)
    if not jid or not title:
        return None
    raw_salary = g(_SALARY_KEYS)
    if "面议" in raw_salary:
        raw_salary = ""
    parsed = db.parse_salary(raw_salary)
    lo, hi = (parsed[0], parsed[1]) if parsed else (None, None)
    area = g(_CITY_KEYS)
    city, _, district = area.partition("·") if area else ("", "", "")
    return {
        "platform": "zhitong",
        "platform_job_id": jid,
        "title": title,
        "company": g(_COMPANY_KEYS),
        "salary_raw": raw_salary,
        "salary_min": lo, "salary_max": hi, "salary_months": None,
        "city": city, "district": district,
        "experience": g(_EXP_KEYS), "education": g(_EDU_KEYS),
        "industry": "", "company_scale": "",
        "skills": [], "welfare": [], "url": "",
    }


def collect(query: str, city: str = "东莞", max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    daily_remaining = check_daily_cap()
    prefix = CITY_CODES.get(city, "")
    all_jobs: dict[str, dict] = {}

    p, browser = launch(headless=False)  # 实验性通道建议有头运行，便于观察
    try:
        ctx = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
        page = ctx.new_page()

        def on_response(resp):
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                data = resp.json()
            except Exception:
                return
            found: list = []
            _find_job_lists(data, found)
            for d in found:
                rec = _map_item(d)
                if rec:
                    all_jobs.setdefault(rec["platform_job_id"], rec)

        page.on("response", on_response)

        for pno in range(1, max_pages + 1):
            if len(all_jobs) >= daily_remaining:
                break
            base = f"{SITE}/{prefix}/" if prefix else f"{SITE}/"
            url = f"{base}search/?keyword={query}&page={pno}"
            print(f"[zhitong] 第 {pno}/{max_pages} 页: {query} @ {city}（实验性）")
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                print(f"  页面加载失败: {str(e)[:80]}")
                break
            time.sleep(6)
            page.mouse.wheel(0, 2500)
            time.sleep(2)
            # DOM 兜底
            cards = page.locator(".job-card")
            for i in range(cards.count()):
                c = cards.nth(i)
                try:
                    title = c.locator(".job-card_posName").first.inner_text(timeout=1000).strip()
                except Exception:
                    continue
                jid = title  # DOM 兜底没有稳定 id，用标题+公司当键
                try:
                    company = c.locator(".job-card_logo + *, [class*=company]").first.inner_text(timeout=1000).strip()
                except Exception:
                    company = ""
                try:
                    salary = c.locator(".job-card_tip1, [class*=salary]").first.inner_text(timeout=1000).strip()
                except Exception:
                    salary = ""
                rec = _map_item({"posName": title, "coName": company, "salary": salary,
                                 "site": city, "id": f"dom:{jid}:{company}"})
                if rec:
                    all_jobs.setdefault(rec["platform_job_id"], rec)
            if pno < max_pages:
                polite_sleep()
        page.close()
    finally:
        browser.close()
        p.stop()

    records = list(all_jobs.values())[:daily_remaining]
    print(f"[zhitong] 本次去重后采集 {len(records)} 条（实验性通道，结果可能不完整）")
    return records
