"""Playwright 会话公共层：浏览器启动、登录态、限速、每日额度。"""
import json
import random
import subprocess
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


def login_state_report(platform: str, required=()) -> list[str]:
    """登录态体检：只看 cookie 的名字和有效期，值一律不碰。

    返回值是要打印（也可能写进日志）的，把 bst/zp_at 的值打出来等于把账号贴到屏幕上。
    """
    path = AUTH_DIR / f"{platform}_storage.json"
    lines = [f"文件：{path}"]
    if not required:
        # 不需要登录的平台别对着它的（永远不存在的）登录态文件瞎操心
        lines.append("该平台不需要登录凭证，到这里就够了。")
        return lines
    if not path.exists():
        lines.append("还没保存过登录态。下一步：双击 login-boss.bat，"
                     "或 python cli.py login boss --edge。")
        return lines
    try:
        cookies = json.loads(path.read_text(encoding="utf-8")).get("cookies") or []
    except (OSError, ValueError) as e:
        lines.append(f"文件读不出来（{type(e).__name__}）：重新扫一次码覆盖它。")
        return lines
    saved = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    lines.append(f"共 {len(cookies)} 个 cookie，这份存于 {saved}")
    by_name = {str(c.get("name")): c for c in cookies}
    expired, absent, session_only = [], [], []
    for name in sorted(required):
        c = by_name.get(name)
        if c is None:
            absent.append(name)
            lines.append(f"  {name}: 没有（登录判据缺失）")
            continue
        exp = c.get("expires")
        if not isinstance(exp, (int, float)) or exp is None or exp <= 0:
            session_only.append(name)
            lines.append(f"  {name}: 有，但是会话 cookie（关掉浏览器就失效，有效期不判断）")
            continue
        left = (exp - time.time()) / 86400.0
        if left < 0:
            expired.append(name)
            lines.append(f"  {name}: 已过期 {-left:.1f} 天")
        else:
            lines.append(f"  {name}: 还剩 {left:.1f} 天")
    if absent:
        lines.append(f"结论：这份登录态里没有 {'、'.join(absent)}——"
                     "那次是匿名访问，不算登录成功。重新扫一次码。")
    elif expired:
        lines.append(f"结论：{'、'.join(expired)} 已经过期。再敲采集命令也没用，"
                     "重新扫一次码才是正解（这也是 code=37 最常见的那个来源）。")
    elif session_only:
        lines.append(f"结论：凭证都在，但 {'、'.join(session_only)} 看不出有效期，"
                     "能不能用只有采一次才知道。")
    else:
        lines.append("结论：登录凭证都在且没过期。采集还是 0 条的话，看平台返回的 code 再判。")
    return lines


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


def open_edge(url: str, port: int = CDP_PORT, wait: float = 20.0):
    """以独立配置目录启动真实 Edge，并等调试端口真正可用。

    不能启动完就直接接管：Edge 可能把地址转交给同目录的已有实例后自己退出，
    也可能窗口根本没起来，端口状态只有轮询才准。
    """
    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [edge_exe(), f"--remote-debugging-port={port}",
         f"--user-data-dir={CDP_PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.0)
        if cdp_alive(port):
            return proc
        if proc.poll() is not None:
            break
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


def login_with_edge(platform: str, url: str, login_cookies: set[str]) -> None:
    """在真实 Edge 里手动登录，回车后检查是否拿到了登录凭证 cookie。

    刻意不开标签页跳转来校验：Boss 的 SPA 跳转会把接管中的标签页换掉，
    手里的 Page 对象随即失效（TargetClosedError）。cookie 是浏览器级 API，不受换页影响。
    """
    open_edge(url)
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[{platform}] 登录凭证存放地址：{AUTH_DIR / (platform + '_storage.json')}")
    print(f"[{platform}] 已在 Edge 打开登录页，请在该窗口完成登录（扫码/短信均可）。")
    while True:
        input("          登录完成后回到这里按回车检查（不想登录按 Ctrl+C 退出）... ")
        try:
            with attach() as ctx:
                names = {c["name"] for c in ctx.cookies()}
                got = names & login_cookies
                if got:
                    ctx.storage_state(path=str(AUTH_DIR / f"{platform}_storage.json"))
        except RuntimeError:
            # 这里的 attach 失败几乎总是那个窗口中途没了，不是"还没登录"
            raise RuntimeError(
                "刚才那个 Edge 窗口已经不在了（被关掉，或被别的 Edge 实例接管）。"
                "请先关掉所有 Edge 窗口，再重新运行本命令，登录完别再关那个窗口。")
        if got:
            print(f"[{platform}] 登录成功（检测到 {', '.join(sorted(got))}），"
                  f"已保存到 {AUTH_DIR / (platform + '_storage.json')}")
            return
        print("          还没拿到登录凭证。请在 Edge 窗口里完成扫码/验证，再按一次回车。")
