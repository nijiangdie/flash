"""全局配置：合规限速参数都集中在这里，调大前请三思。"""
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
AUTH_DIR = DATA_DIR / "auth"
DB_PATH = DATA_DIR / "jobs.db"
REPORT_DIR = PROJECT_DIR / "reports"

# ---- 合规限速（强烈建议保持默认） ----
DELAY_RANGE = (3.0, 8.0)      # 每次翻页/请求之间的随机间隔（秒）
MAX_PAGES_DEFAULT = 5          # 单次采集任务的默认页数上限
DAILY_CAP = 200                # 每天全平台采集职位总量熔断（条）
REQUEST_TIMEOUT_MS = 30000

# ---- 采集红线：仅允许存这些公开字段，禁止扩展任何个人信息字段 ----
ALLOWED_FIELDS = {
    "platform", "platform_job_id", "title", "company", "salary_raw",
    "salary_min", "salary_max", "salary_months", "city", "district",
    "experience", "education", "industry", "company_scale",
    "skills", "welfare", "url", "fetched_at",
}
