"""入库前的相关性闸门：搜索页给的不全是关键词结果，这一步在本机把它们挡在库外。

实测来源（2026-10-12 拿 job5156 的 IT@东莞 结果页，2026-10-17 拿 IT运维@东莞 的 41 条智通入库）：

1. 智通把**置顶推广位**混在列表同一个 `<ul>` 里（`data-top="true"`，实测前 5 条是
   焊工/CNC编程/总经理助理，和关键词毫无关系），所以由解析器自己丢掉。
2. 传了 `city=东莞`，平台照样返回**别的城市**（同一页里出现惠州、甚至广西贺州）。
   既然命令里写了城市，就以命令为准，别让"周边推荐"进库；真想看全部就传 全国。
3. 关键词太短时平台按**子串**命中：搜 `IT` 会带出 `TITOK运营`、`FIT模师傅`。
   所以只给 ≤3 个字母的短词加词边界判断；长词（python、软件开发）不动——
   实测那批搜索返回的都是对口岗位，加规则反而会误杀标题里没写语言的岗位。
4. 搜 `IT运维` 这种带中文的关键词，上面第 3 条完全不生效（它只管纯字母短词），
   于是"锅炉操作工、污水处理操作员、物业项目负责人"一路进库：实测 41 条里 11 条是这类。
   平台其实把方向给了——智通每条岗位带 `一级行业-二级职类`（二级是**职能**，
   不是公司行业：锅炉操作工标的是"生产制造-技工"），拿它当判据比猜标题靠谱。
   只有肯给这个字段、且给的是"职能"而非"公司行业"的通道才启用（`FAMILY_GATE`）：
   Boss 的 `industry` 装的是公司行业（IT运维岗标"环保"），拿它判方向会误杀。
5. 还有一类词表判得动：标题本身就是一线操作岗/跨行业运维的说法（普工、模师傅、
   光伏运维）。这些跟关键词无关，装的不是"匹不匹配"，是"这条不是办公室技术岗"。

判据一律放宽不放严：标题命中查询词、或者平台职类是技术方向，任一成立就放行。
被拦下的标题会连原因一起打印并落库（`cli.py runs`），误杀了一眼能看到。
"""
import re

# 这些取值表示"不限城市"，闸门里的城市规则对它们不生效
WILDCARD_CITIES = ("", "全国", "全部", "不限")
# ≤3 个字母的纯 ASCII 短词才做词边界判断（it / ai / qa / web / ios ...）
_SHORT_ASCII_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+#.]{0,2}$")
_CITY_SUFFIX_RE = re.compile(r"(市|地区|自治州)$")
_ASCII_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# 平台标的二级职类里，算"技术方向"的说法。这里只做放行，不做拦截，写宽点安全。
TECH_FAMILIES = ("运维", "技术支持", "开发", "测试", "数据", "人工智能", "算法", "架构",
                 "技术项目管理", "高端技术", "网络", "安全", "信息安全", "IT")
# 一看就不是一线以外的办公室技术岗？反过来：这些词出现在标题里就是蓝岗/跨行业运维。
# 智通 2026-10-17 那页实测到的：模师傅、装卸搬运工、洗水杂工、质检员、叉车司机、
# 煮饭阿姨、丝印/移印技工、锅炉操作工、空调维修工、污水处理操作员、储备干部、光伏运维1。
BLUE_COLLAR = ("普工", "杂工", "操作工", "作业员", "技工", "师傅", "搬运", "装卸", "叉车",
               "保洁", "保安", "阿姨", "煮饭", "后厨", "钳工", "焊工", "电镀", "喷涂",
               "分拣", "流水线", "仓管", "快递", "外卖", "货车", "管培生", "储备干部",
               "模切", "丝印", "移印", "磨床", "打磨", "搓牙", "注塑", "压铸", "模具",
               "光伏", "污水", "锅炉", "空调", "电梯", "暖通")
# 一线岗位爱写进标题的福利/兼岗说法。单发不拦——实测 "IT工程师（包吃住）"、
# "IT专员/IT主管（包吃住+双休+五险一金）"、"IT技术支持兼行政司机" 是真 IT 岗；
# 两个同时出现才拦，那是 "7000-9000技术员包吃住 五险一金长白班运维" 的写法。
BLUE_HINTS = ("包吃住", "长白班", "两班倒", "司机", "跟单", "业务员", "学徒")


def _norm_city(name: str) -> str:
    return _CITY_SUFFIX_RE.sub("", (name or "").strip())


def city_ok(scraped: str, wanted: str) -> bool:
    """页面写的城市 vs 命令里传的城市。抓不到城市就不拦（缺数据不该当成不符）。"""
    if not wanted or wanted.strip() in WILDCARD_CITIES:
        return True
    a, b = _norm_city(scraped), _norm_city(wanted)
    return not a or a == b


def _ascii_terms(query: str) -> list[str]:
    return _ASCII_RUN_RE.findall(query or "")


def _cjk_grams(query: str) -> list[str]:
    """中文部分切成二字组：'软件开发' -> 软件/件开/开发。

    二字组是刻意的：查询写"软件开发"，标题常写"后端开发工程师"，整串相等判不出来，
    二字组能接住；反过来"运维"接不住"光伏运维"以外的写法，但那是词表管的。
    """
    out = []
    for run in _CJK_RUN_RE.findall(query or ""):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _texts(record: dict) -> tuple[str, list[str]]:
    title = record.get("title") or ""
    tags = [s for s in (record.get("skills") or []) if isinstance(s, str)]
    return title, tags


def keyword_ok(record: dict, query: str) -> bool:
    """短 ASCII 关键词得真的出现在这条职位上，不接受被埋在一个更长的英文单词里。

    标题按词边界判（`TITOK`、`FIT模师傅` 就是这么混进 IT 搜索结果的）；
    技能标签放宽成子串判——那是公司自己打的标签，`ITIL` 这类缩写是真相关的，
    按词边界反而会误杀。长词、中文词不做字面剔除（见模块文档第 3 条）。
    """
    q = (query or "").strip()
    if not q or not _SHORT_ASCII_RE.match(q):
        return True
    pat = rf"(?<![A-Za-z0-9]){re.escape(q)}(?![A-Za-z0-9])"
    if re.search(pat, record.get("title") or "", re.I):
        return True
    needle = q.lower()
    return any(needle in s.lower() for s in (record.get("skills") or [])
               if isinstance(s, str))


def query_hits(record: dict, query: str) -> bool:
    """查询词（英文单词按词边界 + 中文二字组按子串）有没有出现在这条岗位上。

    只当放行用：命中就 leave alone，不命中不代表不相关（"程序员"里没 IT 也没运维）。
    """
    title, tags = _texts(record)
    low_title, low_tags = title.lower(), [t.lower() for t in tags]
    for term in _ascii_terms(query):
        pat = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
        if re.search(pat, title, re.I):
            return True
        needle = term.lower()
        if any(needle in t for t in low_tags):
            return True
    for gram in _cjk_grams(query):
        if gram in title or any(gram in t for t in low_tags):
            return True
    return False


def job_family(record: dict) -> str:
    """平台给的职类方向。智通的 industry 是「一级行业-二级职类」，取第二段。"""
    industry = (record.get("industry") or "").strip()
    if "-" not in industry:
        return ""
    return industry.split("-")[-1].strip()


def family_ok(record: dict) -> bool:
    fam = job_family(record)
    # 没这个字段（前程无忧列表不给 / Boss 不给职类）就不判，缺数据不等于不符
    if not fam:
        return True
    return any(k in fam for k in TECH_FAMILIES)


def blue_hit(record: dict, query: str = "") -> bool:
    """标题像一线操作岗/跨行业运维。判的是"这条不是办公室技术岗"，不是"这条不匹配关键词"。

    只认两种写法：硬词（普工、模师傅、光伏运维）单发就拦；福利/兼岗说法得两个同时出现。
    单个"包吃住"不能当证据——库里实测有 "IT工程师（包吃住）"、"IT技术支持兼行政司机"、
    "IT专员/IT主管（包吃住+双休+五险一金）"，那都是真 IT 岗（厂里 IT 也包吃住）。
    "7000-9000技术员包吃住 五险一金长白班运维" 是两个说法一起出现，拦得住。
    """
    title = record.get("title") or ""
    if any(w in title for w in BLUE_COLLAR):
        return True
    return sum(1 for h in BLUE_HINTS if h in title) >= 2


def reject_reason(record: dict, query: str = "", city: str = "", family: bool = False):
    """留下返回 None，丢掉给一句原因（直接打给用户看，别写成黑话）。"""
    if not city_ok(record.get("city") or "", city):
        return "城市不符"
    if not keyword_ok(record, query):
        return "标题没写这个关键词"
    if blue_hit(record, query):
        return "标题像一线操作岗"
    if family and not (query_hits(record, query) or family_ok(record)):
        return "平台职类不是这个方向"
    return None


def _label(record: dict) -> str:
    return (record.get("title") or "").strip() or record.get("platform_job_id") or "（无标题）"


def gate(records: list[dict], query: str = "", city: str = "", family: bool = False):
    """筛掉不相关的；返回 (留下的, {原因: [被拦标题]})。

    被拦的是哪些标题一定要看得见——这道闸门是入库前判的，库里没痕迹，
    误杀了除了这里没人会告诉你。
    """
    kept: list[dict] = []
    dropped: dict[str, list[str]] = {}
    for rec in records:
        why = reject_reason(rec, query, city, family)
        if why is None:
            kept.append(rec)
        else:
            dropped.setdefault(why, []).append(_label(rec))
    return kept, dropped


def count(dropped: dict) -> int:
    return sum(len(v) for v in (dropped or {}).values())


def merge(dropped: dict, target: dict) -> None:
    """把一批 gate 结果并进累计字典（翻页采集每页一次）。"""
    for why, titles in (dropped or {}).items():
        target.setdefault(why, []).extend(titles)


def titles(dropped: dict, limit: int = 6) -> str:
    """"锅炉操作工（平台职类不是这个方向）、…"：给采集回执和留痕用的明细。"""
    out = []
    for why, names in (dropped or {}).items():
        for name in names:
            out.append(f"{name}（{why}）")
    shown = "、".join(out[:limit])
    return shown + (f"…共 {len(out)} 条" if len(out) > limit else "")


def summarize(dropped: dict) -> str:
    if not dropped:
        return ""
    return "、".join(f"{k} {len(v)} 条"
                     for k, v in sorted(dropped.items(), key=lambda x: -len(x[1])))
