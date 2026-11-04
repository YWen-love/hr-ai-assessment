"""中文短文本的字面证据匹配（无分词器依赖）。

为什么需要它：项目里没有引入分词器，也不想为了"简历里有没有教培销售经验"
这种判断背上一个 jieba 依赖。bigram（2 字滑窗）能可靠命中复合词里的核心词素：
"师范院校" 能对上 "汉语言文学（师范）"，整词匹配则完全对不上。

两处共用同一份实现，避免日后各自漂移：
1. 简历解析 —— 用它从 exp_keywords 封闭词表里捞出有字面依据的 exp_tags
   （find_hits_spanning，参与打分，必须精确）
2. 备选岗映射佐证 —— app/report.py 的 keyword_hints()
   （allow_partial=True，仅提示性，允许召回）
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set, Tuple

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
    这三个无关片段。命中后 exp_course_sales 规则直接加 2 分，
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


def find_hits_spanning(tokens: Iterable[str], haystack: str, *, min_len: int = 2) -> List[str]:
    """长词优先占位：词表里互相包含的词不会同时命中。

    find_hits 是"每个词各自判断在不在"，所以词表里同时有「销售」和「课程销售」时，
    简历上一句「任职课程销售。」两个都会命中 —— 短词是长词的一部分，
    这就是**子串污染**。打到 exp_tags 上会直接抬高档位：
    一份只写了课程销售的简历，因为还带着"销售"这个更泛的词，
    会被算成既有课程销售、又有普通销售经历。

    这里改成占位制：按长度从长到短处理，长词先占住它在原文里的位置，
    短词只能去占长词没占过的位置，占不到就不算命中。
    同一个词在原文里出现多次时，只要有一处没被占就算命中
    （否则「课程销售 课程销售」会留一个空位让"销售"钻进来）。

    命中词按**传入顺序**返回（不是按出现位置），方便和词表对得上。
    """
    hay = normalize(haystack or "")
    if not hay:
        return []

    ordered: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        cleaned = (token or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)

    # 每个词在原文里的所有整词出现位置
    spans: Dict[str, List[Tuple[int, int]]] = {}
    for token in ordered:
        if len(token) < min_len:
            continue
        found: List[Tuple[int, int]] = []
        start = hay.find(token)
        while start >= 0:
            found.append((start, start + len(token)))
            start = hay.find(token, start + 1)
        if found:
            spans[token] = found

    claimed: List[Tuple[int, int]] = []
    hits: Set[str] = set()

    def _is_free(span: Tuple[int, int]) -> bool:
        start, end = span
        return all(end <= c_start or start >= c_end for c_start, c_end in claimed)

    # 长词先占位。sorted 是稳定排序，同长度按传入顺序
    for token in sorted(spans, key=len, reverse=True):
        free = [span for span in spans[token] if _is_free(span)]
        if not free:
            continue
        hits.add(token)
        claimed.extend(free)

    return [token for token in ordered if token in hits]