"""Playwright 会话公共层：浏览器启动、登录态、限速、每日额度。"""
import json
import random
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from . import db
from .config import (AUTH_DIR, CDP_PORT, CDP_PROFILE_DIR, DELAY_RANGE, DAILY_CAP,
                     EDGE_PATHS, REQUEST_TIMEOUT_MS)

# 只为把 CDP 窗口拉起来而打开的落地页：端口存在与否才是通道就绪的依据。
EDGE_HOME_URL = "https://www.zhipin.com/"


def first_line(exc: BaseException) -> str:
    """只取异常第一行。

    Playwright 的报错会附一整段 Call log，里面原样带着请求头——包括 bst/zp_at 登录凭证，
    绝不能跟着 traceback 一起打进终端。
    """
    return (str(exc).splitlines() or [type(exc).__name__])[0]


def _read_cookies(platform: str):
    """登录态文件里的 cookie 列表，返回 (那份列表, 读不出来的原因)；两者不会同时有值。

    原因要一路带到屏幕上：这个文件被别的程序占着（杀毒软件、正在编辑它的编辑器）和
    这份文件真的坏了，是两件不同的事——只说"重新扫一次码"会让人把好的登录态覆盖掉。
    """
    path = AUTH_DIR / f"{platform}_storage.json"
    if not path.exists():
        return None, "还没保存过登录态"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, first_line(e)
    # 解得开但形状不对（不是 {"cookies": [...]}）也归到"没有 cookie"：
    # 判据缺失是能被巡检逮住的实话，把 AttributeError 抛到终端不是。
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return [], None
    return [c for c in cookies if isinstance(c, dict)], None


def _cookie_left(c: dict):
    """这枚 cookie 还剩几天；会话 cookie（expires 缺失/<=0）判不了，返回 None。"""
    exp = c.get("expires")
    if not isinstance(exp, (int, float)) or exp is None or exp <= 0:
        return None
    return (exp - time.time()) / 86400.0


def _judge(cookies: list, required) -> dict:
    """把一份 cookie 列表按登录判据过一遍：缺哪些、哪些判不了有效期、最短还剩几天。

    体检报告和开工闸门必须走同一个判据。两处各写一遍的时候，改一处忘另一处就是
    "屏幕上写着还剩 7 天、点采集却被一句『已过期』拦下"那种事——这一轮真翻过一次车。
    """
    by_name = {str(c.get("name")): c for c in cookies}
    absent, session_only, expired, left = [], [], [], []
    for name in sorted(required):
        c = by_name.get(name)
        if c is None:
            absent.append(name)
            continue
        v = _cookie_left(c)
        if v is None:
            session_only.append(name)
            continue
        if v < 0:
            expired.append(name)
        left.append(v)
    return {"by_name": by_name, "absent": absent, "session_only": session_only,
            "expired": expired, "worst": min(left) if left else None}


def _usable(verdict: dict) -> bool:
    """这份凭证现在能开工：该有的一笔都在，也没有一笔是过期的。"""
    return not verdict["absent"] and not verdict["expired"]


def _describe(verdict: dict, required) -> list[str]:
    """每个登录判据一行。只有名字和天数，值不碰；`判据缺失` 这个词影刀在匹配，别改措辞。"""
    lines = []
    for name in sorted(required):
        if name in verdict["absent"]:
            lines.append(f"  {name}: 没有（登录判据缺失）")
        elif name in verdict["session_only"]:
            lines.append(f"  {name}: 有，但是会话 cookie（关掉浏览器就失效，有效期不判断）")
        else:
            left = _cookie_left(verdict["by_name"][name])
            lines.append(f"  {name}: 已过期 {-left:.1f} 天" if left < 0
                         else f"  {name}: 还剩 {left:.1f} 天")
    return lines


def _conclude(verdict: dict) -> str:
    """一句结论。影刀巡检是在这段话里匹配 `已过期` / `看不出有效期` 的，措辞是接口。"""
    if verdict["absent"]:
        return (f"结论：这份登录态里没有 {'、'.join(verdict['absent'])}——"
                "那次是匿名访问，不算登录成功。重新扫一次码。")
    if verdict["expired"]:
        return (f"结论：{'、'.join(verdict['expired'])} 已经过期。再敲采集命令也没用，"
                "重新扫一次码才是正解（这也是 code=37 最常见的那个来源）。")
    if verdict["session_only"]:
        return (f"结论：凭证都在，但 {'、'.join(verdict['session_only'])} 看不出有效期，"
                "能不能用只有采一次才知道。")
    return "结论：登录凭证都在且没过期。采集还是 0 条的话，看平台返回的 code 再判。"


def login_state_report(platform: str, required=()) -> list[str]:
    """登录态体检：只看 cookie 的名字和有效期，值一律不碰。

    先问实时通道：那个 Edge 窗口开着就读它现在这份（凭证是好的就顺手把快照刷成这一份），
    读不到才退回快照文件——那份文件只是历史，把它的日期当成账号的死活会假拦。
    两条来源都在输出里写明，看到「快照文件」就知道这话不代表"现在还能不能采"。
    """
    path = AUTH_DIR / f"{platform}_storage.json"
    lines = [f"文件：{path}"]
    if not required:
        # 不需要登录的平台别对着它的（永远不存在的）登录态文件瞎操心
        lines.append("该平台不需要登录凭证，到这里就够了。")
        return lines
    required = set(required)
    live = _live_state(platform, required)
    if live is not None:
        verdict, cookies, write_error = live
        if not _usable(verdict):
            lines.append("来源：实时通道（Edge 窗口里现在这份，凭证是坏的，没往快照里写）")
        elif write_error:
            lines.append(f"来源：实时通道（Edge 窗口里现在这份，凭证是好的，但快照文件没写进去"
                         f"：{write_error}——不影响采集，下次体检还会重新问）")
        else:
            lines.append("来源：实时通道（Edge 窗口里现在这份，已顺手刷进快照）")
        lines.append(f"共 {len(cookies)} 个 cookie")
    else:
        cookies, why = _read_cookies(platform)
        if cookies is None:
            if not path.exists():
                lines.append("还没保存过登录态。下一步：双击 login-boss.bat，"
                             "或 python cli.py login boss --edge。")
                # 这一支必须有 `结论` 那一句：影刀分支表第 1 行是"文件里没有结论这句话
                # = 巡检自己没跑成"。少了它，"从没扫过码"（该去扫码）会被读成"巡检坏了"，
                # 而这两种要做的动作不一样。
                lines.append(f"结论：这台机器上还没有 {platform} 的登录态——不是过期，是从没扫过码。")
            else:
                # 这一支故意不写 `结论`：连文件都读不出来就是"判不出来"，
                # 该由影刀分支表第 1 行接住（报"巡检没跑成"），而不是被算成健康。
                lines.append(f"文件读不出来（{why}）：先关掉正在占用它的程序再跑一次；"
                             "确认不是占用问题才重新扫一次码覆盖它。")
            return lines
        saved = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append("来源：快照文件（Edge 窗口没开，读的是上次存的那份，不代表现在）")
        lines.append(f"共 {len(cookies)} 个 cookie，这份存于 {saved}")
        verdict = _judge(cookies, required)
    lines += _describe(verdict, required)
    lines.append(_conclude(verdict))
    return lines


def _live_state(platform: str, required):
    """已经开着的 Edge 通道里的登录凭证：返回 (判定, 那份 cookie 列表, 写快照失败的原因)；
    通道不在或接不上返回 None（=判不了）。第三个位置是 None 表示这一步没问题
    （要么写成了，要么凭证是坏的、本来就不该写），是字符串就是 `storage_state()` 报错的第一行。

    只读 cookie 的名字和有效期，值不落地。凭证是好的时候顺手把快照刷成这一份——
    真身在 Edge 配置目录里，`boss_storage.json` 只是份快照，没人刷新它能停四周
    （那份 09-26 的文件就是这么假报出"凭证已过期 20.8 天"的）。坏的快照比没有更坏，
    所以判据缺失/已过期时一个字都不往文件里写。
    写不成不能默默咽掉：2026-10-25 实测撞上过一回合——屏幕上写着"已顺手刷进快照"，
    文件的 mtime 一动没动。所以成功判据是"文件真的写了"，报出来还要带上为什么没写成
    （只取异常第一行：Playwright 的 Call log 里原样带请求头，bst/zp_at 就在那里面）。
    """
    if not cdp_alive():
        return None
    try:
        with attach() as ctx:
            cookies = ctx.cookies()
            verdict = _judge(cookies, required)
            write_error = None
            if _usable(verdict):
                try:
                    ctx.storage_state(path=str(AUTH_DIR / f"{platform}_storage.json"))
                except Exception as e:
                    write_error = first_line(e)
            return verdict, cookies, write_error
    except Exception:
        # 通道刚被关掉 / attach 失败：判不了，别把环境问题说成账号问题
        return None


def refresh_snapshot(platform: str, required) -> bool:
    """通道开着且实时凭证确实能用，就把快照文件刷成这一份；返回文件是不是真的更新了。

    快照只是缓存，所以三个写入口（login 自动存盘、体检、采集前刷新）都只认
    "凭证确认可用"这一个条件：把坏状态写回文件，等于让明天的体检拿今天的错误当事实。
    凭证好用但文件被挡住（杀毒软件/另一个进程占着）也算没刷成——调用方那句
    "已刷新"不能说，明天体检拿的还是旧那份。
    """
    if not required:
        return False
    live = _live_state(platform, set(required))
    return bool(live) and _usable(live[0]) and live[2] is None


def login_block_reason(platform: str, required=()) -> str | None:
    """这个平台的登录态现在能不能开工；不能就返回一句原因，能就返回 None。

    只认**实时通道**里的凭证：Boss 的采集和正文都从那个 Edge 上下文现取 cookie，
    那份快照文件里写了什么根本不影响请求能不能成。拿文件判死活的结果就是把
    "刚扫过码的人"拦在门外，而拦住的那批一条请求都没发。
    通道没开时返回 None（放行）：`boss.ensure_window()` 会自己把窗口弹回来，
    `collect_details` 在弹窗之后再问一次这个函数——那时候有实测证据，拦才拦得对。
    会话 cookie 判不了有效期，不算过期：那种情况只有真采一次才知道，别提前判死刑。
    """
    if not required:
        return None
    live = _live_state(platform, set(required))
    if live is None:
        return None
    verdict, _, _ = live
    if verdict["absent"]:
        return (f"Edge 窗口里也没有 {'、'.join(verdict['absent'])}（那个浏览器现在没登录）")
    if verdict["expired"]:
        return f"登录凭证已过期 {-verdict['worst']:.1f} 天"
    return None


def polite_sleep():
    d = random.uniform(*DELAY_RANGE)
    print(f"  .. 礼貌等待 {d:.1f}s")
    time.sleep(d)


def check_daily_cap() -> int:
    """超额度则抛错熔断，否则返回今日剩余可采条数。"""
    used = db.count_today()
    if used >= DAILY_CAP:
        raise RuntimeError(
            f"已触发每日额度熔断：今日已采集 {used} 条（上限 {DAILY_CAP}）。"
            "明天再跑，或自行评估后修改 config.py 中的 DAILY_CAP。"
        )
    print(f"  今日已采集 {used}/{DAILY_CAP} 条")
    return DAILY_CAP - used


def launch(headless=False):
    """优先用本机 Chrome（指纹更真实），没有则退回内置 Chromium。"""
    p = sync_playwright().start()
    try:
        try:
            browser = p.chromium.launch(channel="chrome", headless=headless)
        except Exception:
            browser = p.chromium.launch(headless=headless)
    except Exception:
        # 两次都起不来时，这个 driver 子进程已经归我们管了：不 stop 就是每失败一次
        # 留一个孤儿进程，Windows 上反复跑 job51 会越攒越多，最后连浏览器都开不动。
        p.stop()
        raise
    return p, browser


class Session:
    """带登录态的浏览器会话，用完 with 自动关闭。

    save_state=False 时只有显式调用 save() 才落盘——登录失败不该留下半套登录态。
    """

    def __init__(self, platform: str, headless=False, save_state=True):
        self.platform = platform
        self.headless = headless
        self.auto_save = save_state
        self._p = self._browser = self._context = None
        self.storage = None

    def __enter__(self):
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        self.storage = AUTH_DIR / f"{self.platform}_storage.json"
        self._p, self._browser = launch(headless=self.headless)
        kwargs = dict(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        if self.storage.exists():
            kwargs["storage_state"] = str(self.storage)
        self._context = self._browser.new_context(**kwargs)
        self._context.set_default_timeout(REQUEST_TIMEOUT_MS)
        return self._context

    def save(self):
        """把当前 cookie/localStorage 写回登录态文件（登录过程中会更新）。"""
        if self.storage is None:
            return
        try:
            self._context.storage_state(path=str(self.storage))
        except Exception:
            pass

    def __exit__(self, *exc):
        if self.auto_save:
            self.save()
        self._browser.close()
        self._p.stop()


def interactive_login(platform: str, url: str, success_check) -> None:
    """打开有头浏览器让用户手动登录，成功后保存登录态。"""
    print(f"[{platform}] 正在打开浏览器，请在页面中完成登录（扫码/短信均可）...")
    session = Session(platform, headless=False, save_state=False)
    with session as ctx:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        print("等待登录成功（最长 5 分钟）...")
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                here = page.url
            except Exception:
                raise RuntimeError(f"[{platform}] 浏览器窗口被关闭，登录中止。")
            if here == "about:blank":
                # 平台反爬：登录页加载十几秒后会被自己的安全脚本清成空白页
                raise RuntimeError(
                    f"[{platform}] 页面被平台安全检测清空（about:blank），"
                    "自动化浏览器已被拦截，无法在此登录。"
                    "建议改用 job51（前程无忧）或 zhitong（智通直聘）采集。"
                )
            if success_check(page):
                session.save()
                print(f"[{platform}] 登录成功，登录态已保存。")
                time.sleep(2)
                return
            time.sleep(3)
        raise RuntimeError("等待登录超时（5 分钟），请重试。")


# ---- 真实 Edge 通道（CDP 接管）----
# Boss直聘 的反自动化会把 Playwright 自建的浏览器在十几秒后清成 about:blank；
# 用本机真实 Edge 打开页面、事后再接管，实测不会被清空。


def edge_exe() -> str:
    for cand in EDGE_PATHS:
        if Path(cand).exists():
            return cand
    raise RuntimeError("未找到本机 Edge，真实浏览器通道不可用（请改用 job51 / zhitong）。")


def cdp_alive(port: int = CDP_PORT) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
        return True
    except Exception:
        return False


_EDGE_RELAY = ("import subprocess, sys; subprocess.Popen("
               "sys.argv[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
               "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)")


def open_edge(url: str, port: int = CDP_PORT, wait: float = 20.0):
    """以独立配置目录启动真实 Edge，并等调试端口真正可用。

    不能启动完就直接接管：Edge 可能把地址转交给同目录的已有实例后自己退出，
    也可能窗口根本没起来，端口状态只有轮询才准。

    启动时中间隔一个立刻退出的进程，而不是直接 Popen 那个 msedge：真窗口挂在启动它的那条
    命令的父子关系上，命令被按 PID 树清场（停任务、打断命令这一类）会把这个登录窗口一起
    带走。同一把 `taskkill /T /F` 各试一种起法：直接 Popen 的那份下一拍端口就没了、进程全清；
    隔中转的那份杀完之后 12 秒里端口照用。
    代价是拿不到那个进程的状态，坏情况（同配置目录已有实例却不带调试端口）要等满 wait 秒
    才报，不再子进程一退出就 bail。
    """
    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [sys.executable, "-c", _EDGE_RELAY, edge_exe(),
         f"--remote-debugging-port={port}",
         f"--user-data-dir={CDP_PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", url])
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.0)
        if cdp_alive(port):
            return
    raise RuntimeError(
        f"Edge 调试端口 {port} 起不来。请先关掉所有 Edge 窗口，再重新运行 "
        "python cli.py login boss --edge")


@contextmanager
def attach(port: int = CDP_PORT, relaunch=None):
    """接管已运行的真实 Edge，交出 BrowserContext；退出时只断开，不关用户的窗口。

    端口不在多半只是那个窗口被随手关掉了（不是风控，也不是登录失效），
    登录态存在专用配置目录里，所以先自己把窗口弹回来一次，别把用户推向 login 命令。
    """
    if not cdp_alive(port):
        try:
            (relaunch or (lambda: open_edge(EDGE_HOME_URL, port)))()
        except Exception as e:
            raise RuntimeError(f"Edge 窗口自动恢复失败：{first_line(e)}") from e
        if not cdp_alive(port):
            raise RuntimeError(
                "Edge 窗口自动恢复后端口仍不可用。请运行 python cli.py login boss --edge "
                "重新打开那个窗口（登录态还在，一般不用重新扫码）。")
        print("[boss] Edge 窗口已自动重新打开，继续。")
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        yield browser.contexts[0]
    finally:
        p.stop()


def _login_snapshot(ctx, platform: str, login_cookies: set[str]) -> tuple[set[str], set[str]]:
    """看一眼通道里的 cookie 名字，返回 (拿到的判据, 还缺的判据)。

    只有判据凑齐才落盘：半套快照写进文件后，`status` 和体检报告读到的就是它，
    报出来照样像"登录过"，其实是采集一跑就失败的坏状态——比不存更容易骗人。
    """
    have = {str(c.get("name")) for c in ctx.cookies()} & set(login_cookies)
    missing = set(login_cookies) - have
    if not missing and have:
        ctx.storage_state(path=str(AUTH_DIR / f"{platform}_storage.json"))
    return have, missing


def _saved_line(have: set[str], missing: set[str], path) -> str:
    """凑齐时说清存到了哪；没凑齐就直说缺哪笔、这一份没存。"""
    if not missing:
        return f"登录成功（拿到 {', '.join(sorted(have))}，已保存到 {path}）"
    return (f"还缺 {', '.join(sorted(missing))}——采集判据要这几笔都在，这一份没存。"
            f"现在能看到的是 {', '.join(sorted(have)) or '没有'}。"
            "在窗口里刷新一次页面、再等几秒，然后回这个窗口按回车复查。")


def _login_manual_check(platform: str, login_cookies: set[str], path) -> None:
    """手动回车检查（自动轮询等超时或通道断了才走这里）。"""
    while True:
        input("          按回车检查一次登录状态（不想再检查按 Ctrl+C 退出）... ")
        try:
            with attach() as ctx:
                have, missing = _login_snapshot(ctx, platform, login_cookies)
        except RuntimeError:
            # 这里的 attach 失败几乎总是那个窗口中途没了，不是"还没登录"
            raise RuntimeError(
                "刚才那个 Edge 窗口已经不在了（被关掉，或被别的 Edge 实例接管）。"
                "请先关掉所有 Edge 窗口，再重新运行本命令，登录完别再关那个窗口。")
        if not missing:
            print(f"[{platform}] " + _saved_line(have, missing, path))
            return
        if have:
            print(f"[{platform}] " + _saved_line(have, missing, path))
        else:
            print("          还没拿到登录凭证。请在 Edge 窗口里完成扫码/验证，再按一次回车。")


def login_with_edge(platform: str, url: str, login_cookies: set[str],
                    wait: float = 300.0, poll: float = 3.0) -> None:
    """在真实 Edge 里登录：判据一凑齐就自动存盘，不用回黑窗口按回车。

    原来只有回车那一次检查会落盘，于是"登录成功却没存下快照"成了常态空档：回车按早了、
    或者扫完码顺手关了那个窗口，凭证在浏览器里照样好用，快照却能停四周——而 `status`
    和 login-check 读的正是那份文件。所以这里挂住同一个通道每 `poll` 秒看一次 cookie 名字
    （值不落地），凑齐才存；等满 `wait` 分钟还没等到才退回手动回车——扫得慢不算失败。
    刻意不开标签页跳转来校验：Boss 的 SPA 跳转会把接管中的标签页换掉，
    手里的 Page 对象随即失效（TargetClosedError）。cookie 是浏览器级 API，不受换页影响。
    """
    path = AUTH_DIR / f"{platform}_storage.json"
    open_edge(url)
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[{platform}] 登录凭证存放地址：{path}")
    print(f"[{platform}] 已在 Edge 打开登录页，请在该窗口完成登录（扫码/短信均可）。")
    print(f"[{platform}] 判据凑齐会自动保存（最多等 {wait / 60:.0f} 分钟），不用在这个窗口按键。")
    partial = None
    try:
        with attach() as ctx:
            deadline = time.time() + wait
            while True:
                try:
                    have, missing = _login_snapshot(ctx, platform, login_cookies)
                except Exception as e:
                    print(f"[{platform}] 通道断了：{first_line(e)}")
                    break
                if have:
                    partial = (have, missing)
                if not missing:
                    print(f"[{platform}] " + _saved_line(have, missing, path))
                    return
                if time.time() >= deadline:
                    print(f"[{platform}] {wait / 60:.0f} 分钟到了还没等齐登录判据。"
                          "还在登就继续按回车复查，不想登就 Ctrl+C。")
                    break
                time.sleep(poll)
    except Exception as e:
        # 接不上窗口（被关掉/被别的实例接管）：交回手动那条路，它会给完整的原因
        print(f"[{platform}] 暂时接不上那个 Edge 窗口：{first_line(e)}")
    if partial and partial[1]:
        # 轮询期间见过半套：先把缺哪笔讲明白，再交给回车那条路，省得他从零猜
        print(f"[{platform}] " + _saved_line(partial[0], partial[1], path))
    _login_manual_check(platform, login_cookies, path)
