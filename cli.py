#!/usr/bin/env python3
"""职位统计工具 CLI。

用法示例：
  python cli.py serve                        # 启动本地筛选台（采集+筛选都在页面里，推荐）
  python cli.py serve --host 0.0.0.0         # 让 Docker 里的 n8n 也能连（配合 JOBSTATS_TOKEN）
  python cli.py login boss                   # 打开浏览器登录 Boss直聘（保存登录态）
  python cli.py login boss --edge            # 用本机真实 Edge 登录（平台拦截自动化浏览器时用）
  python cli.py status                       # 体检：登录态还剩几天 + 今日额度 + 投出去几条、回了几条
  python cli.py digest                       # 纯文本摘要（今日新增/待补/投递漏斗/匹配度 Top），给邮件当正文
  python cli.py collect --platform boss --query "python" --city 深圳 --pages 5
  python cli.py collect --platform job51 --query "python" --city 全国 --pages 3
  python cli.py collect --platform zhitong --query "python" --city 东莞
  python cli.py runs                              # 最近几次采集用的什么词、剔掉多少、被拦的是谁（留痕）
  python cli.py detail --ids 562,560            # 只给要投的那几条抓正文（JD）
  python cli.py detail --platform zhitong --limit 5   # 挑缺正文的职位补抓
  python cli.py analyze                         # 本机大模型算匹配度（LM Studio，不出这台机器）
  python cli.py analyze --limit 5 --model qwen3.5-9b
  python cli.py analyze --dry-run               # 只看送出去的提示词，不调模型
  python cli.py report                       # 生成 HTML 统计报告
  python cli.py browse                       # 生成可离线双击打开的静态筛选台
  python cli.py report --demo                # 用演示数据看报告样式
  python cli.py export                       # 导出 CSV
"""
import argparse
import socket
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobstats import analyze, browser, db, details, relevance, report, server
from jobstats.collectors import COLLECTORS, collect_and_save
from jobstats.config import (ANALYSIS_PROFILE_PATH, ANALYZE_BATCH_DEFAULT, DAILY_CAP,
                             DETAIL_DAILY_CAP, LLM_BASE_URL, LLM_MODEL)


def cmd_login(args):
    mod = COLLECTORS[args.platform]
    if args.edge:
        fn = getattr(mod, "login_edge", None)
        if fn is None:
            print(f"{args.platform} 没有 Edge 登录通道（该平台无需登录）。")
            return
        fn()
        return
    mod.login()


def _llm_listening(url: str, timeout: float = 0.6) -> tuple[bool, str]:
    """探本机那个端口有没有人听：只做 TCP connect，不发请求体、不出这台机器。"""
    p = urlparse(url)
    host, port = p.hostname or "127.0.0.1", p.port or 1234
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port}"
    except OSError:
        return False, f"{host}:{port}"


def _detail_backlog():
    """待补正文的总数，以及其中"现在补不了"的部分：{平台: (条数, 原因)}。

    总数里混着登录态已过期平台的行时，"补完要 N 天"就是句假话——那批行在重新扫码
    之前一条都抓不到。拆开说，天数只按现在真能补的算。
    """
    miss = db.jobs_missing_details(limit=10000, exclude_platforms=tuple(details.NO_DETAIL))
    blocked = {}
    for p in sorted({r["platform"] for r in miss}):
        reason = details.block_reason(p)
        if reason:
            blocked[p] = (sum(1 for r in miss if r["platform"] == p), reason)
    return len(miss), blocked


def _analysis_check():
    """匹配度那条链的两个前置：本机自述文件 + LM Studio 端口。

    2026-10-15 查过一次：库里 job_analysis 一直是 0 行，原因不是模型算错，
    是这两步都没开始——`analyze` 报的错又在最后一步，看不出卡在哪。
    """
    ok_profile = ANALYSIS_PROFILE_PATH.exists()
    if ok_profile:
        # 光看文件在不在、有多少字节不够：模板 987 字节、8 个【】一个没填，
        # 说它"已就绪"会让人以为 analyze 失败是模型的锅，其实是尺子还没刻度。
        try:
            ph = analyze.placeholders(ANALYSIS_PROFILE_PATH.read_text(
                encoding="utf-8", errors="replace"))
        except OSError:
            ph = []
        if ph:
            hint = (f"还有 {len(ph)} 处【】没填（如 {ph[0]}）——"
                    "填掉这些才能算匹配度，analyze 会拒绝带着占位跑")
            ok_profile = False
        elif ANALYSIS_PROFILE_PATH.stat().st_size < 40:
            hint = f"存在但只有 {ANALYSIS_PROFILE_PATH.stat().st_size} 字节，基本是空的"
            ok_profile = False
        else:
            hint = "已就绪"
    else:
        hint = f"没有写（{ANALYSIS_PROFILE_PATH}）"
    print(f"分析前置：本机自述文件 {hint}。")
    listening, where = _llm_listening(LLM_BASE_URL)
    print(f"          LM Studio 端口 {where} "
          f"{'有人在听' if listening else '没人听：模型没起来，analyze 会直接失败'}。")
    if not ok_profile or not listening:
        print("          这两样齐了才能算匹配度；筛选台里那一列在没跑过分析前一直是空的。")
    missing_detail, blocked = _detail_backlog()
    reachable = missing_detail - sum(n for n, _ in blocked.values())
    pending = len(db.pending_analysis(limit=10000))
    print(f"待补正文 {missing_detail} 条（每日额度 {DETAIL_DAILY_CAP} 条），待分析 {pending} 条。")
    if details.NO_DETAIL:
        print(f"  这个数已排除不能按条补正文的平台（{'、'.join(sorted(details.NO_DETAIL))}），"
              "那些行不算待补。")
    for p, (n, reason) in blocked.items():
        print(f"  其中 {report.PLATFORM_NAMES.get(p, p)} {n} 条现在补不了：{reason}——"
              "重新扫码之前别拿它们估天数。")
    if reachable > DETAIL_DAILY_CAP:
        days = -(-reachable // DETAIL_DAILY_CAP)
        # "现在能补的"只在真有平台被拦的时候才有意义，否则等于把同一个数说两遍
        qual = f"现在能补的 {reachable} 条" if blocked else f"这 {reachable} 条"
        print(f"  正文是分析的瓶颈：按每天 {DETAIL_DAILY_CAP} 条算，"
              f"补完{qual}要 {days} 天。"
              "别等全补完，用 `detail --ids 行号` 先给要投的那几条补。")


def _funnel_check() -> None:
    """投递漏斗：产出侧和输入侧放在同一屏上。

    前面那些行数的全是"进"（额度、待补正文、待分析），而"投出去几条、回了几条"
    原来这一屏上一个字都没有：队列永远看得见，转化从来没被打印过。2026-10-26 第一次
    把它数出来，40 天采的 444 条里 1 条"想投"、0 条"已投"。
    """
    f = db.status_funnel()
    parts = " / ".join(f"{s} {f['labels'].get(s, 0)}" for s in db.STATUS_LABELS[1:])
    print(f"投递：{parts}，未标注 {f['unmarked']} 条（库里共 {f['total'] + f['unmarked']} 条）。")
    if f["applied"]:
        print(f"  投出去 {f['applied']} 条，有回音 {f['replied']} 条 = {f['rate']:.0f}%"
              "（按最后一次改状态的时刻算，不是按投递那一刻）。")
        w = db.status_funnel(days=7)
        print(f"  近 7 天：投出 {w['applied']} 条、有回音 {w['replied']} 条。")
    else:
        print("  一条都没标「已投」及以上，回复率现在算不出来——这不是 0%，是分母还没出现。")
    if f["undated"]:
        print(f"  另有 {f['undated']} 条标过状态但没记时刻"
              "（那是加这一列之前标的），按天的窗口数不到它。")


def cmd_status(args):
    """体检：登录态还剩几天、今日额度用了多少、投出去的有几分回了。不联网，也不打印任何 cookie 值。"""
    names = [args.platform] if args.platform else list(COLLECTORS)
    for name in names:
        required = getattr(COLLECTORS[name], "LOGIN_COOKIES", None)
        print(f"[{name}]")
        if not required:
            print("  该平台不需要登录，没有登录态要管。")
            continue
        for line in browser.login_state_report(name, set(required)):
            print("  " + line)
    db.init_db()
    print(f"额度：今日已入库 {db.count_today()}/{DAILY_CAP} 条，"
          f"正文 {db.count_details_today()}/{DETAIL_DAILY_CAP} 条。")
    _analysis_check()
    _funnel_check()


def cmd_digest(args):
    """纯文本摘要，一行一件事，给邮件/影刀/n8n 当正文用。

    为什么不直接让它们读 `status` 或数据库：`status` 那些是给人在终端看的（中文长句 +
    缩进 + 换行提示），外部程序想拼一封邮件就得去 parse 它，还得先猜对编码；
    而 SQLite 那个文件只有一个写者，多开一个读连接就多一个 `database is locked` 的来源。
    这条命令走的是页面同一份 `fetch_all()`，所以摘要里的数和你屏幕上看到的必然一致。
    """
    days = max(1, min(int(args.days), 30))
    db.init_db()   # 和 cmd_status 一样先建表：老库没 job_details 时下面那句查询会炸 OperationalError
    rows = db.fetch_all()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    plat = report.PLATFORM_NAMES

    by_day: dict[str, int] = {}
    by_day_plat: dict[str, dict[str, int]] = {}
    for r in rows:
        day = (r.get("fetched_at") or "")[:10]
        by_day[day] = by_day.get(day, 0) + 1
        per = by_day_plat.setdefault(day, {})
        per[r["platform"]] = per.get(r["platform"], 0) + 1

    print(f"职位摘要 {now.strftime('%Y-%m-%d %H:%M')}")
    if not rows:
        print("库里 0 条：还没采过。先跑 python cli.py collect --platform zhitong "
              "--query IT运维 --city 东莞")
        return
    total_plat = Counter(r["platform"] for r in rows)
    print("库里共 {} 条：{}".format(
        len(rows), " / ".join(f"{plat.get(p, p)} {n}"
                              for p, n in total_plat.most_common())))
    print(f"今日新增 {by_day.get(today, 0)} 条（额度 {db.count_today()}/{DAILY_CAP}）")

    span = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)][::-1]
    print("近 {} 天新增：{}".format(
        days, " / ".join(f"{d[5:]} {by_day.get(d, 0)}" for d in span)))
    new_today = by_day_plat.get(today) or {}
    if new_today:
        print("今日分平台：" + " / ".join(
            f"{plat.get(p, p)} {n}" for p, n in
            sorted(new_today.items(), key=lambda kv: -kv[1])))

    missing_detail, blocked = _detail_backlog()
    reachable = missing_detail - sum(n for n, _ in blocked.values())
    pending = len(db.pending_analysis(limit=10000))
    line = f"待补正文 {missing_detail} 条（每天 {DETAIL_DAILY_CAP} 条额度）"
    for p, (n, reason) in blocked.items():
        line += f"，{report.PLATFORM_NAMES.get(p, p)} {n} 条卡在{reason}"
    if reachable > DETAIL_DAILY_CAP:
        # 没有平台被拦时"能补的"就是同一个数，说两遍只会让人以为还有别的东西卡着
        line += (f"，能补的 {reachable} 条约 {-(-reachable // DETAIL_DAILY_CAP)} 天"
                 if blocked else f"，约 {-(-reachable // DETAIL_DAILY_CAP)} 天补完")
    print(f"{line}，待分析 {pending} 条。")

    # 放在匹配度之前：匹配度那一段一条分析都没有时会自己 return，
    # 而"投出去几条"跟模型跑没跑没关系，不能跟着它一起被跳过。
    f = db.status_funnel()
    print("投递漏斗：" + " / ".join(f"{s}{f['labels'].get(s, 0)}"
                                    for s in db.STATUS_LABELS[1:])
          + f"，未标注 {f['unmarked']}"
          + (f"；投出 {f['applied']} 条、有回音 {f['replied']} 条 = {f['rate']:.0f}%"
             if f["applied"] else "；投出 0 条，回复率没有分母"))

    scored = sorted((r for r in rows if r.get("score") is not None),
                    key=lambda r: -r["score"])
    if not scored:
        print("匹配度：还没跑过分析——把 data/profile.txt 里的【】填完、"
              "把 LM Studio 的端口起起来，再跑 python cli.py analyze")
        return
    print(f"匹配度：已分析 {len(scored)} 条，最高 {min(3, len(scored))} 条：")
    for r in scored[:3]:
        # `:g` 是给 88.0 这种用的：库里存一位小数，邮件里写「88分」就够，别拖个 .0
        print(f"  {r['score']:g}分 {r['title']} · {r['company']}"
              f"（{plat.get(r['platform'], r['platform'])}）")


def _split_values(raw: str) -> list[str]:
    return [v.strip() for v in raw.replace(",", " ").split() if v.strip()]


# --drop 用中文类型，库里的 kind 是英文，这里两种都认
_RULE_KINDS = {"公司": "company", "关键词": "keyword",
               "company": "company", "keyword": "keyword"}
_RULE_LABEL = {"company": "公司", "keyword": "关键词"}


def cmd_blocked(args):
    """屏蔽名单：在页面上删过的职位会进这张名单，以后每次采集都跳过它们。"""
    db.init_db()
    if args.company or args.keyword:
        for kind, raw in (("company", args.company), ("keyword", args.keyword)):
            for v in _split_values(raw or ""):
                added = db.add_block_rule(kind, v)
                print(f"  {'已加上' if added else '本来就有'}屏蔽规则："
                      f"{_RULE_LABEL[kind]}「{v}」")
        print("规则在入库那一步生效：以后每次采集，命中它的职位直接不进库。")
        print("（已经躺在库里的那些不会被规则清掉——那要在页面上删，或者"
              "`export`/report 里把它们筛出来。）")
        return
    if args.drop:
        for spec in _split_values(args.drop):
            kind_name, _, value = spec.partition(":")
            kind = _RULE_KINDS.get(kind_name.strip())
            if not kind or not value.strip():
                print(f"格式得是 公司:某某公司 或 关键词:仓库，这几个看不懂：{spec}")
                continue
            ok = db.remove_block_rule(kind, value)
            print(f"  {'已放掉' if ok else '名单里本来没有'}规则：{_RULE_LABEL[kind]}「{value}」")
        print(f"剩下的规则：{len(db.block_rule_list())} 条。")
        return
    if args.clear:
        keys = [k.strip() for k in args.clear.replace(",", " ").split() if k.strip()]
        bad = [k for k in keys if ":" not in k]
        if bad:
            print(f"格式得是 平台:职位编号，这几个看不懂：{'、'.join(bad)}"
                  "（整平台放回去用 boss:*）")
            return
        n = db.unblock_jobs(keys)
        print(f"已从屏蔽名单放掉 {n} 条（名单里剩下的条目标的是同一个职位编号）。")
        print(f"名单里还剩 {len(db.blocked_list())} 条。放掉的职位下次采集会正常入库，"
              "要立刻拿回来就再采一次那个关键词。")
        return
    rules = db.block_rule_list()
    if rules:
        print(f"屏蔽规则 {len(rules)} 条（在入库那一步就拦下整一类）：")
        for r in rules:
            print(f"  {_RULE_LABEL.get(r['kind'], r['kind'])}「{r['value']}」  加于 {r['created_at']}")
        print("  放掉某条：python cli.py blocked --drop 关键词:仓库")
    rows = db.blocked_list()
    if not rows:
        print("屏蔽名单（单条职位）是空的。在页面上删掉职位之后，它们才会进这张名单——"
              "不进名单的话，第二天定时跑同一个关键词会把这些噪音原样采回来。")
        if not rules:
            print("想一次挡掉一整类：python cli.py blocked --keyword 仓库 或 --company 公司名")
        return
    print(f"屏蔽名单 {len(rows)} 条（采集时跳过；想放回去：python cli.py blocked --clear 平台:编号）：")
    for r in rows[:50]:
        print(f"  {r['platform']}:{r['platform_job_id']}  {(r['title'] or '—')[:28]:<28}"
              f"  {(r['company'] or '')[:20]:<20}  删于 {r['blocked_at']}")
    if len(rows) > 50:
        print(f"  …另外 {len(rows) - 50} 条没列出来。")


def cmd_collect(args):
    db.init_db()
    mod = COLLECTORS[args.platform]
    records, st = collect_and_save(mod, args.query, args.city, args.pages)
    if not records:
        print("未采集到数据：判据看上面最后一条提示（结构变了 / 无结果 / 风控 / 登录态失效），"
              "别一律当风控。一条也没入库就不占今日额度，可以换关键词再试。")
        return
    print(f"入库完成：新入库 {st['added']} 条，刷新 {st['refreshed']} 条"
          "（同一个职位再采一遍会更新薪资这些字段，不动你标的投递状态）。")
    if st["blocked"]:
        print(f"  另有 {st['blocked']} 条被屏蔽名单拦住（你在页面上删过它们）："
              "`python cli.py blocked` 看名单，`blocked --clear 平台:编号` 放回去。")
    if st.get("rules"):
        print(f"  另有 {st['rules']} 条被屏蔽规则拦掉（公司/关键词）："
              "`python cli.py blocked` 看规则，`blocked --drop 关键词:仓库` 放回去。")
    if st.get("dropped"):
        print(f"  另有 {st['dropped']} 条没过相关性闸门（城市不符 / 短词只是被别的单词带着），"
              "上面已按原因列出条数。")
    print("提示：可运行 `python cli.py report` 生成统计报告。")


def cmd_runs(args):
    """采集留痕：最近几次采集用了什么关键词、几个城市、剔掉的是被谁拦的。"""
    db.init_db()
    rows = db.recent_collect_runs(limit=args.limit)
    if not rows:
        print("还没有采集留痕（这张表是 2026-10-15 加的，之前那几次查不回来）。")
        return
    print(f"最近 {len(rows)} 次采集（新的在前）：")
    for r in rows:
        line = (f"  {r['started_at']}  {(r['platform'] or '?'):<8} 「{r['query']}」@ {r['city']}"
                f"  最多{r['pages']}页  采到{r['collected']}  新增{r['added']}"
                f"  刷新{r['refreshed']}")
        if r["dropped"]:
            line += f"  闸门剔{r['dropped']}"
        if r["blocked"]:
            line += f"  屏蔽拦{r['blocked']}"
        if r.get("rules"):
            line += f"  规则拦{r['rules']}"
        line += f"  耗时{r['seconds']}s"
        print(line)
        if r["drop_reasons"]:
            print("      剔掉原因：" + "、".join(f"{k} {v} 条"
                                     for k, v in r["drop_reasons"].items()))
        if r.get("drop_titles"):
            print("      被拦标题：" + relevance.titles(r["drop_titles"], limit=8))
        if r["error"]:
            print(f"      失败：{r['error']}")


def cmd_report(args):
    db.init_db()
    out = report.generate(demo=args.demo)
    print(f"报告已生成：{out}")


def cmd_browse(args):
    db.init_db()
    out = report.generate_browser(demo=args.demo)
    print(f"筛选台已生成：{out}")
    print("双击该 HTML 文件即可使用（所有筛选在本地实时生效）。")


def cmd_export(args):
    db.init_db()
    out = Path(args.out or "data/export.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    db.export_csv(out)
    print(f"已导出 CSV：{out}")


def cmd_detail(args):
    db.init_db()
    if args.platform in details.NO_DETAIL:
        print(f"{args.platform}：{details.NO_DETAIL[args.platform]}")
        return
    if args.ids is not None:
        # 判据是"给没给这个参数"，不是"参数里有没有东西"：写成 if args.ids 的话
        # `--ids ""` 会被当成没给，直接掉进下面的自动补抓，一天 20 条正文额度静默花光。
        try:
            ids = [int(x) for x in args.ids.replace(",", " ").split()]
        except ValueError:
            raise RuntimeError(f"--ids 里有不是行号的东西：{args.ids!r}。"
                               "要的是库里的行号，如 --ids 562,560")
        if not ids:
            raise RuntimeError("--ids 是空的。要么给行号，要么把这个参数整个去掉，再按每日额度补抓。")
        jobs = db.jobs_by_ids(ids)
        if not jobs:
            # 这句话不能落到下面那句"都已抓过或今日额度用完"上：那是另一个原因
            print(f"这些行号在库里都找不到：{args.ids}，没抓任何正文。")
            return
        if len(jobs) < len(ids):
            print(f"有 {len(ids) - len(jobs)} 个行号在库里找不到，已忽略。")
    else:
        jobs = db.jobs_missing_details(platform=args.platform,
                                       limit=args.limit or DETAIL_DAILY_CAP,
                                       exclude_platforms=tuple(details.NO_DETAIL))
    blocked = {p: r for p in sorted({j["platform"] for j in jobs})
               if (r := details.block_reason(p))}
    if blocked:
        rest = [j for j in jobs if j["platform"] not in blocked]
        for p, reason in blocked.items():
            n = sum(1 for j in jobs if j["platform"] == p)
            tail = "其余平台照常补。" if rest else "重新扫码之后再跑这条命令。"
            print(f"{report.PLATFORM_NAMES.get(p, p)} 的 {n} 条这次不抓：{reason}。{tail}")
        jobs = rest
    if not jobs:
        if blocked:
            print("这一批里没有现在能抓正文的职位，一条请求都没发。")
        else:
            print("没有需要抓正文的职位（都已经有正文了，或库里没有带详情页链接的职位）。")
        return
    details.collect_details(jobs, limit=args.limit, force=args.force)


def cmd_analyze(args):
    db.init_db()
    analyze.run(limit=args.limit, platform=args.platform, redo=args.redo,
                url=args.url, model=args.model, profile_path=args.profile,
                dry_run=args.dry_run)


def cmd_serve(args):
    server.serve(port=args.port, open_browser=not args.no_browser, host=args.host)


def _force_utf8_output():
    """输出一律走 UTF-8。

    Python 的 stdout 只有在连着终端时才用 Unicode API；一旦重定向进文件或管道，就退回
    机器代码页（这台是 cp936）。登录巡检那条链读的就是重定向出来的文件，职位标题又是
    抓来的原文，只要混进一个 🔥 就 UnicodeEncodeError 把整条命令打断——摘要缺半截，
    比缺内容更糟，因为外面看到的是一份"跑成功了"的空文件。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # 被换掉的 stdout（比如测试里重定向的 StringIO）就算了，不影响正确性


def main():
    _force_utf8_output()
    ap = argparse.ArgumentParser(description="多平台职位统计工具（合规限速版）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="平台登录（目前仅 boss 需要）")
    p.add_argument("platform", choices=list(COLLECTORS))
    p.add_argument("--edge", action="store_true",
                   help="用本机真实 Edge 登录（Boss直聘 拦截自动化浏览器时走这条）")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("status",
                       help="体检：登录态还剩几天、今日额度用了多少、投出去的有几分回了（不联网，不打印 cookie 值）")
    p.add_argument("platform", nargs="?", default=None, choices=list(COLLECTORS),
                   help="只看这一个平台，不给就三家都看")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("digest",
                       help="纯文本摘要（今日新增/待补正文/投递漏斗/匹配度 Top），给邮件和外部调度当正文")
    p.add_argument("--days", type=int, default=7, help="近几天新增（默认 7，最多 30）")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("blocked",
                       help="屏蔽名单：页面上删过的职位，采集时跳过它们（不给参数就是列名单）")
    p.add_argument("--clear", default=None, metavar="平台:编号[ 平台:编号…]",
                   help="把若干条从名单里放回去；整平台用 boss:*，空格或逗号分隔")
    p.add_argument("--company", default=None, metavar="公司名[ 公司名…]",
                   help="加屏蔽规则：这家公司以后采到的职位一律不进库（空格或逗号分隔多个）")
    p.add_argument("--keyword", default=None, metavar="标题词[ 标题词…]",
                   help="加屏蔽规则：标题含这个词的一律不进库，如 仓库/普工/打印机维修")
    p.add_argument("--drop", default=None, metavar="类型:值[ 类型:值…]",
                   help="放掉一条规则，类型是 公司 或 关键词，如 关键词:仓库")
    p.set_defaults(func=cmd_blocked)

    p = sub.add_parser("collect", help="采集职位数据")
    p.add_argument("--platform", required=True, choices=list(COLLECTORS))
    p.add_argument("--query", required=True,
                   help="搜索关键词，用长词（python开发/后端工程师）；IT 这种 2~3 字母短词"
                        "平台按子串命中，闸门会剔掉 TITOK运营 这类垃圾")
    p.add_argument("--city", default="全国", help="城市名（boss/job51 用模块内 CITY_CODES 的城市，zhitong 直接传中文城市名）；传了城市就按城市拦，平台推荐的邻近城市不入库")
    p.add_argument("--pages", type=int, default=5, help="本次最多翻几页（默认 5）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("runs",
                       help="采集留痕：最近几次采集用的关键词/城市/页数，以及各剔掉多少")
    p.add_argument("--limit", type=int, default=20, help="看最近几次（默认 20）")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("report", help="生成 HTML 统计报告")
    p.add_argument("--demo", action="store_true", help="用演示数据生成报告")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("browse", help="生成交互式职位筛选台（HTML）")
    p.add_argument("--demo", action="store_true", help="用演示数据生成")
    p.set_defaults(func=cmd_browse)

    p = sub.add_parser("export", help="导出 CSV")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("detail", help="抓职位正文（JD）：逐条打开详情页，每日额度更小")
    p.add_argument("--ids", default=None,
                   help="只抓这几条（库里 id，逗号分隔）；不给就自动挑还没正文的职位")
    p.add_argument("--platform", default=None, choices=list(COLLECTORS),
                   help="只抓这个平台的（前程无忧详情页要滑块验证，按红线不抓）")
    p.add_argument("--limit", type=int, default=None,
                   help=f"本次最多抓几条（默认不超过每日额度 {DETAIL_DAILY_CAP} 条）")
    p.add_argument("--force", action="store_true", help="已有正文的也重抓覆盖")
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("analyze",
                       help="本机大模型算职位匹配度：只连 LM Studio，不访问任何招聘平台")
    p.add_argument("--limit", type=int, default=ANALYZE_BATCH_DEFAULT,
                   help=f"本次最多算几条（默认 {ANALYZE_BATCH_DEFAULT}，本地推理慢，别一次塞太多）")
    p.add_argument("--platform", default=None, choices=list(COLLECTORS),
                   help="只算这个平台的职位")
    p.add_argument("--model", default=LLM_MODEL,
                   help="LM Studio 里的模型 id；不给就自动用它当前加载的那个")
    p.add_argument("--url", default=LLM_BASE_URL,
                   help="OpenAI 兼容接口地址，默认本机 LM Studio")
    p.add_argument("--profile", default=None,
                   help="自述文件路径，默认 data/profile.txt（匹配度的参照）")
    p.add_argument("--redo", action="store_true", help="已经算过的也重算（改完提示词用）")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="只打印实际送出去的提示词，不调模型、不写库")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("serve", help="启动本地筛选台（页面里可直接采集/刷新，推荐）")
    p.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p.add_argument("--host", default=server.HOST,
                   help="监听地址，默认只监听 127.0.0.1。"
                        "Docker 里的 n8n 要连本机时填 0.0.0.0，务必同时设 JOBSTATS_TOKEN")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n[中止] 已手动取消。", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        # 风控/额度/超时属可预期失败，给结论即可，不必甩堆栈
        print(f"\n[中止] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
