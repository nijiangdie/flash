"""全局配置：合规限速参数都集中在这里，调大前请三思。"""
import os
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
# 抓职位正文（JD）是逐条访问详情页，比列表页更容易触发风控，所以单独一个更小的额度。
DETAIL_DAILY_CAP = 20          # 每天最多抓多少条职位正文
REQUEST_TIMEOUT_MS = 30000

# ---- 真实浏览器通道（CDP 接管）----
# Boss直聘 会把 Playwright 自建的浏览器页面清成 about:blank，改用本机真实 Edge + 调试端口接管。
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CDP_PORT = 9223                              # 只监听 127.0.0.1，不要做端口转发
# 这条通道专用的独立配置目录，不读写你平时用的 Edge 资料。
# 刻意放在项目外：Edge 会往里塞组件和缓存，实测轻松上百 M，没必要跟着工程走。
_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
CDP_PROFILE_DIR = _LOCAL_APP_DATA / "job-stats" / "edge-profile"

# ---- 采集红线：仅允许存这些公开字段，禁止扩展任何个人信息字段 ----
ALLOWED_FIELDS = {
    "platform", "platform_job_id", "title", "company", "salary_raw",
    "salary_min", "salary_max", "salary_months", "city", "district",
    "experience", "education", "industry", "company_scale",
    "skills", "welfare", "url", "fetched_at",
}

# 职位正文单独一张表、单独一份白名单：正文很长，混进 jobs 会把筛选台页面撑爆。
# 同样只允许公开职位内容，HR 姓名/头像/联系方式一类一律不落地。
ALLOWED_DETAIL_FIELDS = {
    "platform", "platform_job_id", "description", "keyword_tags", "url", "fetched_at",
}
