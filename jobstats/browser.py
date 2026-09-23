"""Playwright 会话公共层：浏览器启动、登录态、限速、每日额度。"""
import random
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from . import db
from .config import (AUTH_DIR, CDP_PORT, CDP_PROFILE_DIR, DELAY_RANGE, DAILY_CAP,
                     EDGE_PATHS, REQUEST_TIMEOUT_MS)


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
        browser = p.chromium.launch(channel="chrome", headless=headless)
    except Exception:
        browser = p.chromium.launch(headless=headless)
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
def attach(port: int = CDP_PORT):
    """接管已运行的真实 Edge，交出 BrowserContext；退出时只断开，不关用户的窗口。"""
    if not cdp_alive(port):
        raise RuntimeError("没有可接管的 Edge 窗口，请先运行 login。")
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
    print(f"[{platform}] 已在 Edge 打开登录页，请在该窗口完成登录（扫码/短信均可）。")
    while True:
        input("          登录完成后回到这里按回车检查（不想登录按 Ctrl+C 退出）... ")
        with attach() as ctx:
            names = {c["name"] for c in ctx.cookies()}
            got = names & login_cookies
            if got:
                ctx.storage_state(path=str(AUTH_DIR / f"{platform}_storage.json"))
        if got:
            print(f"[{platform}] 登录成功（检测到 {', '.join(sorted(got))}），"
                  f"已保存到 data/auth/{platform}_storage.json")
            return
        print("          还没拿到登录凭证。请在 Edge 窗口里完成扫码/验证，再按一次回车。")
