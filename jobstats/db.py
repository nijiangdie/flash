"""SQLite 存储层：职位记录去重入库、每日额度查询、CSV 导出。"""
import csv
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import skills
from .config import ALLOWED_DETAIL_FIELDS, DB_PATH, TRASH_DIR, ALLOWED_FIELDS

# 入库列顺序（fetched_at 由本层统一赋值）
INSERT_COLUMNS = (
    "platform", "platform_job_id", "title", "company", "salary_raw",
    "salary_min", "salary_max", "salary_months", "city", "district",
    "experience", "education", "industry", "company_scale",
    "skills", "welfare", "url", "fetched_at",
)
# 与 config 的合规白名单保持一致，新增字段时两处需同步
assert set(INSERT_COLUMNS) == ALLOWED_FIELDS, "INSERT_COLUMNS 与 ALLOWED_FIELDS 不一致"

# ---- 投递状态（本地标注，不是平台数据）----
# status/note 刻意不在 ALLOWED_FIELDS 里：采集器写不进这两列，唯一入口是下面的 set_status，
# 记录的是"我自己投没投、面得怎么样"，全程不访问平台，也不去平台同步任何沟通状态。
STATUS_LABELS = ("", "想投", "已投", "HR已读", "约面", "已面", "offer", "不合适")
NOTE_MAX = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_job_id TEXT NOT NULL,
    title TEXT,
    company TEXT,
    salary_raw TEXT,
    salary_min REAL,
    salary_max REAL,
    salary_months INTEGER,
    city TEXT,
    district TEXT,
    experience TEXT,
    education TEXT,
    industry TEXT,
    company_scale TEXT,
    skills TEXT,
    welfare TEXT,
    url TEXT,
    fetched_at TEXT,
    status TEXT,
    status_at TEXT,
    note TEXT,
    last_seen_at TEXT,
    UNIQUE(platform, platform_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at);
CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);

-- 屏蔽名单：你在页面上删掉的职位记在这儿，下次采集直接跳过。
-- 没有这张表的话，"每天定时跑同一个关键词"会把你手工清掉的噪音一条条原样采回来。
-- title/company 只是让你认得出当初删的是哪条，不参与判断。
CREATE TABLE IF NOT EXISTS blocked_jobs (
    platform TEXT NOT NULL,
    platform_job_id TEXT NOT NULL,
    title TEXT,
    company TEXT,
    blocked_at TEXT,
    PRIMARY KEY(platform, platform_job_id)
);

-- 屏蔽规则：按公司或标题关键词批量拦，一条顶掉一整类噪音。
-- 单条名单挡不住"仓库管理员/打印机维修"这类换公司反复出现的岗位——2026-10-13
-- Boss 那 18 条里 5 条是这一类，一条条删要删 5 次，第二天还可能换个公司再来。
-- kind 只有 company / keyword 两种：company 比对整串相等，keyword 是标题子串（不分大小写）。
CREATE TABLE IF NOT EXISTS blocked_rules (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY(kind, value)
);

CREATE TABLE IF NOT EXISTS job_details (
    platform TEXT NOT NULL,
    platform_job_id TEXT NOT NULL,
    description TEXT,
    keyword_tags TEXT,
    url TEXT,
    fetched_at TEXT,
    PRIMARY KEY(platform, platform_job_id)
);
CREATE INDEX IF NOT EXISTS idx_details_fetched ON job_details(fetched_at);

CREATE TABLE IF NOT EXISTS job_analysis (
    platform TEXT NOT NULL,
    platform_job_id TEXT NOT NULL,
    score REAL,
    matched TEXT,
    gaps TEXT,
    reasons TEXT,
    model TEXT,
    analyzed_at TEXT,
    PRIMARY KEY(platform, platform_job_id)
);

-- 采集留痕：每次跑采集记一行「用的什么关键词、什么城市、几页、结果如何」。
-- 2026-10-13 查噪声时发现库里查不到那天 Boss 搜的是 IT 还是 Python开发，
-- 只能从 18 条标题反推；命令行那条路径又不像页面采集那样有任务日志。
-- drop_reasons 存闸门按原因汇总的条数，方便回答"今天少掉的那几条是被谁拦的"。
-- drop_titles 存同一批被拦下的标题（原因 -> 标题列表）：闸门在入库之前判，库里没有这些行，
-- 光有数字看不出有没有误杀，这列就是给 `cli.py runs` 复核用的。
CREATE TABLE IF NOT EXISTS collect_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    seconds REAL,
    platform TEXT,
    query TEXT,
    city TEXT,
    pages INTEGER,
    collected INTEGER,
    added INTEGER,
    refreshed INTEGER,
    blocked INTEGER,
    rules INTEGER,
    dropped INTEGER,
    drop_reasons TEXT,
    drop_titles TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON collect_runs(started_at DESC);
"""


@contextmanager
def _conn():
    """连接在语句块成功时提交、任何情况下都会关闭。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.executescript(SCHEMA)
        # 老库补列：SQLite 的 ALTER TABLE 没有 IF NOT EXISTS，先看现有的列再补
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        for col in ("status", "status_at", "note"):
            if col not in cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
        # status_at 刻意不回填：补列之前那几条是什么时候标的没人知道，last_seen_at 是
        # "最后一次采到"、不是"最后一次标注"，拿它顶上去等于给漏斗造一个假日期。
        # 老行就留 NULL，下面的 status_funnel 会单独数出"改过状态但没记时刻"的条数。
        if "last_seen_at" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN last_seen_at TEXT")
            # 补列之前的行没有"最近见到"，只能用入库时间顶上：老数据从没被重新采过，
            # 两者本来就相等。不回填的话筛选台上一整片会显示空白。
            c.execute("UPDATE jobs SET last_seen_at=fetched_at WHERE last_seen_at IS NULL")
        # collect_runs 是前几轮加的表；先建起来的那版缺 rules / drop_titles，按同一套路补上
        run_cols = {r["name"] for r in c.execute("PRAGMA table_info(collect_runs)")}
        for col, decl in (("rules", "INTEGER"), ("drop_titles", "TEXT")):
            if run_cols and col not in run_cols:
                c.execute(f"ALTER TABLE collect_runs ADD COLUMN {col} {decl}")


# 重新采到同一条职位时，用平台新数据刷新的列。
# 刻意不含 platform/platform_job_id（定位用的键）、fetched_at（下面 save_jobs 里说明）、
# 也不含 status/note/last_seen_at（本地标注和本层自己的时间戳，采集器写不到那些列）。
REFRESH_COLUMNS = tuple(c for c in INSERT_COLUMNS
                        if c not in ("platform", "platform_job_id", "fetched_at"))
_REFRESH_SQL = "UPDATE jobs SET {}, last_seen_at=? WHERE platform=? AND platform_job_id=?".format(
    ", ".join(f"{c}=COALESCE(?, {c})" for c in REFRESH_COLUMNS))
_JSON_COLUMNS = ("skills", "welfare")


def _refresh_values(rec: dict) -> list:
    """刷新用的值：这次是空的列传 None，让 SQL 里的 COALESCE 保住上一次的值得。

    列表页经常不给行业/公司规模/技能标签，照直刷会把详情里拿到的数据抹平。
    """
    out = []
    for col in REFRESH_COLUMNS:
        v = rec.get(col)
        if v in (None, "", [], {}):
            out.append(None)
        elif col in _JSON_COLUMNS:
            out.append(json.dumps(v, ensure_ascii=False))
        else:
            out.append(v)
    return out


def save_jobs(records: list[dict], stats: dict | None = None) -> int:
    """入库：新职位插入，已采过的用平台最新数据刷新。返回**新入库**条数。

    `stats` 传一个空字典进来，跑完能从中读到 `added / refreshed / blocked`，
    命令行和页面的采集回执靠它说清"这次没新增"到底是重复、还是被你删过。

    刷新时 `fetched_at` 保持第一次采到的时刻不动：每日额度按 `fetched_at` 计数，
    同一条职位今天再采一遍不该把额度吃掉。`status / note` 是你自己标的投递进度，
    采集器压根写不到那两列（不在 ALLOWED_FIELDS 里），刷新也就冲不掉。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added = refreshed = blocked = rules_hit = 0
    insert_sql = "INSERT INTO jobs({},{}) VALUES({}{})".format(
        ",".join(INSERT_COLUMNS), "last_seen_at",
        ",".join("?" * len(INSERT_COLUMNS)), ",?")
    with _conn() as c:
        known = {f"{r['platform']}:{r['platform_job_id']}"
                 for r in c.execute("SELECT platform, platform_job_id FROM jobs")}
        blocked_keys = {f"{r['platform']}:{r['platform_job_id']}"
                        for r in c.execute("SELECT platform, platform_job_id FROM blocked_jobs")}
        companies, keywords = _load_block_rules(c)
        for r in records:
            # 合规校验：只允许白名单字段
            bad = set(r) - ALLOWED_FIELDS
            if bad:
                raise ValueError(f"检测到白名单之外的字段: {bad}，已拒绝入库")
            key = f"{r['platform']}:{r['platform_job_id']}"
            if key in blocked_keys:
                blocked += 1
                continue
            if rule_hit(r, companies, keywords):
                rules_hit += 1
                continue
            if key in known:
                c.execute(_REFRESH_SQL,
                          _refresh_values(r) + [now, r["platform"], r["platform_job_id"]])
                refreshed += 1
            else:
                row = dict(r, fetched_at=now)
                row["skills"] = json.dumps(r.get("skills") or [], ensure_ascii=False)
                row["welfare"] = json.dumps(r.get("welfare") or [], ensure_ascii=False)
                c.execute(insert_sql, [row.get(k) for k in INSERT_COLUMNS] + [now])
                known.add(key)
                added += 1
    if stats is not None:
        stats.update({"added": added, "refreshed": refreshed, "blocked": blocked,
                      "rules": rules_hit})
    return added


def save_stats(records: list[dict]) -> dict:
    """同 save_jobs，但要的是几个计数：新入库 / 刷新 / 被单条名单拦掉 / 被规则拦掉。"""
    st = {}
    added = save_jobs(records, stats=st)
    # stats 是 save_jobs 直接填的；调用方换成自己的实现（测试里就 mock 过）时，
    # added 至少还能从返回值拿回来。
    st.setdefault("added", added)
    st.setdefault("refreshed", 0)
    st.setdefault("blocked", 0)
    st.setdefault("rules", 0)
    return st


def log_collect_run(run: dict) -> None:
    """写一条采集留痕。写不进去不该让采集失败，所以异常只打印一行。"""
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO collect_runs(started_at, seconds, platform, query, city,
                       pages, collected, added, refreshed, blocked, rules, dropped,
                       drop_reasons, drop_titles, error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run.get("started_at"), run.get("seconds"), run.get("platform"),
                 run.get("query"), run.get("city"), run.get("pages"),
                 run.get("collected"), run.get("added"), run.get("refreshed"),
                 run.get("blocked"), run.get("rules"),
                 run.get("dropped"),
                 json.dumps(run.get("drop_reasons") or {}, ensure_ascii=False),
                 json.dumps(run.get("drop_titles") or {}, ensure_ascii=False),
                 run.get("error")))
    except (OSError, sqlite3.Error) as e:
        print(f"  采集留痕没写进去（不影响已入库的数据）：{e}")


def recent_collect_runs(limit: int = 20) -> list[dict]:
    """最近几次采集，新的在前。"""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM collect_runs ORDER BY started_at DESC, id DESC LIMIT ?",
            (max(1, limit),))]
    for r in rows:
        r["drop_reasons"] = json.loads(r.get("drop_reasons") or "{}")
        r["drop_titles"] = json.loads(r.get("drop_titles") or "{}")
    return rows


def set_status(job_id: int, status: str | None = None, note: str | None = None) -> dict | None:
    """标注投递进度。传 None 的那一列保持不动，改哪列由调用方决定。

    职位被重新采集时走的是 INSERT OR IGNORE（已存在就整行跳过），所以这里写进去的
    状态和备注不会被下一次采集冲掉。

    状态一变就盖 `status_at`（这是"投递漏斗"唯一的时间锚点）；只改备注不碰它，
    否则一条写着"周三再问一下"的备注会把投递日期推到今天，按周算的回复率就假了。
    标回空串等于撤掉这条标注，时刻也跟着清掉——留一个"没有状态但有日期"的行，
    漏斗里既不能算进分母、又解释不了它是谁。
    """
    if status is None and note is None:
        return None
    if status is not None and status not in STATUS_LABELS:
        raise ValueError(f"不认识的投递状态: {status}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets, args = [], []
    if status is not None:
        sets.append("status=?")
        args.append(status)
        sets.append("status_at=?")
        args.append(now if status else None)
    if note is not None:
        sets.append("note=?")
        args.append(note.strip()[:NOTE_MAX])
    args.append(job_id)
    with _conn() as c:
        if not c.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            return None
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)
        row = c.execute("SELECT status, note FROM jobs WHERE id=?", (job_id,)).fetchone()
    return {"status": row["status"] or "", "note": row["note"] or ""}


# 分母里算"已经投出去"的档位，和分子里算"有回音"的档位。
# 「想投」只在候选清单里，一条都没发出去，所以它两边都不算：把它算进分母，
# 回复率会被一堆根本没发生的投递稀释；「不合适」是终局，也不算回音。
APPLIED_LABELS = ("已投", "HR已读", "约面", "已面", "offer")
REPLIED_LABELS = ("HR已读", "约面", "已面", "offer")


def status_funnel(days: int | None = None) -> dict:
    """按投递状态计数，用来算"投出去多少条、有几条回了"。

    `labels` 只收 7 个真状态：空串（没标过、或标回空串）走 `unmarked` 那一个数，
    两边都收会在"库里共 N 条"里把同一条数两次。
    `days` 给定时只看 `status_at` 落在最近这些天里的行，同时把 `undated` 数出来：
    那几条在补 `status_at` 之前就标过状态了，谁也不知道是哪天标的，只能报数、不能猜日期。
    `status_at` 是"最后一次改状态的时刻"，不是"投递那一刻"——从已投改成 HR已读 会覆盖掉
    前一次，所以这里算得出比值，算不出"几天回复"。
    """
    where, args = "COALESCE(status, '')<>''", []
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        where += " AND status_at >= ?"
        args.append(cutoff)
    out = {"labels": {}, "total": 0, "undated": 0, "unmarked": 0, "window": days}
    with _conn() as c:
        for label, n in c.execute(
                f"SELECT status, COUNT(*) FROM jobs WHERE {where} GROUP BY status", args):
            out["labels"][label] = n
        # 这两句不带窗口条件：它们回答的是"库里还有什么没走到这一步"，
        # 跟"最近几天投了几条"是两个问题，塞进同一个 WHERE 会数出没人看得懂的数。
        out["undated"] = c.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE COALESCE(status, '')<>'' AND status_at IS NULL").fetchone()[0]
        out["unmarked"] = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE COALESCE(status, '')=''").fetchone()[0]
    out["total"] = sum(out["labels"].values())
    out["applied"] = sum(n for s, n in out["labels"].items() if s in APPLIED_LABELS)
    out["replied"] = sum(n for s, n in out["labels"].items() if s in REPLIED_LABELS)
    out["rate"] = (out["replied"] / out["applied"] * 100) if out["applied"] else None
    return out



def count_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        try:
            return c.execute(
                "SELECT COUNT(*) FROM jobs WHERE fetched_at LIKE ?", (today + "%",)
            ).fetchone()[0]
        except sqlite3.OperationalError:  # 表尚未创建
            return 0


# ---- 职位正文（JD）----
# 刻意不塞进 jobs 表：筛选台会把每行职位内嵌进 HTML，正文一条就上千字。
DETAIL_COLUMNS = ("platform", "platform_job_id", "description", "keyword_tags",
                  "url", "fetched_at")
assert set(DETAIL_COLUMNS) == ALLOWED_DETAIL_FIELDS, \
    "DETAIL_COLUMNS 与 ALLOWED_DETAIL_FIELDS 不一致"


def save_details(records: list[dict]) -> int:
    """写入职位正文，同一条职位重复抓则覆盖（招聘方会改 JD）。返回写入条数。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sql = "INSERT OR REPLACE INTO job_details({}) VALUES({})".format(
        ",".join(DETAIL_COLUMNS), ",".join("?" * len(DETAIL_COLUMNS)))
    n = 0
    with _conn() as c:
        for r in records:
            bad = set(r) - ALLOWED_DETAIL_FIELDS
            if bad:
                raise ValueError(f"检测到正文白名单之外的字段: {bad}，已拒绝入库")
            if not (r.get("description") or "").strip():
                continue
            row = dict(r, fetched_at=now)
            row["keyword_tags"] = json.dumps(r.get("keyword_tags") or [],
                                             ensure_ascii=False)
            n += c.execute(sql, [row.get(k) for k in DETAIL_COLUMNS]).rowcount
    return n


def count_details_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        try:
            return c.execute(
                "SELECT COUNT(*) FROM job_details WHERE fetched_at LIKE ?",
                (today + "%",)).fetchone()[0]
        except sqlite3.OperationalError:  # 表尚未创建
            return 0


def get_detail(platform: str, platform_job_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM job_details WHERE platform=? AND platform_job_id=?",
                        (platform, platform_job_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["keyword_tags"] = json.loads(d["keyword_tags"] or "[]")
    return d


def detail_keys() -> list[str]:
    """已有正文的职位键（platform:job_id），页面据此决定按钮写"看"还是"抓"。"""
    with _conn() as c:
        try:
            rows = c.execute("SELECT platform, platform_job_id FROM job_details").fetchall()
        except sqlite3.OperationalError:      # 表尚未创建
            return []
    return [f"{r['platform']}:{r['platform_job_id']}" for r in rows]


# ---- 职位分析结果（本地大模型算出来的匹配度）----
# 单独一张表、刻意不进 ALLOWED_FIELDS：分数是下游标注，和 status/note 同类，
# 采集器写不到它，重新采集也就冲不掉它；同时它由外部进程（n8n）通过接口送进来，
# 字段必须在这里收紧，不能让 POST 往库里塞任意键。
ANALYSIS_COLUMNS = ("platform", "platform_job_id", "score", "matched", "gaps",
                    "reasons", "model", "analyzed_at")
REASONS_MAX = 2000
MATCHED_MAX = 40       # 命中/缺口技能列表各截断，防止外部一次送进来几百项


def _str_list(val, cap: int) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        val = [x for x in re.split(r"[,、;；\|/]| / ", val) if x.strip()]
    if not isinstance(val, (list, tuple)):
        raise ValueError("matched/gaps 得是字符串数组")
    return [str(x).strip()[:60] for x in val if str(x).strip()][:cap]


def save_analysis(rec: dict) -> dict | None:
    """写入一条分析结果，同一职位重新分析则覆盖。

    职位必须已在 jobs 表里：这是接口侧的兜底，免得外部脚本把来路不明的
    platform_job_id 写进库，事后在筛选台里冒出一条对不上号的分数。
    """
    bad = set(rec) - set(ANALYSIS_COLUMNS)
    if bad:
        raise ValueError(f"分析结果有白名单之外的字段: {bad}")
    platform = str(rec.get("platform") or "").strip()
    pjid = str(rec.get("platform_job_id") or "").strip()
    if not platform or not pjid:
        raise ValueError("分析结果缺 platform / platform_job_id")
    score = rec.get("score")
    if score is not None:
        try:
            score = round(float(score), 1)
        except (TypeError, ValueError):
            raise ValueError("score 得是数字")
        if not 0 <= score <= 100:
            raise ValueError("score 得在 0~100 之间")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "platform": platform,
        "platform_job_id": pjid,
        "score": score,
        "matched": json.dumps(_str_list(rec.get("matched"), MATCHED_MAX),
                              ensure_ascii=False),
        "gaps": json.dumps(_str_list(rec.get("gaps"), MATCHED_MAX), ensure_ascii=False),
        "reasons": str(rec.get("reasons") or "").strip()[:REASONS_MAX],
        "model": str(rec.get("model") or "")[:80],
        "analyzed_at": now,
    }
    sql = "INSERT OR REPLACE INTO job_analysis({}) VALUES({})".format(
        ",".join(ANALYSIS_COLUMNS), ",".join("?" * len(ANALYSIS_COLUMNS)))
    with _conn() as c:
        if not c.execute("SELECT 1 FROM jobs WHERE platform=? AND platform_job_id=?",
                         (platform, pjid)).fetchone():
            return None
        c.execute(sql, [row[k] for k in ANALYSIS_COLUMNS])
    return {"platform": platform, "platform_job_id": pjid, "score": score,
            "analyzed_at": now}


def analysis_keys() -> list[str]:
    """已分析过的职位键，供外部编排判断哪些还没跑过模型。"""
    with _conn() as c:
        try:
            rows = c.execute("SELECT platform, platform_job_id FROM job_analysis").fetchall()
        except sqlite3.OperationalError:
            return []
    return [f"{r['platform']}:{r['platform_job_id']}" for r in rows]


def pending_analysis(limit: int = 20, platform: str | None = None,
                     redo: bool = False) -> list[dict]:
    """挑「模型还没分析过、正文也已经在库里」的职位，把正文一起带出来。

    两个调用方（`cli.py analyze` 和 n8n 走的 GET /api/pending_analysis）都只做只读拉取：
    刻意限定在已有正文的职位上，拉一批待办绝不会顺带去访问招聘平台，
    每日正文额度也就不会被下游流程吃掉。redo=True 时连分析过的也返回，换提示词重跑用。
    """
    limit = max(1, min(int(limit), 100))
    with _conn() as c:
        tables = _tables(c)
        if "job_details" not in tables:
            return []          # 一条正文都还没有，没东西可分析
        sql = """SELECT j.*, d.description AS jd_text, d.keyword_tags AS jd_tags
                 FROM jobs j
                 JOIN job_details d
                   ON d.platform = j.platform AND d.platform_job_id = j.platform_job_id"""
        args: list = []
        if "job_analysis" in tables:
            sql += """ LEFT JOIN job_analysis a
                   ON a.platform = j.platform AND a.platform_job_id = j.platform_job_id"""
            if not redo:
                sql += " WHERE a.platform IS NULL"
        if platform:
            sql += " AND" if " WHERE" in sql else " WHERE"
            sql += " j.platform = ?"
            args.append(platform)
        sql += " AND TRIM(IFNULL(d.description, '')) <> ''"
        rows = [dict(r) for r in c.execute(sql + " ORDER BY j.fetched_at DESC LIMIT ?",
                                           args + [limit])]
    out = []
    for d in rows:
        d["description"] = d.pop("jd_text") or ""
        # n8n 那条链路上还要一份原样的详情页关键词，所以先留着再走清洗
        d["keyword_tags"] = json.loads(d.get("jd_tags") or "[]")
        # 老库的行这两列可能是 NULL，和 fetch_all 保持同一口径
        d["status"] = d.get("status") or ""
        d["note"] = d.get("note") or ""
        _clean_row_skills(d)
        out.append(d)
    return out


def jobs_by_ids(ids: list[int]) -> list[dict]:
    """按筛选台上的行号取职位（顺序跟传入的 id 顺序一致）。"""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM jobs WHERE id IN ({marks})", ids).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def jobs_missing_details(platform: str | None = None, limit: int = 20,
                         exclude_platforms: tuple = ()) -> list[dict]:
    """挑还没抓过正文、且留有详情页链接的职位，按最近采集优先。

    exclude_platforms 传 details.NO_DETAIL 的键：这些平台的行永远补不上正文，
    留在候选里只会让「待补正文」虚高，并把 --limit 的名额花在注定失败的行上。
    """
    sql = """SELECT j.* FROM jobs j
             LEFT JOIN job_details d
               ON d.platform = j.platform AND d.platform_job_id = j.platform_job_id
             WHERE d.platform IS NULL AND IFNULL(j.url, '') <> ''"""
    args: list = []
    if platform:
        sql += " AND j.platform = ?"
        args.append(platform)
    excl = sorted(set(exclude_platforms or ()))
    if excl:
        sql += f" AND j.platform NOT IN ({','.join('?' * len(excl))})"
        args.extend(excl)
    sql += " ORDER BY j.fetched_at DESC LIMIT ?"
    args.append(max(1, limit))
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _clean_row_skills(d: dict) -> None:
    """把一行的技能/福利按读取侧口径洗好，并记下技能是从哪儿来的（就地改 d）。

    列表页有词就以列表页为准；只有它为空、而这条抓过正文（`jd_tags` 是 LEFT JOIN
    带出来的详情页关键词）才拿正文的词顶上。`fetch_all` 和 `pending_analysis` 必须走
    这同一个函数：分叉过一次，筛选台上写着 14 个技能词的职位，送给模型的提示词却回一句
    「技能标签：未列」，模型就照着"这条没写技能"打分了。
    """
    raw_skills = json.loads(d.get("skills") or "[]")
    # 51job/Boss 把福利词塞在技能里，挪回福利列，两边都不丢信息
    moved = [s for s in raw_skills if skills.is_welfare(s)]
    list_side = skills.clean(raw_skills)
    jd_side = skills.clean(json.loads(d.pop("jd_tags", None) or "[]"))
    d["skills"] = list_side or jd_side
    d["skill_src"] = "list" if list_side else ("jd" if jd_side else "")
    d["welfare"] = skills.welfare(json.loads(d.get("welfare") or "[]") + moved)


def fetch_all() -> list[dict]:
    """读出全部职位，技能/福利按读取侧口径清洗过（库里存的仍是平台原文）。

    匹配度顺带 LEFT JOIN 出来：它存在 job_analysis，是另一张只读标注表，
    一次带出比让页面每行再查一遍划算，也不影响 jobs 的采集白名单。
    正文表的职位关键词也一并带出来，但只干一件事：列表页不给技能列的那些平台
    （`skills.NO_LIST_SKILLS`）抓过正文之后，技能列不该一直空着。同样不写库，
    来源记在 `skill_src` 里，界面和统计靠它说"这几个词是从详情页来的"。
    """
    with _conn() as c:
        tables = _tables(c)
        cols = ["j.*"]
        joins = []
        if "job_analysis" in tables:
            cols.append("a.score AS a_score, a.matched AS a_matched, a.gaps AS a_gaps, "
                        "a.reasons AS a_reasons, a.model AS a_model, "
                        "a.analyzed_at AS a_analyzed_at")
            joins.append("LEFT JOIN job_analysis a ON a.platform = j.platform"
                         " AND a.platform_job_id = j.platform_job_id")
        if "job_details" in tables:
            cols.append("d.keyword_tags AS jd_tags")
            joins.append("LEFT JOIN job_details d ON d.platform = j.platform"
                         " AND d.platform_job_id = j.platform_job_id")
        sql = ("SELECT " + ", ".join(cols) + " FROM jobs j " + " ".join(joins)
               + " ORDER BY j.fetched_at DESC")
        rows = c.execute(sql).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        # 迁移前的老行这两列是 NULL，统一成空字符串，页面和导出都不用防 null
        d["status"] = d.get("status") or ""
        d["note"] = d.get("note") or ""
        # 薪资区间是入库那一刻算的，口径后来升级过（千/万、裸数字、只写一个数），
        # 没再采第二遍的老行就一直是 NULL；Boss 通道另有一套只认 "N-MK" 的写法。
        # 读的时候按原文兜一次：否则这些行在薪资轴、中位数、年薪和 CSV 里
        # 全被当成"薪资不明"悄悄消失，页面上看不出是它的锅。
        if d.get("salary_min") is None:
            parsed = parse_salary(d.get("salary_raw") or "")
            if parsed:
                d["salary_min"], d["salary_max"], d["salary_months"] = parsed
        d["score"] = d.pop("a_score", None)
        d["matched"] = json.loads(d.pop("a_matched", None) or "[]")
        d["gaps"] = json.loads(d.pop("a_gaps", None) or "[]")
        d["reasons"] = d.pop("a_reasons", None) or ""
        d["model"] = d.pop("a_model", None) or ""
        d["analyzed_at"] = d.pop("a_analyzed_at", None) or ""
        _clean_row_skills(d)
        skills.add_buckets(d)   # 页面按档位筛，experience/education 本身仍是平台原文
        out.append(d)
    return out


def annual_wan(rec: dict) -> float | None:
    """年薪估算（万元/年）：月薪中位数 × 发薪月数，平台没写月数按 12 薪算。"""
    lo, hi = rec.get("salary_min"), rec.get("salary_max")
    if not lo or not hi:
        return None
    return round((lo + hi) / 2 * (rec.get("salary_months") or 12) / 10, 1)


# 导出列：数值薪资和年薪放一起，Excel 里能直接排序，也是以后喂给 AI 的字段
# status/note 是自己标的投递进度，导出后就是一份投递清单；status_at 让它同时是一份时间线
# （只有最后一次改状态的时刻，不是每一次；见 status_funnel 的说明）；正文放最后一列，免得挡住前面的
EXPORT_FIELDS = ["platform", "platform_job_id", "title", "company", "salary_raw",
                 "salary_min", "salary_max", "salary_months", "salary_annual_wan",
                 "city", "district", "experience", "education", "industry",
                 "company_scale", "skills", "welfare", "status", "status_at", "note",
                 "url", "fetched_at", "description"]


def export_csv(path):
    rows = fetch_all()
    with _conn() as c:
        details = {(r["platform"], r["platform_job_id"]): r["description"]
                   for r in c.execute(
                       "SELECT platform, platform_job_id, description FROM job_details")}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        w.writeheader()
        for r in rows:
            r["skills"] = " / ".join(r["skills"])
            r["welfare"] = " / ".join(r["welfare"])
            r["salary_annual_wan"] = annual_wan(r)
            r["description"] = details.get(
                (r["platform"], r["platform_job_id"])) or ""
            w.writerow({k: r.get(k) if r.get(k) is not None else ""
                        for k in EXPORT_FIELDS})


# ---- 删除与回收站 ----
# 删的只有本地库里的记录：不发任何平台请求，也不等于"撤销投递"。
# 删之前把三张表的原始行写进 data/trash/<批次>.json，页面上的"撤销"是从那儿拿回去的——
# 刻意不让浏览器把职位数据传回来，免得那个令牌变成任意写入的通道。
DELETE_MAX = 500           # 一次最多删这么多条（"删掉当前筛选结果"够用，又挡住一键清库）
TRASH_KEEP = 20            # 回收站只留最近这么多批，免得悄悄攒出第二个库
BATCH_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}\.json$")
# 还原用的列：不带自增 id（放回时重新分配，免得和删除之后新采的行撞号），status/note 要留住
# 撤销时逐列原样插回去。last_seen_at 必须跟着回来：INSERT_COLUMNS 是采集用的，
# 里面没有它，撤销后就只剩 fetched_at，而页面拿 last_seen_at 判「疑似已下架」
# （report.py 的 seenAt 会退回 fetched_at）——第一次采到在 14 天前的在职职位，
# 撤回来就被标成下架了。status_at 同理：漏掉它，删一条再撤回来就把"投出去几天了"
# 抹成没有日期，那条从此进不了按周算的分母。
RESTORE_JOBS = INSERT_COLUMNS + ("status", "status_at", "note", "last_seen_at")


def _tables(c) -> set:
    return {r["name"] for r in
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _key_cond(keys: list[tuple[str, str]]):
    return (" OR ".join(["(platform=? AND platform_job_id=?)"] * len(keys)),
            [x for k in keys for x in k])


def _trash_dir(trash_dir=None) -> Path:
    # 默认值直接在签名里写 TRASH_DIR 会在定义时就绑死，测试改 db.TRASH_DIR 就没用了
    return Path(trash_dir if trash_dir is not None else TRASH_DIR)


def _write_trash(payload: dict, trash_dir=None) -> str:
    d = _trash_dir(trash_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = "{}-{}.json".format(datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    dest = d / name
    blob = json.dumps(payload, ensure_ascii=False)
    # 先写临时文件再改名，中间不留"半个批次文件"：这个文件是唯一能撤销的依据，
    # 截断的 JSON 会让 trash_batches 跳过整批，"撤销"按钮再也不出现，而职位已经删了。
    tmp = Path(str(dest) + ".tmp")
    tmp.write_text(blob, encoding="utf-8")
    tmp.replace(dest)   # 同目录改名，Windows 上也是原子的
    keep = [f for f in sorted(d.glob("*.json"), reverse=True) if BATCH_RE.match(f.name)]
    for old in keep[TRASH_KEEP:]:
        old.unlink(missing_ok=True)
    return name


def delete_jobs(ids, trash_dir=None) -> dict:
    """按行号删职位，连带它的正文和分析结果；原始行先落回收站，再动库。"""
    if not ids:
        raise ValueError("没给要删除的职位行号")
    try:
        wanted = list(dict.fromkeys(int(i) for i in ids))
    except (TypeError, ValueError):
        raise ValueError("职位行号得是数字")
    if len(wanted) > DELETE_MAX:
        raise ValueError(f"一次最多删 {DELETE_MAX} 条，你说的这批有 {len(wanted)} 条")
    marks = ",".join("?" * len(wanted))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM jobs WHERE id IN ({marks})", wanted)]
        if not rows:
            raise ValueError("这些行号在库里都找不到")
        keys = [(r["platform"], r["platform_job_id"]) for r in rows]
        cond, args = _key_cond(keys)
        have = _tables(c)
        side = {t: ([dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE {cond}", args)]
                    if t in have else [])
                for t in ("job_details", "job_analysis")}
    # 备份先落盘：万一后面那步删失败，最坏是"没删掉但留了个备份文件"，不会反过来
    batch = _write_trash({"deleted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "jobs": rows, "details": side["job_details"],
                          "analysis": side["job_analysis"]}, trash_dir)
    with _conn() as c:
        for t in ("job_details", "job_analysis"):
            if side[t]:
                c.execute(f"DELETE FROM {t} WHERE {cond}", args)
        c.execute(f"DELETE FROM jobs WHERE id IN ({marks})", wanted)
        # 同时记进屏蔽名单：不记的话第二天定时跑同一个关键词，这批噪音会原样回来。
        if "blocked_jobs" in _tables(c):
            c.executemany(
                "INSERT OR IGNORE INTO blocked_jobs"
                "(platform, platform_job_id, title, company, blocked_at) VALUES(?,?,?,?,?)",
                [(r["platform"], r["platform_job_id"], r.get("title") or "",
                  r.get("company") or "", now) for r in rows])
    return {"batch": batch, "deleted": len(rows),
            "details": len(side["job_details"]), "analysis": len(side["job_analysis"])}


def trash_batches(trash_dir=None) -> list[dict]:
    """回收站里现有哪些批，最新的排前面（页面据此决定给不给"撤销"）。"""
    d = _trash_dir(trash_dir)
    files = sorted(d.glob("*.json"), reverse=True) if d.exists() else []
    out = []
    for f in files:
        if not BATCH_RE.match(f.name):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue          # 单个文件坏了不该让整个列表打不开
        jobs = data.get("jobs") or []
        out.append({"batch": f.name, "deleted_at": data.get("deleted_at") or "",
                    "jobs": len(jobs), "titles": [j.get("title") or "" for j in jobs[:5]]})
    return out


def _restore_rows(c, table: str, cols, rows, have) -> int:
    """把回收站里的行插回去：已存在就跳过，绝不覆盖删除之后新采或新标的数据。"""
    if table not in have:
        return 0
    sql = "INSERT OR IGNORE INTO {}({}) VALUES({})".format(
        table, ",".join(cols), ",".join("?" * len(cols)))
    added = 0
    for r in rows or []:
        if isinstance(r, dict) and r.get("platform") and r.get("platform_job_id"):
            added += c.execute(sql, [r.get(k) for k in cols]).rowcount
    return added


def restore_batch(batch: str, trash_dir=None) -> dict | None:
    """撤销一次删除：把那一批职位放回三张表。批次号用完即废，撤销只有一次机会。"""
    if not BATCH_RE.match(batch or ""):
        raise ValueError("回收站批次号不对")
    path = _trash_dir(trash_dir) / batch  # 批次号已严格校验过，拼不出别的目录
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"回收站那个文件读不出来：{e}")
    n = {}
    with _conn() as c:
        have = _tables(c)
        n["jobs"] = _restore_rows(c, "jobs", RESTORE_JOBS, data.get("jobs"), have)
        n["details"] = _restore_rows(c, "job_details", DETAIL_COLUMNS, data.get("details"), have)
        n["analysis"] = _restore_rows(c, "job_analysis", ANALYSIS_COLUMNS,
                                      data.get("analysis"), have)
        # 撤销 = "这批我反悔了"，屏蔽名单里得跟着退，否则职位虽然回到了库里，
        # 以后每次采集还是被名单拦住，等于撤销只成功了一半。
        if "blocked_jobs" in have:
            c.executemany("DELETE FROM blocked_jobs WHERE platform=? AND platform_job_id=?",
                          [(r["platform"], r["platform_job_id"])
                           for r in (data.get("jobs") or [])
                           if isinstance(r, dict) and r.get("platform")
                           and r.get("platform_job_id")])
    path.unlink(missing_ok=True)
    return {"batch": batch, **n}


def blocked_list() -> list[dict]:
    """屏蔽名单（你在页面上删过的职位），新删的排前面。"""
    with _conn() as c:
        if "blocked_jobs" not in _tables(c):
            return []
        return [dict(r) for r in c.execute(
            "SELECT platform, platform_job_id, title, company, blocked_at FROM blocked_jobs"
            " ORDER BY blocked_at DESC")]


def unblock_jobs(keys) -> int:
    """从屏蔽名单里放掉若干条，参数是 (platform, platform_job_id)；`platform:*` 放整个平台。"""
    removed = 0
    with _conn() as c:
        if "blocked_jobs" not in _tables(c):
            return 0
        for k in keys:
            platform, _, pjid = str(k).partition(":")
            if not platform:
                continue
            if pjid == "*":
                removed += c.execute("DELETE FROM blocked_jobs WHERE platform=?",
                                     (platform,)).rowcount
            else:
                removed += c.execute(
                    "DELETE FROM blocked_jobs WHERE platform=? AND platform_job_id=?",
                    (platform, pjid)).rowcount
    return removed


BLOCK_RULE_KINDS = ("company", "keyword")
BLOCK_VALUE_MAX = 60


def add_block_rule(kind: str, value: str) -> bool:
    """加一条屏蔽规则，返回是不是新增的（重复加返回 False，不报错）。"""
    if kind not in BLOCK_RULE_KINDS:
        raise ValueError(f"不认识的屏蔽规则类型: {kind}（只支持 {'/'.join(BLOCK_RULE_KINDS)}）")
    v = (value or "").strip()[:BLOCK_VALUE_MAX]
    if not v:
        raise ValueError("屏蔽规则的内容不能是空的")
    with _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO blocked_rules(kind, value, created_at) VALUES(?,?,?)",
            (kind, v, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        # 用 rowcount 判"这条是不是这次加进去的"：回查表只会一直返回 True，
        # 因为第一次插入已经把行留在那儿了。
        return cur.rowcount > 0


def remove_block_rule(kind: str, value: str) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM blocked_rules WHERE kind=? AND value=?",
                         (kind, (value or "").strip())).rowcount > 0


def block_rule_list() -> list[dict]:
    with _conn() as c:
        if "blocked_rules" not in _tables(c):
            return []
        return [dict(r) for r in c.execute(
            "SELECT kind, value, created_at FROM blocked_rules ORDER BY kind, created_at DESC")]


def _load_block_rules(c) -> tuple[set[str], list[str]]:
    """连接里读规则，供 save_jobs 一次读完循环用。老库没这张表就是空规则。"""
    if "blocked_rules" not in _tables(c):
        return set(), []
    companies, keywords = set(), []
    for r in c.execute("SELECT kind, value FROM blocked_rules"):
        if r["kind"] == "company":
            companies.add((r["value"] or "").strip())
        elif r["kind"] == "keyword":
            keywords.append((r["value"] or "").strip().lower())
    return companies, [k for k in keywords if k]


def rule_hit(rec: dict, companies: set[str], keywords: list[str]) -> str | None:
    """命中规则就给一句原因，没命中给 None。公司整串相等，关键词是标题子串。"""
    company = (rec.get("company") or "").strip()
    if company and company in companies:
        return f"屏蔽规则：公司「{company}」"
    title = (rec.get("title") or "").lower()
    for k in keywords:
        if k in title:
            return f"屏蔽规则：标题含「{k}」"
    return None


def _salary_to_k(v: float, unit: str | None) -> float:
    if unit == "万":
        return v * 10
    if unit in ("千", "K", "k"):
        return v
    # 裸数字：招聘站的月薪写法要么是个位/十位的 K（'8-12'），要么是元的 '3000-5000'。
    # 不猜就得出 300 万/月，按量级判一下至少不会把筛选台的薪资轴撑爆。
    if unit:
        return v
    return v / 1000 if v >= 500 else v


def parse_salary(raw: str):
    """解析常见薪资文本为 (min_k, max_k, months)，单位：千元/月。

    支持: '15-25K·14薪' '1.2-2.4万' '8千-1.2万' '15-25千' '3000-5000'
          '15K' '15K以上'（单值按下限记，上下限相等——不去猜平台没给的上限）
          '200-300元/天'(返回 None)
    """
    if not raw:
        return None
    raw = raw.strip()
    if "天" in raw or "小时" in raw or "年" in raw:
        return None
    months = 12
    m = re.search(r"[·xX*](\d+)薪", raw)
    if m:
        months = int(m.group(1))
    m = re.match(
        r"(\d+(?:\.\d+)?)\s*(万|千|K|k)?\s*[-~]\s*(\d+(?:\.\d+)?)\s*(万|千|K|k)?", raw
    )
    if m:
        lo, u1, hi, u2 = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4)
        return (_salary_to_k(lo, u1 or u2), _salary_to_k(hi, u2 or u1), months)
    m = re.match(r"(\d+(?:\.\d+)?)\s*(万|千|K|k)?", raw)
    if m:
        v = _salary_to_k(float(m.group(1)), m.group(2))
        return (v, v, months)
    return None
