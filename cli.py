#!/usr/bin/env python3
"""职位统计工具 CLI。

用法示例：
  python cli.py serve                        # 启动本地筛选台（采集+筛选都在页面里，推荐）
  python cli.py login boss                   # 打开浏览器登录 Boss直聘（保存登录态）
  python cli.py collect --platform boss --query "python" --city 深圳 --pages 5
  python cli.py collect --platform job51 --query "python" --city 全国 --pages 3
  python cli.py collect --platform zhitong --query "python" --city 东莞
  python cli.py report                       # 生成 HTML 统计报告
  python cli.py browse                       # 生成可离线双击打开的静态筛选台
  python cli.py report --demo                # 用演示数据看报告样式
  python cli.py export                       # 导出 CSV
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobstats import db, report, server
from jobstats.collectors import boss, job51, zhitong

COLLECTORS = {"boss": boss, "job51": job51, "zhitong": zhitong}


def cmd_login(args):
    if args.platform not in ("boss",):
        print("目前只有 Boss直聘 需要登录（51job / 智通的搜索列表无需登录）。")
        if args.platform in COLLECTORS:
            COLLECTORS[args.platform].login()
        return
    boss.login()


def cmd_collect(args):
    db.init_db()
    mod = COLLECTORS[args.platform]
    records = mod.collect(query=args.query, city=args.city, max_pages=args.pages)
    if not records:
        print("未采集到数据（可能触发风控/无结果），今日先到此为止。")
        return
    added = db.save_jobs(records)
    print(f"入库完成：新增 {added} 条（重复跳过 {len(records) - added} 条）。")
    print("提示：可运行 `python cli.py report` 生成统计报告。")


def cmd_report(args):
    out = report.generate(demo=args.demo)
    print(f"报告已生成：{out}")


def cmd_browse(args):
    out = report.generate_browser(demo=args.demo)
    print(f"筛选台已生成：{out}")
    print("双击该 HTML 文件即可使用（所有筛选在本地实时生效）。")


def cmd_export(args):
    out = Path(args.out or "data/export.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    db.export_csv(out)
    print(f"已导出 CSV：{out}")


def cmd_serve(args):
    server.serve(port=args.port, open_browser=not args.no_browser)


def main():
    ap = argparse.ArgumentParser(description="多平台职位统计工具（合规限速版）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="平台登录（目前仅 boss 需要）")
    p.add_argument("platform", choices=["boss", "job51", "zhitong"])
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("collect", help="采集职位数据")
    p.add_argument("--platform", required=True, choices=list(COLLECTORS))
    p.add_argument("--query", required=True, help="搜索关键词，如 python")
    p.add_argument("--city", default="全国", help="城市名（boss/job51/zhitong 支持的城市见模块内 CITY_CODES）")
    p.add_argument("--pages", type=int, default=5, help="本次最多翻几页（默认 5）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("report", help="生成 HTML 统计报告")
    p.add_argument("--demo", action="store_true", help="用演示数据生成报告")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("browse", help="生成交互式职位筛选台（HTML）")
    p.add_argument("--demo", action="store_true", help="用演示数据生成")
    p.set_defaults(func=cmd_browse)

    p = sub.add_parser("export", help="导出 CSV")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("serve", help="启动本地筛选台（页面里可直接采集/刷新，推荐）")
    p.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
