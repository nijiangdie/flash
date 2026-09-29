"""详情页 HTML 片段 → 纯文本的小工具：正文保住换行，标签列表保住原文。

各平台只负责"从哪一段 HTML 里取"（选择器不同），怎么还原成人看的文本共用这里。
刻意不做技能归一化（那是 skills.py 在读取侧的事），正文要保留平台原文。
"""
import html
import re

_BR_RE = re.compile(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
# 不间断空格和全角空格在中文 JD 里到处都是，一起当空白压掉
_BLANK_RE = re.compile("[ \t\r\f\v\xa0　]+")
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S)


def to_lines(fragment: str, keep_blanks: bool = True) -> str:
    """把一段带标签的正文还原成多行纯文本：换行保住，连续空行并成一个。

    keep_blanks=False 给非 <pre> 的内容用：源码里的缩转换行不是段落，
    只有 <br/> 才是；这种页面留空行只会留出假段落。
    """
    text = _BR_RE.sub("\n", fragment or "")
    text = html.unescape(_TAG_RE.sub("", text))
    out = []
    for line in text.splitlines():
        s = _BLANK_RE.sub(" ", line).strip()
        if s:
            out.append(s)
        elif keep_blanks and out and out[-1]:
            out.append("")
    return "\n".join(out)


def list_items(fragment: str) -> list[str]:
    """取 <li> 列表的文本，按出现顺序去重（关键词标签用）。"""
    tags = []
    for raw in _LI_RE.findall(fragment or ""):
        t = _BLANK_RE.sub(" ", html.unescape(_TAG_RE.sub("", raw))).strip()
        if t and t not in tags:
            tags.append(t)
    return tags
