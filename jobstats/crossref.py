"""跨平台对照：同一家公司在几个平台发了同一个岗位，读取侧算出来，不改库。

判据是"归一后的公司名 + 归一后的标题"，两边都严格对上才算同一条职位。这样只会漏、
不会认错：猎头代招换了主体名、`IT运维工程师` 写成 `运维工程师`，都归不到一起。
所以界面措辞只能是"另见某平台"，不能写成"这个职位重复了 N 份"，更不能据此删行。

2026-10-18 在真实库（409 行）上量的密度：强匹配 2 组、三家都有的 0 组。
稀的根本原因是各平台跑的关键词不一样，不是平台之间没有重复。
"""
import re

from . import db

# 公司名尾巴上那些法律形式和技术后缀，同一家公司经常一边写全一边省略。
# 分组必须整体锚在结尾：`A|B$` 只给最后一项加上了 $，那样 "XX科技(东莞)" 会连着剥掉中段。
COMPANY_SUFFIX = ("股份有限公司", "有限责任公司", "有限公司", "分公司", "集团", "科技", "技术")
_SUFFIX_RE = re.compile("(?:" + "|".join(COMPANY_SUFFIX) + ")$")
# 标题里的括号一般是福利或编号（`IT工程师（包吃住）`、`JO00123`），不参与判据
_PAREN_RE = re.compile(r"[（(].*?[)）]")
_NOISE_RE = re.compile(r"[\s/·\-_—|,，.。#*+()]")


def norm_company(s) -> str:
    """公司名归一：去空格，再一层层剥掉结尾的法律形式和"科技/技术"后缀。

    一层不够——`XX科技有限公司东莞分公司` 得剥两轮才跟另一平台写的 `XX` 对上。
    剥多了只会让两边一起剥，判据是"归一后完全相等"，不会因此认错人。
    """
    c = re.sub(r"\s+", "", str(s or ""))
    while True:
        stripped = _SUFFIX_RE.sub("", c)
        if stripped == c:
            return c
        c = stripped


def norm_title(s) -> str:
    return _NOISE_RE.sub("", _PAREN_RE.sub("", str(s or ""))).lower()


def key(r: dict) -> tuple:
    """这条职位的对照键；公司名或标题归一后为空就不参与对照。"""
    c, t = norm_company(r.get("company")), norm_title(r.get("title"))
    return (c, t) if c and t else ()


def groups(records: list[dict]) -> dict[tuple, list[dict]]:
    """只留下跨平台的组：同一个键必须出现在 2 个以上平台才算。

    同平台内标题重复（同一个职位被两天都采到）本来就会被去重成一行，
    这里再按平台数卡一道，避免把"同一平台的两条相似岗位"当成跨平台对照。
    """
    g: dict[tuple, list[dict]] = {}
    for r in records:
        k = key(r)
        if k:
            g.setdefault(k, []).append(r)
    return {k: v for k, v in g.items() if len({r["platform"] for r in v}) > 1}


def attach(records: list[dict]) -> list[dict]:
    """给每行加一个 `cross`：同组里其他平台的那几条（不含自己）。

    只补读取侧字段，不写库；`has_detail` 用已有的正文键表算，页面据此决定
    是"借正文"还是"去抓正文"。同一组里同平台还有多条时全部列出，让人自己看。
    """
    have = set(db.detail_keys())
    for k, rows in groups(records).items():
        for r in rows:
            r["cross"] = [{"id": o["id"], "platform": o["platform"],
                           "platform_job_id": o["platform_job_id"],
                           "title": o.get("title") or "", "company": o.get("company") or "",
                           "salary_raw": o.get("salary_raw") or "",
                           "city": "·".join(x for x in (o.get("city"), o.get("district")) if x),
                           "url": o.get("url") or "",
                           "seen": (o.get("last_seen_at") or o.get("fetched_at") or "")[:10],
                           "has_detail": f"{o['platform']}:{o['platform_job_id']}" in have}
                          for o in rows if o["id"] != r["id"]]
    for r in records:
        r.setdefault("cross", [])
    return records
