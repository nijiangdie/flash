"""SQLite 存储层：职位记录去重入库、每日额度查询、CSV 导出。"""
import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import skills
from .config import ALLOWED_DETAIL_FIELDS, DB_PATH, ALLOWED_FIELDS

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
    note TEXT,
    UNIQUE(platform, platform_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at);
CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);

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
        for col in ("status", "note"):
            if col not in cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")


def save_jobs(records: list[dict]) -> int:
    """入库（按 platform+job_id 去重，已存在则跳过）。返回新增条数。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sql = "INSERT OR IGNORE INTO jobs({}) VALUES({})".format(
        ",".join(INSERT_COLUMNS), ",".join("?" * len(INSERT_COLUMNS)))
    added = 0
    with _conn() as c:
        for r in records:
            # 合规校验：只允许白名单字段
            bad = set(r) - ALLOWED_FIELDS
            if bad:
                raise ValueError(f"检测到白名单之外的字段: {bad}，已拒绝入库")
            row = dict(r, fetched_at=now)
            row["skills"] = json.dumps(r.get("skills") or [], ensure_ascii=False)
            row["welfare"] = json.dumps(r.get("welfare") or [], ensure_ascii=False)
            added += c.execute(sql, [row.get(k) for k in INSERT_COLUMNS]).rowcount
    return added


def set_status(job_id: int, status: str | None = None, note: str | None = None) -> dict | None:
    """标注投递进度。传 None 的那一列保持不动，改哪列由调用方决定。

    职位被重新采集时走的是 INSERT OR IGNORE（已存在就整行跳过），所以这里写进去的
    状态和备注不会被下一次采集冲掉。
    """
    if status is None and note is None:
        return None
    if status is not None and status not in STATUS_LABELS:
        raise ValueError(f"不认识的投递状态: {status}")
    sets, args = [], []
    if status is not None:
        sets.append("status=?")
        args.append(status)
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


def jobs_missing_details(platform: str | None = None, limit: int = 20) -> list[dict]:
    """挑还没抓过正文、且留有详情页链接的职位，按最近采集优先。"""
    sql = """SELECT j.* FROM jobs j
             LEFT JOIN job_details d
               ON d.platform = j.platform AND d.platform_job_id = j.platform_job_id
             WHERE d.platform IS NULL AND IFNULL(j.url, '') <> ''"""
    args: list = []
    if platform:
        sql += " AND j.platform = ?"
        args.append(platform)
    sql += " ORDER BY j.fetched_at DESC LIMIT ?"
    args.append(max(1, limit))
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def fetch_all() -> list[dict]:
    """读出全部职位，技能/福利按读取侧口径清洗过（库里存的仍是平台原文）。"""
    with _conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY fetched_at DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        # 迁移前的老行这两列是 NULL，统一成空字符串，页面和导出都不用防 null
        d["status"] = d.get("status") or ""
        d["note"] = d.get("note") or ""
        raw_skills = json.loads(d["skills"] or "[]")
        # 51job/Boss 把福利词塞在技能里，挪回福利列，两边都不丢信息
        moved = [s for s in raw_skills if skills.is_welfare(s)]
        d["skills"] = skills.clean(raw_skills)
        d["welfare"] = skills.welfare(json.loads(d["welfare"] or "[]") + moved)
        out.append(d)
    return out


def annual_wan(rec: dict) -> float | None:
    """年薪估算（万元/年）：月薪中位数 × 发薪月数，平台没写月数按 12 薪算。"""
    lo, hi = rec.get("salary_min"), rec.get("salary_max")
    if not lo or not hi:
        return None
    return round((lo + hi) / 2 * (rec.get("salary_months") or 12) / 10, 1)


# 导出列：数值薪资和年薪放一起，Excel 里能直接排序，也是以后喂给 AI 的字段
# status/note 是自己标的投递进度，导出后就是一份投递清单；正文放最后一列，免得挡住前面的
EXPORT_FIELDS = ["platform", "platform_job_id", "title", "company", "salary_raw",
                 "salary_min", "salary_max", "salary_months", "salary_annual_wan",
                 "city", "district", "experience", "education", "industry",
                 "company_scale", "skills", "welfare", "status", "note",
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


def parse_salary(raw: str):
    """解析常见薪资文本为 (min_k, max_k, months)，单位：千元/月。

    支持: '15-25K·14薪' '1.2-2.4万' '8千-1.2万' '15-25千' '200-300元/天'(返回 None)
    """
    import re
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
    if not m:
        return None
    lo, u1, hi, u2 = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4)

    def to_k(v, unit):
        unit = unit or "K"  # 无单位时默认按 K 处理
        return v * 10 if unit == "万" else v

    return (to_k(lo, u1 or u2), to_k(hi, u2 or u1), months)
