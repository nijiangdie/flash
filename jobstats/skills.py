"""职位标签口径统一：三家平台往 skills 里塞的东西完全不一样。

实测（data/jobs.db）：智联的 skills 基本为空、51job 塞的是福利、Boss 连学历和年限都往里放，
所以"技能排行"以前第一名是五险一金。这里在**读取侧**统一一次，入库仍存平台原文，
好处是已采的历史数据和以后采的用同一套口径，不用重采。
"""
import re

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
