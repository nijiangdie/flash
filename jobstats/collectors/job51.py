"""前程无忧 (we.51job.com) 采集器：无需登录，DOM+sensorsdata 结构化解析（已实测）。"""
import json
import re
import time

from .. import db
from .. import relevance
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


# 支持每页一存（collect 的 on_page），见 zhitong.py 里同名的说明。
PER_PAGE_SAVE = True
# 这条通道自己在 collect 里过了相关性闸门，公共落库口不用再判一遍。
SELF_GATES = True


def collect(query: str, city: str = "全国", max_pages: int = MAX_PAGES_DEFAULT,
            on_page=None, on_drop=None) -> list[dict]:
    """翻页采集。给了 `on_page` 就每采完一页立刻入库一次，免得第 4 页崩了前面白采。

    `on_drop` 每页收一次**全量累计**的（原因 → 被拦标题）明细：这条通道自己在采集里判，
    公共落库口看不到，不给它就没有留痕，误杀了事后查不到。中途崩了也至少留下上一页的明细。
    """
    daily_remaining = check_daily_cap()
    city_code = CITY_CODES.get(city, city)
    all_jobs: dict[str, dict] = {}
    seen_ids: set[str] = set()      # 含被闸门剔掉的，用来区分"翻到头"和"整页不合相关性"
    dropped: dict[str, list[str]] = {}
    flushed = 0                     # 已经交给 on_page 落库的条数

    p, browser = launch(headless=True)
    try:
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        page = ctx.new_page()
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
            n_cards = cards.count()
            fresh = 0
            no_id = 0
            for i in range(n_cards):
                rec = _parse_sensors(cards.nth(i))
                if not rec or not rec["platform_job_id"]:
                    no_id += 1
                    continue
                jid = rec["platform_job_id"]
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                fresh += 1
                why = relevance.reject_reason(rec, query, city)
                if why:
                    # 拦在补全公司信息之前：反正不入库，省一次 DOM 调用
                    dropped.setdefault(why, []).append(rec["title"])
                    continue
                _fill_card_extras(cards.nth(i), rec)
                all_jobs[jid] = rec
            if on_drop:
                on_drop(dropped)
            if on_page:
                room = daily_remaining - flushed
                batch = list(all_jobs.values())[flushed:flushed + room]
                if batch:
                    on_page(batch)
                    flushed += len(batch)
            if n_cards and no_id == n_cards:
                # 卡片在、字段没了：这是平台改版，不是风控，也不是没结果
                print(f"  {n_cards} 张卡片都取不到职位编号（结构变了，平台改了埋点字段），"
                      "要改的是 job51.py 的 _parse_sensors，不是你的账号，停止。")
                break
            if pno > 1 and not fresh:
                print("  本页无新增数据（可能无更多页），停止。")
                break
            if pno < max_pages:
                polite_sleep()
        page.close()
    finally:
        browser.close()
        p.stop()

    records = list(all_jobs.values())[:daily_remaining]
    print(f"[job51] 本次去重后采集 {len(records)} 条")
    if dropped:
        print(f"  相关性闸门另剔掉 {relevance.count(dropped)} 条：{relevance.summarize(dropped)}"
              "（想放宽就别传具体城市，或用更长的关键词）")
        print(f"  剔掉的：{relevance.titles(dropped)}")
    return records


# 这个平台没有 fetch_detail：正文能力的事实在 details.NO_DETAIL 里，
# 那里是唯一一处，页面、CLI 和接口都引用它。
