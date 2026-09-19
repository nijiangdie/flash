"""Playwright 会话公共层：浏览器启动、登录态、限速、每日额度。"""
import random
import time

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from . import db
from .config import AUTH_DIR, DELAY_RANGE, DAILY_CAP, REQUEST_TIMEOUT_MS


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
    """带登录态的浏览器会话，用完 with 自动关闭。"""

    def __init__(self, platform: str, headless=False):
        self.platform = platform
        self.headless = headless
        self._p = self._browser = self._context = None

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

    def __exit__(self, *exc):
        # 刷新登录态文件（登录过程中 cookie 会更新）
        try:
            self._context.storage_state(path=str(self.storage))
        except Exception:
            pass
        self._browser.close()
        self._p.stop()


def interactive_login(platform: str, url: str, success_check) -> None:
    """打开有头浏览器让用户手动登录，成功后保存登录态。"""
    print(f"[{platform}] 正在打开浏览器，请在页面中完成登录（扫码/短信均可）...")
    with Session(platform, headless=False) as ctx:
        page = ctx.new_page()
        page.goto(url)
        print("等待登录成功（最长 5 分钟）...")
        deadline = time.time() + 300
        while time.time() < deadline:
            if success_check(page):
                print(f"[{platform}] 登录成功，登录态已保存。")
                time.sleep(2)
                return
            time.sleep(3)
        raise RuntimeError("等待登录超时（5 分钟），请重试。")
