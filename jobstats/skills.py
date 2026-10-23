"""职位标签口径统一：三家平台往 skills 里塞的东西完全不一样。

实测（data/jobs.db）：智联的 skills 基本为空、51job 塞的是福利、Boss 连学历和年限都往里放，
所以"技能排行"以前第一名是五险一金。这里在**读取侧**统一一次，入库仍存平台原文，
好处是已采的历史数据和以后采的用同一套口径，不用重采。
"""
import re

# 有的平台**列表页就没有技能这一列**——那 238 条智通的 skills 全是空不是解析失败，
# 是页面不提供。这句话得由代码来说，不能让界面把"没数据"显示成"这职位没要求"。
# 实测（2026-10-18，一页 30 条）：智通列表的 tag-list-item 只有
# `城市-镇区 / 学历 / 经验 / 公司性质 / 公司规模 / 行业` 六类，一个技能词都没有；
# 技能词在详情页的 `<ul class="keyword-tags">` 里，抓过正文才有（见 db.fetch_all 的兜底）。
NO_LIST_SKILLS = {
    "zhitong": "智通的列表页没有技能标签这一列（实测：一页 30 条的标签只有城市-镇区/学历/经验/"
               "公司性质/公司规模/行业）。技能词写在详情页的职位关键词里，抓过正文才会出现在这里。",
}

# 规范写法：小写键 -> 展示名。缩写统一大写，专有名词按官方写法。
ALIASES = {
    "python": "Python", "py": "Python", "python/shell": "Python/Shell",
    "java": "Java", "javascript": "JavaScript", "js": "JavaScript",
    "typescript": "TypeScript", "ts": "TypeScript",
    "html": "HTML", "css": "CSS", "json": "JSON", "api": "API", "sop": "SOP",
    "sql": "SQL", "mysql": "MySQL", "mysql数据库": "MySQL", "oracle": "Oracle",
    "mongodb": "MongoDB", "redis": "Redis",
    "linux": "Linux", "windows": "Windows", "web": "Web",
    "docker": "Docker", "nginx": "Nginx", "apache": "Apache", "vpn": "VPN",
    "django": "Django", "flask": "Flask", "pandas": "pandas",
    "tensorflow": "TensorFlow", "selenium": "Selenium", "matlab": "MATLAB",
    "ai": "AI", "bi": "BI", "etl": "ETL", "idc": "IDC", "agv": "AGV",
    "erp": "ERP", "oa": "OA", "mes": "MES", "dms": "DMS", "plc": "PLC",
    "cad": "CAD", "solidworks": "SolidWorks", "ps": "PS",
    "cpp": "C/C++",
    "英文": "英语", "英语读写": "英语",
}

# 福利同义词合并：各平台叫法不同，统计时算一个
WELFARE_ALIASES = {
    "年终奖": "年终奖金", "绩效奖": "绩效奖金", "项目奖": "项目奖金",
    "社会五险": "五险", "养老保险": "五险", "医疗保险": "五险", "失业保险": "五险",
    "工伤保险": "五险", "生育保险": "五险",
    "周末双休": "双休", "做五休二": "双休",
    "年假": "带薪年假", "带薪休假": "带薪年假", "福利年假": "带薪年假",
    "假日福利": "节日福利",
    "午餐补贴": "餐补", "餐饮补贴": "餐补", "员工餐厅": "餐补",
    "有食堂": "餐补", "提供午饭": "餐补",
    "宿舍": "包住", "员工宿舍": "包住", "提供住宿": "包住",
    "有住宿": "包住", "包食宿": "包吃包住",
    "房补": "住房补贴", "租房补贴": "住房补贴",
    "话补": "通讯补贴", "话费补助": "通讯补贴",
    "交通补助": "交通补贴",
    "加班费": "加班补助",
    "团建聚餐": "定期团建", "团队活动丰富": "定期团建",
    "年度旅游": "员工旅游", "免费旅游": "员工旅游",
    "免费体检": "定期体检", "免费培训": "专业培训",
    "公司下午茶": "零食下午茶",
    "节假日加班费": "加班补助",
    "弹性工作时间": "弹性工作",
}

# 出现在技能列里时应挪到福利列的词（挪完后这个集合会自动覆盖大部分常见叫法）
WELFARE_WORDS = ({"五险一金", "六险一金", "补充医疗保险", "补充公积金", "商业保险",
                  "股票期权", "意外险", "五险", "全勤奖", "工龄奖",
                  "季度奖", "高提成", "保底工资", "底薪加提成", "项目提成", "生日福利",
                  "生日礼物", "子女福利", "开工利是", "免费工装", "免费班车", "免费健身房",
                  "8小时工作制", "双休", "单休", "单双休", "法定假日", "法定节假日三薪",
                  "带薪年假", "带薪病假", "节日福利", "员工旅游", "定期体检", "定期团建",
                  "专业培训", "绩效奖金", "年终奖金", "项目奖金", "通讯补贴", "出差补贴",
                  "交通补贴", "高温补贴", "夜班补助", "零食下午茶", "弹性工作",
                  "包吃", "包住", "包吃包住", "餐补", "有无线网",
                  "工作环境优美", "交通便利", "氛围欢乐", "发展平台", "晋升"}
                 | set(WELFARE_ALIASES) | set(WELFARE_ALIASES.values()))

# 与 education / experience 列完全重复的标签，技能列里没信息量
EDU_WORDS = {"大专", "本科", "硕士", "博士", "中专", "中技", "高中", "初中",
             "学历不限", "在校/应届", "应届生"}
_EXP_RE = re.compile(
    r"^(在校|应届|\d+\s*[-–~到]\s*\d+\s*年|\d+\s*年(以内|以上)|经验不限|无经验)")


def canonical(tag: str) -> str:
    """一个标签 -> 规范写法；返回空串表示这条该丢掉。"""
    t = (tag or "").strip()
    if not t:
        return ""
    key = t.lower()
    name = ALIASES.get(key) or WELFARE_ALIASES.get(t) or t
    if name in WELFARE_WORDS or name in EDU_WORDS or _EXP_RE.match(name):
        return ""
    return name


def clean(tags) -> list[str]:
    """清洗一组标签：去噪 + 归一 + 保序去重。"""
    out, seen = [], set()
    for tag in tags or []:
        name = canonical(tag)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def welfare(tags) -> list[str]:
    """福利列清洗：合并同义词叫法并去重。"""
    out, seen = [], set()
    for tag in tags or []:
        t = (tag or "").strip()
        if not t:
            continue
        name = WELFARE_ALIASES.get(t) or t
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def is_welfare(tag: str) -> bool:
    """技能列里的这个词其实是福利，该并进福利列。"""
    t = (tag or "").strip()
    return t in WELFARE_WORDS or WELFARE_ALIASES.get(t) in WELFARE_WORDS


# ---- 经验 / 学历口径 ----
# 实测 data/jobs.db 里 experience 一个字段有 47 种写法：`经验经验不限`(61)、`经验不限`(4)、
# `无需经验`(10) 是同一件事，`3年及以上`(33) 和 `经验3年及以上`(26) 也是（三家平台各自加了
# 个"经验"前缀）。全列成筛选项就没人会用，所以跟技能一样在**读取侧**归成固定几档，
# 库里存的仍是平台原文，导出 CSV 也仍是原文。
#
# 归档看的是**最低**要求：`3-10年`、`3年及以上`、`3-5年` 都进 `3-5年`——因为"这个岗位门槛
# 是几年"这条线在区间底下，投不投得进去也只取决于它。上限不参与（`经验5-8年` 和
# `经验5年及以上` 对求职者是同一类）。
EXP_BUCKETS = ("经验不限", "应届/在校", "1年以内", "1-3年", "3-5年", "5-10年", "10年以上",
               "未标注", "其他")
EDU_BUCKETS = ("学历不限", "初中及以下", "高中/中专", "大专", "本科", "硕士及以上",
               "未标注", "其他")

_EXP_NUM = re.compile(r"\d+(?:\.\d+)?")
_NO_LIMIT = ("不限", "无需经验", "无经验", "零经验")
_FRESH = ("应届", "在校", "在读", "实习", "毕业生")


def _exp_band(years: float) -> str:
    if years < 1:
        return "1年以内"
    if years < 3:
        return "1-3年"
    if years < 5:
        return "3-5年"
    if years < 10:
        return "5-10年"
    return "10年以上"


def exp_bucket(raw) -> str:
    """平台写的经验要求 → 固定档位。判据是区间下限，`1年以内` 这类上限写法单独处理。"""
    s = (raw or "").strip()
    if not s:
        return "未标注"
    if any(w in s for w in _NO_LIMIT):
        return "经验不限"
    if any(w in s for w in _FRESH):
        return "应届/在校"
    nums = [float(x) for x in _EXP_NUM.findall(s)]
    if not nums:
        return "其他"
    lo = min(nums)
    # "以内/以下"是上限说法（`1年以内` 不等于 `1年及以上`）；"以上"不是，别误伤 `10年以上`
    if "以内" in s or ("以下" in s and "以上" not in s):
        return "1年以内" if lo <= 1 else _exp_band(lo - 1)
    return _exp_band(lo)


def edu_bucket(raw) -> str:
    """平台写的学历要求 → 固定档位。`中专`/`中专/中技`/`中技/中专` 是同一种，合成一档。"""
    s = (raw or "").strip()
    if not s:
        return "未标注"
    if "不限" in s:
        return "学历不限"
    for words, name in ((("博士", "硕士", "研究生"), "硕士及以上"),
                        (("本科", "学士"), "本科"),
                        (("大专", "专科"), "大专"),
                        (("初中", "小学"), "初中及以下"),
                        (("高中", "中专", "中技", "职高", "技校", "中职"), "高中/中专")):
        if any(w in s for w in words):
            return name
    return "其他"


def add_buckets(rec: dict) -> dict:
    """给一行职位挂上 experience_bucket / education_bucket，原字段一个字符都不改。"""
    rec["experience_bucket"] = exp_bucket(rec.get("experience"))
    rec["education_bucket"] = edu_bucket(rec.get("education"))
    return rec
