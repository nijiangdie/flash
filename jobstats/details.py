"""按职位逐条抓详情页正文（JD）：单独的小额度、单独的熔断。

列表页一次翻页能带出二十条，详情页必须一条条打开，请求数涨得比列表快得多，
所以这里不复用 DAILY_CAP，而是单独一份 DETAIL_DAILY_CAP，并且每抓一条就限速一次。
碰到平台的人机验证，采集器会直接抛错，整批停在这里，不重试、不改频率。

不是每个平台都有正文：拿不到正文的平台写在 NO_DETAIL 里，这一层直接跳过并说明原因，
不让页面和 CLI 把"这条通道不抓正文"说成"职位已下架"。
"""
from . import db
from .browser import polite_sleep
from .collectors import boss, zhitong
from .config import DETAIL_DAILY_CAP

FETCHERS = {"boss": boss.fetch_detail, "zhitong": zhitong.fetch_detail}

# 有的平台的正文根本拿不到，而原因不是"这条职位出问题了"。
# 这句话给页面、CLI 和接口共用，免得再把能力限制说成下架或风控。
NO_DETAIL = {
    "job51": "前程无忧这条通道不抓正文：实测（2026-09-27）从列表点进详情页直接弹滑块验证，"
             "按合规红线不绕，所以一条正文也拿不到。职位在平台上是正常的；"
             "要正文请用智通或 Boss 采到的同类岗位。",
}


def check_detail_cap() -> int:
    """超额度则抛错熔断，否则返回今日还能抓几条正文。"""
    used = db.count_details_today()
    if used >= DETAIL_DAILY_CAP:
        raise RuntimeError(
            f"已触发每日正文额度熔断：今日已抓 {used} 条（上限 {DETAIL_DAILY_CAP}）。"
            "逐条打开详情页比翻页更容易触发风控，请明天再跑，"
            "不要调大 config.py 里的 DETAIL_DAILY_CAP。")
    print(f"  今日正文已抓 {used}/{DETAIL_DAILY_CAP} 条")
    return DETAIL_DAILY_CAP - used


def collect_details(jobs: list[dict], limit: int | None = None,
                    force: bool = False) -> dict:
    """逐条抓正文入库。已有正文的默认跳过（force=True 则重抓）；
    NO_DETAIL 里的平台一条都不试，直接算进 unreachable。"""
    remaining = check_detail_cap()
    if limit:
        remaining = min(remaining, limit)

    todo, skipped, unreachable = [], 0, 0
    for j in jobs:
        if j["platform"] in NO_DETAIL:
            unreachable += 1
            continue
        if j["platform"] not in FETCHERS:
            print(f"  不认识的平台 {j['platform']}，跳过。")
            continue
        if not force and db.get_detail(j["platform"], j["platform_job_id"]):
            skipped += 1
            continue
        todo.append(j)
    if unreachable:
        names = sorted({j["platform"] for j in jobs if j["platform"] in NO_DETAIL})
        print(f"  {unreachable} 条属于拿不到正文的平台，不占额度也不占名额："
              + "、".join(names))
        for n in names:
            print(f"    {NO_DETAIL[n]}")
    if skipped:
        print(f"  {skipped} 条已有正文，本次跳过（要重抓加 --force）。")
    todo = todo[:remaining]
    if not todo:
        print("[正文] 没有需要抓的职位（都已抓过、属于不抓正文的平台，或今日额度已用完）。")
        return {"saved": 0, "failed": 0, "skipped": skipped,
                "unreachable": unreachable}

    # Boss 走真实 Edge 通道，和采集一样先把窗口准备好
    if any(j["platform"] == "boss" for j in todo):
        hint = boss.ensure_window()
        if hint:
            raise RuntimeError(hint)

    saved = failed = 0
    for n, job in enumerate(todo, 1):
        print(f"[正文 {n}/{len(todo)}] {job['platform']} "
              f"#{job['platform_job_id']} {job.get('title') or ''} · "
              f"{job.get('company') or ''}")
        try:
            rec = FETCHERS[job["platform"]](job)
        except RuntimeError:
            raise      # 验证/通道类错误直接往上抛，整批停住
        except Exception as e:
            failed += 1
            print(f"  这条抓取失败: {str(e)[:90]}")
            continue
        if rec:
            saved += db.save_details([rec])
        else:
            failed += 1
        if n < len(todo):
            polite_sleep()

    left = max(0, DETAIL_DAILY_CAP - db.count_details_today())
    extra = f"，属于不抓正文的平台 {unreachable} 条" if unreachable else ""
    print(f"[正文] 本次写入 {saved} 条，失败 {failed} 条，跳过已有 {skipped} 条{extra}；"
          f"今日还能抓 {left} 条")
    return {"saved": saved, "failed": failed, "skipped": skipped,
            "unreachable": unreachable, "left": left}
