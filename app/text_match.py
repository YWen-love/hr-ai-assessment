"""中文短文本的字面证据匹配（无分词器依赖）。

为什么需要它：项目里没有引入分词器，也不想为了"简历里有没有教培销售经验"
这种判断背上一个 jieba 依赖。bigram（2 字滑窗）能可靠命中复合词里的核心词素：
"师范院校" 能对上 "汉语言文学（师范）"，整词匹配则完全对不上。

两处共用同一份实现，避免日后各自漂移：
1. 简历解析 —— 用它从 exp_keywords 封闭词表里捞出有字面依据的 exp_tags
   （allow_partial=False，参与打分，必须精确）
2. 备选岗映射佐证 —— app/report.py 的 keyword_hints()
   （allow_partial=True，仅提示性，允许召回）
"""

from __future__ import annotations

import re
from typing import Iterable, List

# 常见分隔符 + 标点，用于把长句切成候选 token
_TOKEN_SPLIT_RE = re.compile(r"[\s、，,。;；:：!！?？~～/\\|()（）\[\]【】<>《》“”\"'`\-—–]+")

# 连词。只有"匹配条件"这类自然语言短语才需要按它切分 ——
# 专业名"计算机和软件工程"里的"和"是名字的一部分，切了反而错。
_CONJUNCTION_RE = re.compile(r"(?:以及|和|或)")


def normalize(text: str) -> str:
    """归一化：全角转半角、去除多余空白，统一大小写。

    简历里 "ＣＡＴ" 和 "CAT" 指的是同一所学校，"本科" 和 "本 科" 也是同一件事。
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        # 全角 ASCII 区（U+FF01–U+FF5E）转半角
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:  # 全角空格
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def tokenize(text: str, *, min_len: int = 2, split_conjunctions: bool = False) -> List[str]:
    """按分隔符切词，过滤掉过短的碎片。

    split_conjunctions=True 时额外按"和/或/以及"切分，
    用于"实习经历为授课、代课、主讲"这类枚举式匹配条件。
    """
    pieces = _TOKEN_SPLIT_RE.split(normalize(text))
    if split_conjunctions:
        pieces = [p for piece in pieces for p in _CONJUNCTION_RE.split(piece)]

    return [
        token.strip()
        for token in pieces
        if len(token.strip()) >= min_len
    ]


def has_trace(token: str, haystack: str, *, allow_partial: bool = True) -> bool:
    """token 在 haystack 里是否有字面依据。

    allow_partial=True（默认，仅用于【提示性】场景）：
        整词命中，或任一二字词素命中。用于备选岗匹配佐证 ——
        那里多给一条线索无害，漏掉才是问题。

    allow_partial=False（用于【参与打分】的场景）：
        只认整词命中。

    为什么不一律放宽：实测踩过很疼的坑。岗位词表里
    "课程顾问 / 电话销售 / 教育培训" 在一份小学教师简历上全部命中 ——
    分别靠 "求职意向：学习**顾问**"、"联系**电话**"、"小学**教育**"
    这三个无关片段。命中后 exp_edu_sales 规则直接加 3 分，
    一份毫无销售经历的简历被抬进中匹配档。

    结论：**参与打分的匹配必须高精度，只认整词**；
    语义召回交给 LLM 通道（同样受封闭词表约束），
    少命中几个关键词可以靠模型补，误加几分没人能补救。
    """
    token = normalize(token)
    haystack = normalize(haystack)
    if len(token) < 2 or not haystack:
        return False
    if token in haystack:
        return True
    if not allow_partial:
        return False
    return any(token[i : i + 2] in haystack for i in range(len(token) - 1))


def find_hits(tokens: Iterable[str], haystack: str, *, allow_partial: bool = True) -> List[str]:
    """批量判定，返回保持原顺序的去重命中表。"""
    hits: List[str] = []
    seen = set()
    for token in tokens:
        cleaned = (token or "").strip()
        if not cleaned or cleaned in seen:
            continue
        if has_trace(cleaned, haystack, allow_partial=allow_partial):
            seen.add(cleaned)
            hits.append(cleaned)
    return hits