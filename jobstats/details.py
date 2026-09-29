"""按职位逐条抓详情页正文（JD）：单独的小额度、单独的熔断。

列表页一次翻页能带出二十条，详情页必须一条条打开，请求数涨得比列表快得多，
所以这里不复用 DAILY_CAP，而是单独一份 DETAIL_DAILY_CAP，并且每抓一条就限速一次。
碰到平台的人机验证，采集器会直接抛错，整批停在这里，不重试、不改频率。
"""
from . import db
from .browser import polite_sleep
from .collectors import boss, job51, zhitong
from .config import DETAIL_DAILY_CAP

FETCHERS = {"boss": boss.fetch_detail, "job51": job51.fetch_detail,
            "zhitong": zhitong.fetch_detail}


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
    """逐条抓正文入库。已有正文的默认跳过（force=True 则重抓）。"""
    remaining = check_detail_cap()
    if limit:
        remaining = min(remaining, limit)

    todo, skipped = [], 0
    for j in jobs:
        if j["platform"] not in FETCHERS:
            print(f"  不认识的平台 {j['platform']}，跳过。")
            continue
        if not force and db.get_detail(j["platform"], j["platform_job_id"]):
            skipped += 1
            continue
        todo.append(j)
    if skipped:
        print(f"  {skipped} 条已有正文，本次跳过（要重抓加 --force）。")
    todo = todo[:remaining]
    if not todo:
        print("[正文] 没有需要抓的职位（都已抓过，或今日额度已用完）。")
        return {"saved": 0, "failed": 0, "skipped": skipped}

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
    print(f"[正文] 本次写入 {saved} 条，失败 {failed} 条，跳过已有 {skipped} 条；"
          f"今日还能抓 {left} 条")
    return {"saved": saved, "failed": failed, "skipped": skipped, "left": left}
