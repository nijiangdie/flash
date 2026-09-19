"""本地 Web 服务：把『采集新数据』和『刷新数据』都收进页面，浏览器打开一个地址即可。

用法：
  python cli.py serve                    # 启动后自动打开浏览器
  python cli.py serve --port 9000 --no-browser
"""
import contextlib
import io
import json
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, report
from .collectors import boss, job51, zhitong
from .config import AUTH_DIR, DAILY_CAP, MAX_PAGES_DEFAULT

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

COLLECTORS = {"boss": boss, "job51": job51, "zhitong": zhitong}

_CITY_ORDER = ["全国", "北京", "上海", "广州", "深圳", "杭州", "成都", "东莞",
               "长沙", "武汉", "南京", "苏州", "重庆", "天津", "郑州", "西安"]


def city_options() -> list[str]:
    """合并三个平台的 CITY_CODES，作为页面城市下拉的选项。"""
    seen = []
    for mod in COLLECTORS.values():
        for c in getattr(mod, "CITY_CODES", {}):
            if c not in seen:
                seen.append(c)
    seen.sort(key=lambda c: (_CITY_ORDER.index(c) if c in _CITY_ORDER else 99, c))
    return seen


def city_options_html() -> str:
    return "".join(
        f'<option value="{c}"{" selected" if c == "全国" else ""}>{c}</option>'
        for c in city_options()
    )


class CollectTask:
    """单个采集任务的运行状态，供前端轮询。"""

    def __init__(self, platform: str, query: str, city: str, pages: int):
        self.id = uuid.uuid4().hex[:12]
        self.platform = platform
        self.query = query
        self.city = city
        self.pages = pages
        self.status = "running"  # running | done | failed
        self.logs: list[tuple[str, str]] = []
        self.error = None
        self.collected = 0
        self.added = 0
        self.started_at = datetime.now().strftime("%H:%M:%S")
        self.finished_at = None
        self.lock = threading.Lock()

    def log(self, line: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append((ts, line))

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "platform": self.platform, "query": self.query,
                "city": self.city, "pages": self.pages,
                "status": self.status, "error": self.error,
                "collected": self.collected, "added": self.added,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "logs": self.logs[-200:],
            }


class _Capture(io.TextIOBase):
    """把采集过程中的 print 输出逐行收进任务日志。"""

    def __init__(self, task: CollectTask):
        self.task = task

    def write(self, s: str) -> int:
        line = s.rstrip("\r\n")
        if line:
            self.task.log(line)
        return len(s)

    def flush(self):
        pass


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, CollectTask] = {}
        self._current: CollectTask | None = None
        self._lock = threading.Lock()

    def current_id(self) -> str | None:
        with self._lock:
            if self._current and self._current.status == "running":
                return self._current.id
        return None

    def get(self, tid: str) -> CollectTask | None:
        return self._tasks.get(tid)

    def start(self, platform: str, query: str, city: str, pages: int):
        with self._lock:
            if self._current and self._current.status == "running":
                return None, "已有采集任务正在运行，请先等它完成。"
            task = CollectTask(platform, query, city, pages)
            self._tasks[task.id] = task
            self._current = task
        t = threading.Thread(target=self._run, args=(task,), daemon=True)
        t.start()
        return task, None

    def _run(self, task: CollectTask):
        cap = _Capture(task)
        records, added = [], 0
        try:
            with contextlib.redirect_stdout(cap):
                records = COLLECTORS[task.platform].collect(
                    query=task.query, city=task.city, max_pages=task.pages)
                if records:
                    db.init_db()
                    added = db.save_jobs(records)
            with task.lock:
                task.collected = len(records)
                task.added = added
                task.status = "done"
                task.finished_at = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            task.log(f"[失败] {e}")
            with task.lock:
                task.status = "failed"
                task.error = str(e)
                task.finished_at = datetime.now().strftime("%H:%M:%S")


class Handler(BaseHTTPRequestHandler):
    server_version = "JobStats/1.0"
    manager: TaskManager = None
    token: str = ""

    # ---- 基础工具 ----
    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self) -> bool:
        return self.headers.get("X-Token") == self.token

    def log_message(self, *a):
        pass  # 静默访问日志

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._page()
        if not path.startswith("/api/"):
            return self._json({"ok": False, "error": "404"}, 404)
        if not self._check_token():
            return self._json({"ok": False, "error": "安全校验失败"}, 403)
        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": db.fetch_all()})
        if path == "/api/collect/current":
            tid = self.manager.current_id()
            task = self.manager.get(tid) if tid else None
            return self._json({"ok": True, "task": task.snapshot() if task else None})
        if path.startswith("/api/collect/"):
            task = self.manager.get(path.rsplit("/", 1)[-1])
            return self._json({"ok": task is not None,
                               "task": task.snapshot() if task else None})
        return self._json({"ok": False, "error": "404"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/collect":
            return self._json({"ok": False, "error": "404"}, 404)
        if not self._check_token():
            return self._json({"ok": False, "error": "安全校验失败"}, 403)
        try:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                .decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)

        platform = body.get("platform")
        query = (body.get("query") or "").strip()
        city = (body.get("city") or "全国").strip()
        try:
            pages = int(body.get("pages") or MAX_PAGES_DEFAULT)
        except (TypeError, ValueError):
            pages = MAX_PAGES_DEFAULT
        pages = max(1, min(10, pages))

        if platform not in COLLECTORS:
            return self._json({"ok": False, "error": "不支持的平台"}, 400)
        if not query:
            return self._json({"ok": False, "error": "请输入搜索关键词"}, 400)
        if platform == "boss" and not (AUTH_DIR / "boss_storage.json").exists():
            return self._json(
                {"ok": False,
                 "error": "Boss直聘需要先登录一次：在命令行运行 `python cli.py login boss`，登录后再回来采集。"},
                400)

        task, err = self.manager.start(platform, query, city, pages)
        if err:
            return self._json({"ok": False, "error": err}, 409)
        return self._json({"ok": True, "task": task.snapshot()})

    # ---- 页面 ----
    def _page(self):
        records = db.fetch_all()
        html = report.render_web(records, self.token, city_options_html())
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = DEFAULT_PORT, open_browser: bool = True):
    db.init_db()
    manager = TaskManager()
    token = uuid.uuid4().hex

    Handler.manager = manager
    Handler.token = token

    try:
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as e:
        raise SystemExit(
            f"无法监听 {HOST}:{port}（{e}）。"
            f"多半是已有一个筛选台在运行——直接用原来那个即可，或换端口："
            f"python cli.py serve --port {port + 1}")
    url = f"http://{HOST}:{port}"
    print(f"职位筛选台已启动：{url}")
    print(f"今日已入库 {db.count_today()} 条（每日额度 {DAILY_CAP} 条），Ctrl+C 停止服务。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()