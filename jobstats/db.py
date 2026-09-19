"""SQLite 存储层：职位记录去重入库、每日额度查询、CSV 导出。"""
import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH, ALLOWED_FIELDS

# 入库列顺序（fetched_at 由本层统一赋值）
INSERT_COLUMNS = (
    "platform", "platform_job_id", "title", "company", "salary_raw",
    "salary_min", "salary_max", "salary_months", "city", "district",
    "experience", "education", "industry", "company_scale",
    "skills", "welfare", "url", "fetched_at",
)
# 与 config 的合规白名单保持一致，新增字段时两处需同步
assert set(INSERT_COLUMNS) == ALLOWED_FIELDS, "INSERT_COLUMNS 与 ALLOWED_FIELDS 不一致"

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
    UNIQUE(platform, platform_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at);
CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
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


def count_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        try:
            return c.execute(
                "SELECT COUNT(*) FROM jobs WHERE fetched_at LIKE ?", (today + "%",)
            ).fetchone()[0]
        except sqlite3.OperationalError:  # 表尚未创建
            return 0


def fetch_all() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY fetched_at DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["skills"] = json.loads(d["skills"] or "[]")
        d["welfare"] = json.loads(d["welfare"] or "[]")
        out.append(d)
    return out


def export_csv(path):
    fields = ["platform", "title", "company", "salary_raw", "city", "district",
              "experience", "education", "industry", "company_scale", "url", "fetched_at"]
    with _conn() as c, open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for row in c.execute(f"SELECT {','.join(fields)} FROM jobs ORDER BY fetched_at DESC"):
            w.writerow(row)


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
