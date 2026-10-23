"""全局配置：合规限速参数都集中在这里，调大前请三思。"""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
TRASH_DIR = DATA_DIR / "trash"      # 删除前的原始行落这儿，页面上的"撤销"靠它
# 页面里那些采集任务的日志同时写一份在这儿（按天一个文件）。不写盘的话，
# 服务一重启昨晚那次为什么 0 条就再也查不到了。
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "jobs.db"
REPORT_DIR = PROJECT_DIR / "reports"

# 本机用户级的数据根，项目外的东西都挂在这儿（登录凭证、专用 Edge 配置）。
_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")

# ---- 登录凭证存放地址 ----
# 默认放在项目外（和专用 Edge 配置目录并排），这样项目文件夹里任何时候都只有代码：
# 打包、拷给别人、顺手传网盘都不会把 Boss 登录态一起带出去。
# 想放别处就设 JOBSTATS_AUTH_DIR（用 setx 设成用户变量：采集和 serve 也得读到同一个位置，
# 只在某个 bat 里 set 会让登录和采集指向两个目录，等于白扫一次码）。
# 别设到网盘/同步目录或网络盘上：那是登录凭证，同步一份就等于多一台机器能登你的号。
_AUTH_DIR_ENV = os.environ.get("JOBSTATS_AUTH_DIR", "").strip()
AUTH_DIR = Path(_AUTH_DIR_ENV).expanduser() if _AUTH_DIR_ENV \
    else _LOCAL_APP_DATA / "job-stats" / "auth"

# ---- 合规限速（强烈建议保持默认） ----
DELAY_RANGE = (3.0, 8.0)      # 每次翻页/请求之间的随机间隔（秒）
MAX_PAGES_DEFAULT = 5          # 单次采集任务的默认页数上限
DAILY_CAP = 200                # 每天全平台采集职位总量熔断（条）
# 抓职位正文（JD）是逐条访问详情页，比列表页更容易触发风控，所以单独一个更小的额度。
DETAIL_DAILY_CAP = 20          # 每天最多抓多少条职位正文
REQUEST_TIMEOUT_MS = 30000

# ---- "疑似已下架"的推断阈值 ----
# 平台不会告诉你某条职位下架了，只有"最后一次采到它是什么时候"这个间接证据。
# 超过这么多天没再见到，筛选台就标成"疑似已下架"（见 db.py 的 last_seen_at）。
# 这是推断，不是平台给的状态，页面上也是这么写的；设成 0 等于把全部老数据都标灰。
STALE_AFTER_DAYS = 14

# ---- 真实浏览器通道（CDP 接管）----
# Boss直聘 会把 Playwright 自建的浏览器页面清成 about:blank，改用本机真实 Edge + 调试端口接管。
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CDP_PORT = 9223                              # 只监听 127.0.0.1，不要做端口转发
# 这条通道专用的独立配置目录，不读写你平时用的 Edge 资料。
# 刻意放在项目外：Edge 会往里塞组件和缓存，实测轻松上百 M，没必要跟着工程走。
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

# ---- 本地大模型（LM Studio 的 OpenAI 兼容接口）----
# 只连本机的推理端口，职位文本和你的自述都不出这台机器；换成云端 API 需要先想清楚。
LLM_BASE_URL = os.environ.get("JOBSTATS_LLM_URL") or "http://127.0.0.1:1234/v1"
# 留空就用 LM Studio 当前已加载的那个模型；填了则以填的为准（要和 /v1/models 里的 id 一致）。
LLM_MODEL = os.environ.get("JOBSTATS_LLM_MODEL", "")
LLM_TIMEOUT_S = 180        # 本地 9B 跑 CPU/GPU 混合都可能慢，给足超时，但绝不无限等
LLM_TEMPERATURE = 0.0      # 打分要复现性，温度钉死在 0
JD_MAX_CHARS = 2000        # 送进模型的正文上限：本地模型上下文有限，也从源头防止一次塞几百条
ANALYZE_BATCH_DEFAULT = 20  # 单次默认分析条数（纯本地推理，不吃采集额度，只是慢）
# 匹配度的尺子是你自己的情况，写在本地文件里，不进库、不进导出、不喂给任何平台。
ANALYSIS_PROFILE_PATH = DATA_DIR / "profile.txt"
