"""Boss直聘采集器：真实 Edge 登录态 + 直接调用搜索接口分页。

合规说明：仅采集公开职位列表字段；随机限速；每日额度熔断。
"""
import re

from playwright.sync_api import Error as PlaywrightError

from ..browser import (polite_sleep, check_daily_cap, attach, cdp_alive,
                       interactive_login, login_with_edge)
from ..config import AUTH_DIR, MAX_PAGES_DEFAULT

SEARCH_URL = "https://www.zhipin.com/web/geek/jobs"
API_SEARCH = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
LOGIN_URL = "https://www.zhipin.com/"
# 实测：匿名访问只会拿到 __a/__c/__g/lastCity 之类，登录后才多发这两个
LOGIN_COOKIES = {"bst", "zp_at"}

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


def login_edge():
    """用本机真实 Edge 登录：Boss 会把 Playwright 自建的浏览器页面清空，这条通道不会。"""
    login_with_edge("boss", LOGIN_URL, LOGIN_COOKIES)


def channel_error() -> str | None:
    """采集前自检 Boss 通道，返回该展示给用户的提示；可用则返回 None。"""
    if not (AUTH_DIR / "boss_storage.json").exists():
        return ("Boss直聘 需要先登录：在 job-stats 文件夹的地址栏输入 cmd 回车，运行 "
                "python cli.py login boss --edge，在弹出的 Edge 窗口里扫码登录。")
    if not cdp_alive():
        return ("已登录，但没找到那个 Edge 窗口（可能被关闭了）。请再运行一次 "
                "python cli.py login boss --edge，它会直接打开已登录的页面。")
    return None


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


def _first_line(exc: BaseException) -> str:
    """只取异常第一行。

    Playwright 的报错会附一整段 Call log，里面原样带着请求头——包括 bst/zp_at 登录凭证，
    绝不能跟着 traceback 一起打进终端。
    """
    return (str(exc).splitlines() or [type(exc).__name__])[0]


class Channel:
    """一次请求一次接管：不长期持有 ctx。

    真实 Edge 随时可能被关掉或被后台实例接管，一旦 CDP 断开，攒下来的 ctx 就变成
    `Request context disposed`，整轮采集跟着报废。逐页重连接只多几毫秒，断了却只需重试那一页。
    """

    def __init__(self, attach_fn=attach, retries: int = 2):
        self._attach = attach_fn
        self._retries = retries

    def get_json(self, params: dict) -> dict | None:
        last = ""
        for _ in range(self._retries):
            try:
                with self._attach() as ctx:
                    resp = ctx.request.get(
                        API_SEARCH, params=params,
                        headers={"Referer": SEARCH_URL,
                                 "Accept": "application/json, text/plain, */*"})
                    if resp.status != 200:
                        print(f"  接口返回 HTTP {resp.status}，停止。")
                        return None
                    return resp.json()
            except (PlaywrightError, RuntimeError) as e:
                last = _first_line(e)
                print(f"  Edge 通道中断（{last}），重新接管后重试本页。")
        raise RuntimeError(
            f"Boss 通道反复中断（{last}）。采集期间请把那个 Edge 窗口保持打开；"
            "如果它总是自己退出，先关掉所有 Edge 窗口再运行 python cli.py login boss --edge。")


def _harvest(channel: Channel, query: str, city: str, city_code: str,
             max_pages: int, daily_remaining: int) -> dict[str, dict]:
    """用已登录的浏览器上下文直接调搜索接口，逐页收集职位。

    刻意不开标签页、也不监听响应：SPA 跳转会把接管的标签页换掉，响应体也经常
    取不到（No resource with given identifier）。直接请求只依赖登录 cookie，实测稳定。
    """
    all_jobs: dict[str, dict] = {}
    for pno in range(1, max_pages + 1):
        if len(all_jobs) >= daily_remaining:
            print(f"  已达今日剩余额度 {daily_remaining} 条，停止。")
            break
        print(f"[boss] 第 {pno}/{max_pages} 页: {query} @ {city}")
        data = channel.get_json({"query": query, "city": city_code, "page": pno})
        if data is None:
            break
        if data.get("code") != 0:
            print(f"  接口返回 code={data.get('code')}"
                  f"（平台提示：{data.get('message') or '无'}），停止翻页，保留已采到的部分。")
            break
        zpData = data.get("zpData") or {}
        before = len(all_jobs)
        for item in zpData.get("jobList") or []:
            rec = _parse_item(item)
            if rec:
                all_jobs[rec["platform_job_id"]] = rec
        if len(all_jobs) == before:
            print("  本页没有新增职位（无结果或已翻到头），停止翻页。")
            break
        if not zpData.get("hasMore"):
            print("  平台提示已无下一页，停止。")
            break
        if pno < max_pages:
            polite_sleep()
    return all_jobs


def collect(query: str, city: str = "全国", max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    # 绝不自建浏览器兜底：那样必然被风控，白烧一次额度还给出误导性的"触发风控"结论
    err = channel_error()
    if err:
        raise RuntimeError(err)

    daily_remaining = check_daily_cap()
    city_code = CITY_CODES.get(city, city)

    print("[boss] 使用本机 Edge 通道（采集期间请保持那个窗口开着）")
    jobs = _harvest(Channel(), query, city, city_code, max_pages, daily_remaining)

    records = list(jobs.values())[:daily_remaining]
    if not records:
        raise RuntimeError(
            "一条都没采到：多为触发风控或登录态失效。请隔一段时间再试，"
            "或重新运行 python cli.py login boss --edge 刷新登录。")
    print(f"[boss] 本次去重后采集 {len(records)} 条")
    return records
