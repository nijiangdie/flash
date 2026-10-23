"""本地 Web 服务：把『采集新数据』和『刷新数据』都收进页面，浏览器打开一个地址即可。

用法：
  python cli.py serve                    # 启动后自动打开浏览器
  python cli.py serve --port 9000 --no-browser
  set JOBSTATS_TOKEN=xxx && python cli.py serve --host 0.0.0.0   # 给 Docker 里的 n8n 对接用
"""
import contextlib
import io
import json
import os
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import browser, db, details, report
from .collectors import COLLECTORS, boss, collect_and_save
from .config import DAILY_CAP, DETAIL_DAILY_CAP, LOG_DIR, MAX_PAGES_DEFAULT

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# 给外部编排工具（n8n 等）用的固定令牌。不设就每次启动随机一个——
# 随机值只有自带页面能拿到，外部程序每次重启都得先从 HTML 里正则抠，很脆。
TOKEN_ENV = "JOBSTATS_TOKEN"

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


def _query_int(raw: list[str], default: int) -> int:
    try:
        return int(raw[0])
    except (IndexError, TypeError, ValueError):
        return default


def _append_log_file(ts: str, task: "CollectTask", line: str):
    """任务日志同时落盘（按天一个文件）：不写盘的话，服务一重启，昨晚那次为什么 0 条就查不到了。

    文件里只有平台、关键词、城市和采集器自己的 print 输出；Playwright 的报错在 `_run`
    里先过 `browser.first_line` 截断，登录凭证不会跟着 Call log 进这个文件。
    """
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"collect-{day}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} [{task.platform}] {task.id} {line}\n")
    except OSError:
        pass        # 日志写不下去不该让采集失败


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
        _append_log_file(ts, self, line)

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
        records, stats = [], {"added": 0, "refreshed": 0, "blocked": 0,
                              "rules": 0, "dropped": 0}
        task.log(f"开始：{task.platform} 「{task.query}」@ {task.city}，最多 {task.pages} 页")
        try:
            with contextlib.redirect_stdout(cap):
                # 每页一存在 collect_and_save 里面做：中途崩了前面几页已经落库
                records, stats = collect_and_save(COLLECTORS[task.platform],
                                                  task.query, task.city, task.pages)
            parts = [f"入库新增 {stats['added']} 条", f"刷新 {stats['refreshed']} 条"]
            if stats["blocked"]:
                parts.append(f"屏蔽名单拦掉 {stats['blocked']} 条")
            if stats.get("rules"):
                parts.append(f"屏蔽规则拦掉 {stats['rules']} 条")
            if stats.get("dropped"):
                parts.append(f"相关性闸门剔掉 {stats['dropped']} 条")
            task.log(f"完成：采到 {len(records)} 条，" + "、".join(parts))
            with task.lock:
                task.collected = len(records)
                task.added = stats["added"]
                task.status = "done"
                task.finished_at = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            # first_line：Playwright 的 Call log 里原样带着请求头（bst/zp_at），不能整条写进日志
            task.log(f"[失败] {browser.first_line(e)}")
            with task.lock:
                task.status = "failed"
                task.error = browser.first_line(e)
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
        try:
            return self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 页面自己撤了请求（刷新/关标签），没什么可回的
        except Exception as e:
            # 不兜住的话：堆栈打到终端、连接半路掐断，页面只会说 Failed to fetch，
            # 看着像网络问题，其实是本地服务抛了。原因得留在响应里。
            return self._json({"ok": False,
                               "error": f"本地服务出错：{type(e).__name__}: {e}"}, 500)

    def _get(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._page()
        if not path.startswith("/api/"):
            return self._json({"ok": False, "error": "404"}, 404)
        if not self._check_token():
            return self._json({"ok": False, "error": "安全校验失败"}, 403)
        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": db.fetch_all()})
        if path == "/api/details":
            return self._json({"ok": True, "keys": db.detail_keys()})
        if path == "/api/analysis":
            return self._json({"ok": True, "keys": db.analysis_keys()})
        if path == "/api/trash":
            return self._json({"ok": True, "batches": db.trash_batches()})
        if path == "/api/pending_analysis":
            q = parse_qs(urlparse(self.path).query)
            return self._json({"ok": True, "jobs": db.pending_analysis(
                limit=_query_int(q.get("limit"), 20),
                platform=(q.get("platform", [""])[0] or None),
                redo=(q.get("redo", [""])[0] or "").lower() in ("1", "true", "yes"))})
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
        try:
            return self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            return self._json({"ok": False,
                               "error": f"本地服务出错：{type(e).__name__}: {e}"}, 500)

    def _post(self):
        path = self.path.split("?")[0]
        if path not in ("/api/collect", "/api/detail", "/api/status", "/api/analysis",
                        "/api/delete", "/api/restore"):
            return self._json({"ok": False, "error": "404"}, 404)
        if not self._check_token():
            return self._json({"ok": False, "error": "安全校验失败"}, 403)
        try:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                .decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)

        if path == "/api/detail":
            return self._detail(body)
        if path == "/api/status":
            return self._status(body)
        if path == "/api/analysis":
            return self._analysis(body)
        if path == "/api/delete":
            return self._delete(body)
        if path == "/api/restore":
            return self._restore(body)

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
        if platform == "boss":
            # 通道不在就先替他把 Edge 弹出来，页面上点一下就能走完，不用去命令行敲命令
            hint = boss.ensure_window()
            if hint:
                return self._json({"ok": False, "error": hint}, 400)

        task, err = self.manager.start(platform, query, city, pages)
        if err:
            return self._json({"ok": False, "error": err}, 409)
        return self._json({"ok": True, "task": task.snapshot()})

    def _detail(self, body: dict):
        """读一条职位的正文；本地没有就现抓这一条（受每日正文额度熔断）。"""
        try:
            job_id = int(body.get("id"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "缺少职位行号"}, 400)
        rows = db.jobs_by_ids([job_id])
        if not rows:
            return self._json({"ok": False, "error": "库里找不到这条职位"}, 404)
        job = rows[0]
        key = (job["platform"], job["platform_job_id"])
        stored = db.get_detail(*key)
        if stored:
            return self._json({"ok": True, "detail": stored})
        reason = details.NO_DETAIL.get(job["platform"])
        if reason:
            return self._json({"ok": False, "error": reason}, 400)
        if self.manager.current_id():
            return self._json({"ok": False, "error": "采集任务正在运行，先等它结束再抓正文。"},
                              409)
        try:
            res = details.collect_details([job])
        except RuntimeError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        stored = db.get_detail(*key)
        if not stored:
            return self._json({"ok": False, "error":
                               "这条没拿到正文：详情页没解析出正文。可能是职位已下架，"
                               "也可能平台要人机验证（遇到就立刻停手，不绕）。"
                               "点开这条的链接自己核对一下。"},
                              400)
        return self._json({"ok": True, "detail": stored,
                           "note": f"今日正文还能抓 {res['left']} 条"})

    def _status(self, body: dict):
        """标注一条职位的投递进度：只写本地的状态/备注两列，不发任何平台请求。"""
        try:
            job_id = int(body.get("id"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "缺少职位行号"}, 400)
        status = body.get("status")
        note = body.get("note")
        if status is None and note is None:
            return self._json({"ok": False, "error": "没说要改什么"}, 400)
        if not isinstance(status, (str, type(None))) or not isinstance(note, (str, type(None))):
            return self._json({"ok": False, "error": "状态和备注得是文字"}, 400)
        try:
            saved = db.set_status(job_id, status, note)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        if saved is None:
            return self._json({"ok": False, "error": "库里找不到这条职位"}, 404)
        return self._json(dict(ok=True, **saved))

    def _analysis(self, body: dict):
        """收下外部（n8n 调本地模型）算好的一条分析结果，只写 job_analysis。

        和 _status 一样是纯本地标注：不访问任何平台，字段由 db.save_analysis 收紧，
        放宽 jobs 表的采集白名单不是这里的职责。
        """
        try:
            saved = db.save_analysis(body)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        if saved is None:
            return self._json({"ok": False, "error": "库里找不到这条职位"}, 404)
        return self._json(dict(ok=True, **saved))

    def _delete(self, body: dict):
        """删掉本机的几条职位（连带正文和匹配度）。

        只动 data/jobs.db，不发任何平台请求，更不等于撤销投递——页面上确认框写的就是这句。
        删之前的原始行落在 data/trash，撤销靠那个文件，不靠浏览器把数据传回来。
        """
        ids = body.get("ids")
        if not isinstance(ids, list):
            return self._json({"ok": False, "error": "要删除的职位行号得是个列表"}, 400)
        try:
            res = db.delete_jobs(ids)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        return self._json(dict(ok=True, **res))

    def _restore(self, body: dict):
        """撤销一次删除：批次号必须是服务端自己写进回收站的那个。"""
        try:
            res = db.restore_batch(str(body.get("batch") or ""))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        if res is None:
            return self._json({"ok": False,
                               "error": "回收站里找不到这一批（撤销过一次就没了）"}, 404)
        return self._json(dict(ok=True, **res))

    # ---- 页面 ----
    def _page(self):
        records = db.fetch_all()
        html = report.render_web(records, self.token, city_options_html(),
                                 no_detail=details.NO_DETAIL)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def resolve_token() -> str:
    """外部编排（n8n 等）要用 JOBSTATS_TOKEN 里的固定值才连得上。

    没设就每次启动随机一个：页面照样能用（令牌是随 HTML 一起发出去的），
    但外部程序重启后就拿不到了。
    """
    return os.environ.get(TOKEN_ENV, "").strip() or uuid.uuid4().hex


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, host: str = HOST):
    db.init_db()
    manager = TaskManager()
    token = resolve_token()

    Handler.manager = manager
    Handler.token = token

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        raise SystemExit(
            f"无法监听 {host}:{port}（{e}）。"
            f"多半是已有一个筛选台在运行——直接用原来那个即可，或换端口："
            f"python cli.py serve --port {port + 1}")
    # 绑到 0.0.0.0 只是监听范围，不是一个能打开的地址；页面照样用回环地址访问。
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}"
    print(f"职位筛选台已启动：{url}")
    if host != HOST:
        # 这个服务能触发采集、能读写本地库，离开回环地址就等于交给了整个网段。
        print(f"[注意] 监听在 {host}，同网段其它机器也能访问；"
              f"请确认 {TOKEN_ENV} 设了固定值，并用防火墙把 {port} 限到需要的网卡。")
    if not os.environ.get(TOKEN_ENV, "").strip():
        print(f"提示：没设 {TOKEN_ENV}，本次用的是随机令牌，外部程序（n8n 等）连不上。")
    print(f"今日已入库 {db.count_today()} 条（每日额度 {DAILY_CAP} 条），"
          f"正文已抓 {db.count_details_today()} 条（每日额度 {DETAIL_DAILY_CAP} 条）。")
    print("Ctrl+C 停止服务。")
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