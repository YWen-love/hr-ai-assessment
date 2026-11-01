"""红线校验与主线协议解析。

四道防线，从外到内：
1. 结构层：对话 Agent 根本不接收 alternate_role_mapping —— 主防线，模型无从说漏
2. 词法层：本模块扫描每一条候选人可见文本，命中备选岗名称/关键词即拦截
3. 协议层：[NEED_RAG] / [DONE] 标记只在本模块捕获，绝不下发给候选人
4. 收敛层：候选人侧结果由独立调用生成，其上下文不含备选岗信息

第 2 层是纵深防御，不是主要依赖 —— 真正常见的失效场景是运营后来给某岗位
新增了备选岗，而旧会话的 Prompt 是按旧配置渲染的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_NEED_RAG_RE = re.compile(r"\[NEED_RAG\]\s*(.+?)\s*(?:\n|$)")
_DONE_RE = re.compile(r"\[DONE\]")

# 泛化的转岗话术。不依赖具体岗位名，命中即拦截。
_TRANSFER_PATTERNS = (
    "转岗", "换个方向", "换个岗位", "换个赛道", "试试别的岗位",
    "其他岗位", "别的岗位", "别的职位", "更适合的岗位",
    "我们可以考虑", "建议你考虑",
)

# 注意：这里刻意【不】做「任何岗位名后缀」的泛化正则（顾问/销售/运营…）。
# 主岗自己的介绍里就高频出现这些词，泛化正则必然误杀，正常对话直接崩。
# 真正需要拦的岗位名由调用方从 job_config.alternate_role_mapping 精确提供。


@dataclass
class ChatMessage:
    """一条可下发给候选人的消息 + 从中捕获的主线协议信号。

    text 是【可直接下发】的内容：命中红线时已置空（fail-closed）。
    模型原话保留在 raw_text 里供审计，但绝不参与下发。
    宁可漏发一句，也不让调用方漏掉一次检查就把备选岗说给候选人。
    """

    text: str
    need_rag_question: Optional[str] = None
    is_done: bool = False
    blocked_terms: List[str] = field(default_factory=list)
    was_blocked: bool = False
    raw_text: str = ""


def parse_chat_message(
    raw: str,
    *,
    forbidden_terms: Optional[List[str]] = None,
) -> ChatMessage:
    """解析模型原始输出：抽协议标记、剥离标记文本、做红线词法拦截。

    forbidden_terms 由调用方从 job_config.alternate_role_mapping 构造。
    """
    text = (raw or "").strip()

    need_rag_question: Optional[str] = None
    rag_match = _NEED_RAG_RE.search(text)
    if rag_match:
        question = rag_match.group(1).strip()
        # 标记行连同其内容整体剥离，绝不下发给候选人
        text = text[: rag_match.start()].rstrip()
        need_rag_question = question or None

    is_done = bool(_DONE_RE.search(text))
    if is_done:
        text = _DONE_RE.sub("", text).rstrip()

    blocked = find_forbidden_terms(text, forbidden_terms or [])
    return ChatMessage(
        # 命中红线就清空正文：调用方即使忘了看 was_blocked 也发不出去
        text="" if blocked else text,
        need_rag_question=need_rag_question,
        is_done=is_done,
        blocked_terms=blocked,
        was_blocked=bool(blocked),
        raw_text=text,
    )


def find_forbidden_terms(text: str, terms: List[str]) -> List[str]:
    """扫描文本中的红线词。返回去重后的命中词表。"""
    hits: List[str] = []

    for term in terms:
        cleaned = (term or "").strip()
        if len(cleaned) >= 2 and cleaned in text and cleaned not in hits:
            hits.append(cleaned)

    for pattern in _TRANSFER_PATTERNS:
        if pattern in text and pattern not in hits:
            hits.append(pattern)

    return hits


def build_forbidden_terms(job_config: Optional[Dict[str, Any]]) -> List[str]:
    """从岗位配置构造禁词表。

    只取 alternate_role_mapping —— 主岗名是合法内容，不能进禁词表。
    岗位 key 一并纳入：模型偶尔会用 key 形态泄漏。
    """
    if not job_config:
        return []

    terms: List[str] = []
    for backup in job_config.get("alternate_role_mapping") or []:
        if not isinstance(backup, dict):
            continue
        for field_name in ("backup_job_name", "backup_job_key"):
            value = (backup.get(field_name) or "").strip()
            if len(value) >= 2 and value not in terms:
                terms.append(value)
    return terms


def sanitize_candidate_text(
    text: str,
    *,
    forbidden_terms: Optional[List[str]] = None,
    fallback: str = "好的，我这边先记录下来，稍后同步给 HR 跟你确认。",
) -> str:
    """候选人侧最终下发前的最后一道清洗。

    与 parse_chat_message 的区别：这里不做协议解析，只保证"能下发的一定干净"。
    命中红线时返回兜底话术而不是半句残文 —— 残文比错误信息更容易造成误解。
    """
    text = (text or "").strip()
    if not text:
        return fallback

    # 协议标记若漏到这里（理论上不该发生），一并剥掉
    text = _NEED_RAG_RE.sub("", text)
    text = _DONE_RE.sub("", text).strip()

    if find_forbidden_terms(text, forbidden_terms or []):
        return fallback

    return text


def rag_answer_is_offline(
    rag_text: str,
    *,
    forbidden_terms: Optional[List[str]] = None,
) -> bool:
    """RAG 旁路回答的额外校验。

    RAG 资料不得反向影响主线：它只能回答候选人的那一个提问，
    不得包含任何岗位切换、备选岗位、评分相关内容。若检索内容越界，
    宁可只回一句客套话，也不能让旁路把主线带偏。
    """
    text = (rag_text or "").strip()
    if not text:
        return True

    if find_forbidden_terms(text, forbidden_terms or []):
        return True

    off_topic = (
        "匹配档位", "高匹配", "中匹配", "低匹配", "总分", "打分", "评分",
        "推荐你", "建议你去", "更适合",
    )
    return any(keyword in text for keyword in off_topic)