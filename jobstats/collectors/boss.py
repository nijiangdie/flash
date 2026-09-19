"""Boss直聘采集器：登录态 + 搜索页翻页 + 拦截 wapi/joblist.json 结构化数据。

合规说明：仅采集公开职位列表字段；随机限速；每日额度熔断。
"""
import json
import math
import re
import time
from urllib.parse import quote

from ..browser import polite_sleep, check_daily_cap, Session, interactive_login
from ..config import MAX_PAGES_DEFAULT

SEARCH_URL = "https://www.zhipin.com/web/geek/jobs"
LOGIN_URL = "https://www.zhipin.com/"

# 常用城市代码（Boss 站点口径），不在表内可传原始代码或“全国”
CITY_CODES = {
    "全国": "100010000", "北京": "101010100", "上海": "101020100",
    "广州": "101280100", "深圳": "101280600", "杭州": "101210100",
    "成都": "101270100", "武汉": "101200100", "南京": "101190100",
    "西安": "101110100", "苏州": "101190400", "东莞": "101281600",
    "佛山": "101280800", "重庆": "101040100", "长沙": "101250100",
    "郑州": "101180100", "天津": "101030100", "合肥": "101220100",
    "厦门": "101230200", "福州": "101230100", "青岛": "101120200",
    "济南": "101120100", "宁波": "101210400", "无锡": "101190200",
}


def login():
    def check(page):
        try:
            return page.locator("span.btn-outline, .header-login-btn").count() == 0 \
                and page.locator("text=开始沟通").count() > 0
        except Exception:
            return False
    interactive_login("boss", LOGIN_URL, check)


def _parse_item(item: dict) -> dict | None:
    jid = item.get("encryptJobId") or item.get("securityId")
    if not jid:
        return None
    raw_salary = item.get("salaryDesc") or ""
    lo = hi = None
    months = None
    m = re.match(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", raw_salary, re.I)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        m2 = re.search(r"·(\d+)薪", raw_salary)
        months = int(m2.group(1)) if m2 else None
    return {
        "platform": "boss",
        "platform_job_id": str(jid),
        "title": item.get("jobName") or "",
        "company": item.get("brandName") or "",
        "salary_raw": raw_salary,
        "salary_min": lo, "salary_max": hi, "salary_months": months,
        "city": item.get("cityName") or "",
        "district": item.get("areaDistrict") or item.get("businessDistrict") or "",
        "experience": item.get("jobExperience") or "",
        "education": item.get("jobDegree") or "",
        "industry": item.get("brandIndustry") or "",
        "company_scale": item.get("brandScaleName") or "",
        "skills": item.get("skills") or item.get("jobLabels") or [],
        "welfare": item.get("welfareList") or [],
        "url": f"https://www.zhipin.com/job_detail/{jid}.html",
    }


def collect(query: str, city: str = "全国", max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    daily_remaining = check_daily_cap()
    city_code = CITY_CODES.get(city, city)
    all_jobs: dict[str, dict] = {}

    with Session("boss") as ctx:
        page = ctx.new_page()

        def on_response(resp):
            if "joblist.json" not in resp.url or not resp.ok:
                return
            try:
                data = resp.json()
            except Exception:
                return
            if data.get("code") != 0:
                code = data.get("code")
                raise RuntimeError(
                    f"Boss 返回异常 code={code}（403/4104 等通常为风控触发）。"
                    "请降低频率、稍后再试，或重新 login 刷新登录态。"
                )
            for item in data.get("zpData", {}).get("jobList", []) or []:
                rec = _parse_item(item)
                if rec:
                    all_jobs[rec["platform_job_id"]] = rec

        page.on("response", on_response)
        before_prev = 0

        for pno in range(1, max_pages + 1):
            if len(all_jobs) >= daily_remaining:
                print(f"  已达今日剩余额度 {daily_remaining} 条，停止。")
                break
            url = f"{SEARCH_URL}?query={quote(query)}&city={city_code}&page={pno}"
            print(f"[boss] 第 {pno}/{max_pages} 页: {query} @ {city}")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(".job-card-wrapper, .job-list-box", timeout=15000)
            except Exception:
                print("  未检测到职位卡片（可能触发验证码或已无更多数据），停止。")
                break
            page.mouse.wheel(0, 3000)  # 触发懒加载
            time.sleep(2)
            current = len(all_jobs)
            if pno > 1 and current == before_prev:
                print("  本页无新增数据，停止翻页。")
                break
            before_prev = current
            if pno < max_pages:
                polite_sleep()

        page.close()

    records = list(all_jobs.values())[:daily_remaining]
    print(f"[boss] 本次去重后采集 {len(records)} 条")
    return records
