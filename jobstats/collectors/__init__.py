"""各平台采集器的注册表和公共调度。"""
from datetime import datetime

from .. import browser, db, relevance
from . import boss, job51, zhitong  # noqa: F401

COLLECTORS = {"boss": boss, "job51": job51, "zhitong": zhitong}


def _platform_name(mod) -> str:
    """留痕里写的平台名：按注册表认，认不出（测试里的假通道）就留空。"""
    for name, m in COLLECTORS.items():
        if m is mod:
            return name
    return ""


def collect_and_save(mod, query: str, city: str, pages: int):
    """跑一次采集并入库，返回 (采到的记录, {"added", "refreshed", "blocked", "rules", "dropped"})。

    带 `PER_PAGE_SAVE` 的通道每采完一页就存一次：整轮跑完才写库的话，第 4 页崩了
    前面 3 页白采，晚上跑完第二天什么都查不到。Boss 那条通道没这个标记（那个文件
    不在改动范围内），照旧整轮返回后存一次。

    相关性闸门也是同样的道理：自己声明 `SELF_GATES` 的通道在采集器里已经过了一遍
    （剔掉的原因各不相同，由它们自己打印），剩下的通道在这里兜底过。不兜这一层，
    Boss 那条通道就是唯一一条搜索页原样入库的——2026-10-13 实测 18 条里混进
    仓库管理员、打印机维修这类 5 条，就是没人拦的后果。
    判方向要不要用平台给的职类，看通道上的 `FAMILY_GATE`：智通给的是「一级行业-二级职类」
    （第二段是职能），Boss 给的是公司行业，拿后者判方向会把"园区弱电IT运维工程师"（标环保）
    误杀，所以只有声明了这个标记的通道才判。

    不管走哪条路径，最后都给 `collect_runs` 留一行：关键词、城市、页数、各类条数、
    闸门剔掉的原因和被拦的标题。命令行那条路径没有页面那样的任务日志，全靠这一行事后查
    ——闸门是在入库之前判的，库里没有痕迹，误杀只能从这里看。
    """
    stats = {"added": 0, "refreshed": 0, "blocked": 0, "rules": 0, "dropped": 0}
    detail: dict[str, list[str]] = {}     # 原因 -> 被拦标题
    db.init_db()        # 每页一存意味着库里得先有表（屏蔽名单第一次就用到）
    self_gates = bool(getattr(mod, "SELF_GATES", False))
    family = bool(getattr(mod, "FAMILY_GATE", False))
    started_at = datetime.now()

    name = _platform_name(mod)
    # 认不出来的通道（测试里的假模块）不去摸它的登录判据；排序是为了留痕里的顺序稳定
    required = tuple(sorted(getattr(mod, "LOGIN_COOKIES", ()) or ())) if name in COLLECTORS else ()
    if name and required:
        # 采集用的是那个 Edge 窗口里的真凭证，快照只是给体检/status 看的缓存。
        # 开工前顺手刷一次，免得窗口明明好用、文件却停在几周前——那正是
        # "采集天天成功、体检说已过期 20.8 天"那个假警报的来源。
        if browser.refresh_snapshot(name, required):
            print(f"  [{name}] 登录快照已按实时通道刷新")

    def _tally(st: dict):
        for k in ("added", "refreshed", "blocked", "rules"):
            stats[k] += st.get(k, 0)

    def hand_off(dropped: dict):
        """自判通道交回来的是全量累计明细，直接换掉本地这份（重复并会翻倍）。"""
        detail.clear()
        relevance.merge(dropped, detail)

    def save(batch):
        if not self_gates:
            kept, dropped = relevance.gate(batch, query, city, family=family)
            n = relevance.count(dropped)
            if n:
                print(f"  相关性闸门另剔掉 {n} 条：{relevance.summarize(dropped)}")
                print(f"  剔掉的：{relevance.titles(dropped)}")
            relevance.merge(dropped, detail)
            batch = kept
        if batch:
            # 列表页顺带拿到的正文（前程无忧的搜索接口自带）：摘出来单独写 job_details。
            # 不能留在记录里：jobs 表白名单会直接拒收这个键。这类正文没有额外发请求，
            # 由 db.save_list_details 标成来源 list，不占 DETAIL_DAILY_CAP 的逐条额度。
            jd = []
            for r in batch:
                desc = (r.pop("_description", "") or "").strip()
                if desc:
                    jd.append({"platform": r["platform"],
                               "platform_job_id": r["platform_job_id"],
                               "description": desc, "keyword_tags": [],
                               "url": r.get("url") or ""})
            if jd:
                db.save_list_details(jd)
            _tally(db.save_stats(batch))

    per_page = getattr(mod, "PER_PAGE_SAVE", False)
    records: list[dict] = []
    error = None
    kwargs = {"on_page": save} if per_page else {}
    if self_gates:
        # 自判通道不经过这里，明细只能由它自己每页交回来（崩了也至少留下上一页的）
        kwargs["on_drop"] = hand_off
    try:
        records = mod.collect(query=query, city=city, max_pages=pages, **kwargs)
        if records and not per_page:
            save(records)
    except Exception as e:
        # first_line：Playwright 的 Call log 里原样带着请求头（bst/zp_at），不能整条存库
        error = browser.first_line(e)
        raise
    finally:
        counts = {k: len(v) for k, v in detail.items()}
        # 自判通道的剔掉数只存在于它交回来的明细里，这里统一按明细算——
        # 公共落库口自己判的那批本来就算得一样，页面上的回执才不会跟采集器打印的对不上。
        stats["dropped"] = relevance.count(detail)
        db.log_collect_run({
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": round((datetime.now() - started_at).total_seconds(), 1),
            "platform": _platform_name(mod),
            "query": query, "city": city, "pages": pages,
            "collected": len(records), **stats,
            "drop_reasons": counts, "drop_titles": detail, "error": error,
        })
    return records, stats
