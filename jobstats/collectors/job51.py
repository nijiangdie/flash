"""前程无忧 (we.51job.com) 采集器：无需登录，DOM+sensorsdata 结构化解析（已实测）。"""
import json
import re
import time

from .. import db
from ..browser import polite_sleep, check_daily_cap, launch
from ..config import MAX_PAGES_DEFAULT

SEARCH_URL = "https://we.51job.com/pc/search"

# 51job 城市代码（平台长期稳定的经典编码），未收录的城市请到网站筛选后从 URL 抄代码
CITY_CODES = {
    "全国": "000000", "北京": "010000", "上海": "020000", "广州": "030200",
    "深圳": "040000", "武汉": "180200", "西安": "200200", "南京": "070200",
    "杭州": "080200", "成都": "090200", "重庆": "060200", "苏州": "070300",
    "天津": "050000", "长沙": "190200", "郑州": "170200", "东莞": "030800",
}


def login():  # 51job 搜索列表无需登录
    print("[job51] 前程无忧搜索列表无需登录，跳过。")


def _parse_sensors(card) -> dict | None:
    raw = card.get_attribute("sensorsdata")
    if not raw:
        return None
    try:
        s = json.loads(raw)
    except Exception:
        return None
    area = (s.get("jobArea") or "")
    city, _, district = area.partition("·")
    salary_raw = s.get("jobSalary") or ""
    parsed = db.parse_salary(salary_raw)
    lo, hi, months = parsed if parsed else (None, None, None)
    return {
        "platform": "job51",
        "platform_job_id": str(s.get("jobId") or ""),
        "title": s.get("jobTitle") or "",
        "company": "",  # 在主流程补齐
        "salary_raw": salary_raw,
        "salary_min": lo, "salary_max": hi, "salary_months": months,
        "city": city, "district": district,
        "experience": s.get("jobYear") or "",
        "education": s.get("jobDegree") or "",
        "industry": "", "company_scale": "",
        "skills": [], "welfare": [],
        "url": "",
    }


def _fill_card_extras(card, rec: dict):
    try:
        rec["company"] = card.locator(
            ".joblist-item-right .text-cut, .cname, .company-name"
        ).first.inner_text(timeout=2000).strip()
    except Exception:
        try:
            rec["company"] = card.locator(".joblist-item-right").first.inner_text(
                timeout=2000).strip().split("\n")[0]
        except Exception:
            pass
    try:
        rec["skills"] = card.locator(".joblist-item-tags .tag").all_inner_texts()
    except Exception:
        pass
    try:
        href = card.locator("a[href]").first.get_attribute("href", timeout=2000)
        if href:
            rec["url"] = href if href.startswith("http") else f"https:{href}"
    except Exception:
        pass


def collect(query: str, city: str = "全国", max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    daily_remaining = check_daily_cap()
    city_code = CITY_CODES.get(city, city)
    all_jobs: dict[str, dict] = {}

    p, browser = launch(headless=True)
    try:
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        page = ctx.new_page()
        before_prev = 0
        for pno in range(1, max_pages + 1):
            if len(all_jobs) >= daily_remaining:
                print(f"  已达今日剩余额度 {daily_remaining} 条，停止。")
                break
            url = f"{SEARCH_URL}?keyword={query}&jobArea={city_code}&pageNum={pno}"
            print(f"[job51] 第 {pno}/{max_pages} 页: {query} @ {city}")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(".joblist-item-job", state="attached", timeout=30000)
            except Exception:
                # 偶发首次加载慢，再等 10s 重试一次
                time.sleep(10)
                if page.locator(".joblist-item-job").count() == 0:
                    print("  未检测到职位卡片（可能触发风控或无结果），停止。")
                    break
            time.sleep(3)
            cards = page.locator(".joblist-item-job")
            for i in range(cards.count()):
                rec = _parse_sensors(cards.nth(i))
                if not rec or not rec["platform_job_id"]:
                    continue
                if rec["platform_job_id"] in all_jobs:
                    continue
                _fill_card_extras(cards.nth(i), rec)
                all_jobs[rec["platform_job_id"]] = rec
            current = len(all_jobs)
            if pno > 1 and current == before_prev:
                print("  本页无新增数据（可能无更多页），停止。")
                break
            before_prev = current
            if pno < max_pages:
                polite_sleep()
        page.close()
    finally:
        browser.close()
        p.stop()

    records = list(all_jobs.values())[:daily_remaining]
    print(f"[job51] 本次去重后采集 {len(records)} 条")
    return records


def fetch_detail(job: dict) -> dict | None:
    """前程无忧的详情页拿不到正文：实测（2026-09-27）从列表点进去直接弹滑块验证。

    按红线"遇到验证即停、不绕"，这里刻意一个请求都不发。正文请用智通或 Boss。
    """
    print("  前程无忧详情页要求滑块验证，按合规红线不抓正文（请改用智通或 Boss）。")
    return None
