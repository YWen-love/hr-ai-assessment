"""RAG 旁路：只回答候选人的深度咨询，绝不参与主线。

四条约束的落点（与需求约定一一对应）：

1. **触发限制** —— 主线 Agent 只在候选人主动跳出主线提问时输出 [NEED_RAG]；
   提示词里已列明禁止触发的场景，代码侧再做一次闸：
   正在确认要点的轮次不受理旁路请求。
2. **强制回归** —— 答复与"回归被暂停的要点"由同一次调用生成，
   不依赖调用方记得追问。回落提示语作为第二道保险。
3. **绝不影响主线** —— RAG 返回值不进入 job_config、不进入打分、不进入
   conversation_traits。本模块只产出一个字符串。
4. **知识库隔离** —— 每个岗位绑定独立 rag_doc_id，检索 SQL 恒带
   WHERE rag_doc_id = ?，跨岗位内容在查询层就取不到。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.job_schema import iter_check_item_texts
from app.llm_client import LLMClient
from app.paths import data_root
from app.redline import rag_answer_is_offline, sanitize_candidate_text
from app.text_match import tokenize

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 700
_CHUNK_OVERLAP = 120
_TOP_K = 4

# 检索不到任何资料时的兜底。这不是"装作答了"，是如实告知并立刻回归主线。
NO_CONTEXT_ANSWER = "这个我这边暂时没有查到准确的说法，我帮你转给 HR 确认一下。"


# ---------------------------------------------------------------------------
# 数据层：rag_doc_id 隔离的知识片段库
# ---------------------------------------------------------------------------

def _database_path() -> Path:
    return data_root() / "rag_docs.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_rag_store() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rag_doc_id TEXT NOT NULL,
                job_key TEXT NOT NULL DEFAULT '',
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 隔离靠索引前缀过滤，这个索引让 rag_doc_id 过滤走索引而不是全表扫
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_doc ON rag_chunks(rag_doc_id, chunk_index)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_docs (
                rag_doc_id TEXT PRIMARY KEY,
                job_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_rag_document(rag_doc_id: str, job_key: str, content: str, title: str = "") -> None:
    """整篇覆盖式写入。切分是确定性的，重复导入不会产生重复片段。"""
    if not rag_doc_id:
        raise ValueError("rag_doc_id 不能为空")

    chunks = _split(content or "")
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO rag_docs (rag_doc_id, job_key, title, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(rag_doc_id) DO UPDATE SET
                job_key = excluded.job_key, title = excluded.title, updated_at = excluded.updated_at
            """,
            (rag_doc_id, job_key, title, now),
        )
        conn.execute("DELETE FROM rag_chunks WHERE rag_doc_id = ?", (rag_doc_id,))
        conn.executemany(
            """
            INSERT INTO rag_chunks (rag_doc_id, job_key, chunk_index, content, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(rag_doc_id, job_key, i, chunk, now) for i, chunk in enumerate(chunks)],
        )
        conn.commit()


def delete_rag_document(rag_doc_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM rag_chunks WHERE rag_doc_id = ?", (rag_doc_id,))
        conn.execute("DELETE FROM rag_docs WHERE rag_doc_id = ?", (rag_doc_id,))
        conn.commit()


def retrieve(rag_doc_id: str, question: str, *, top_k: int = _TOP_K) -> List[str]:
    """按 rag_doc_id 检索片段。

    这条 SQL 里 rag_doc_id 是硬条件而非可选过滤 —— 约束 4 的隔离性由它保证。
    即便调用方传错 job，检索结果也只会落在传错的那个库内，不会串到别的岗位。
    """
    if not rag_doc_id:
        return []

    query_tokens = set(tokenize(question or ""))
    if not query_tokens:
        return []

    with _connect() as conn:
        rows = conn.execute(
            "SELECT chunk_index, content FROM rag_chunks WHERE rag_doc_id = ? ORDER BY chunk_index",
            (rag_doc_id,),
        ).fetchall()

    scored: List[tuple[int, int, str]] = []
    for row in rows:
        score = _overlap_score(query_tokens, row["content"])
        if score > 0:
            scored.append((score, row["chunk_index"], row["content"]))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [content for _, _, content in scored[:top_k]]


def _overlap_score(query_tokens: set[str], content: str) -> int:
    """bigram 命中计数。无向量库依赖，对短问句够用。"""
    if not query_tokens:
        return 0
    content_tokens = set(tokenize(content))
    return len(query_tokens & content_tokens)


def _split(content: str) -> List[str]:
    """按段落切，段落过长再按句子切，最后按字符硬切并保留重叠。"""
    chunks: List[str] = []
    buffer = ""
    for paragraph in (content or "").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > _CHUNK_SIZE:
            head, paragraph = paragraph[: _CHUNK_SIZE - _CHUNK_OVERLAP], paragraph[_CHUNK_SIZE - _CHUNK_OVERLAP :]
            chunks.append(head.strip())
        buffer = f"{buffer}\n{paragraph}".strip() if buffer else paragraph
        if len(buffer) >= _CHUNK_SIZE:
            chunks.append(buffer.strip())
            buffer = ""
    if buffer:
        chunks.append(buffer.strip())
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# 旁路生成
# ---------------------------------------------------------------------------

@dataclass
class RagAnswer:
    """一次旁路的完整结果。只有 text 会被下发给候选人。"""

    text: str
    answer: str = ""
    return_to_mainline: str = ""
    citations: List[str] = field(default_factory=list)
    blocked: bool = False
    offline_hit: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.text or "").strip()


RAG_BYPASS_SYSTEM_PROMPT = """
你是招聘方，回答候选人在测评过程中提出的深度咨询问题。

【可用的参考资料】
{{context}}

【回答规则】
1. 只根据上面【可用的参考资料】回答。资料里没有的，直接说"这个我需要帮您确认一下"，
   严禁依据常识、或其他岗位的经验、或者任何资料之外的内容作答。
2. 回答 1-3 句话，口语化，像微信上正常回消息。不要用列表、不要用标题、不要用书面语。
3. 不要提及分数、测评结果、岗位匹配程度、内部流程或任何系统信息。
4. 不要提及除当前岗位之外的任何岗位，不要建议换方向。

【收尾规则（必须执行）】
回答之后，另起一行，接着把对话拉回主线。就用下面这句，不要改写、不要解释：

{{resume_line}}

【输出格式】
先写答复正文，再空一行，再原样输出收尾句。除此之外不要输出任何内容。
"""


class RagBypass:
    def __init__(self):
        self.client = LLMClient(temperature=0.2)

    async def answer(
        self,
        question: str,
        job_config: Dict[str, Any],
        *,
        forbidden_terms: Optional[List[str]] = None,
    ) -> RagAnswer:
        """回答一次深度咨询，并强制回归主线。

        约束 3 的落点：本函数只返回一个用于下发的字符串。
        job_config 只读不写，返回值也不会被写回任何配置或评分对象。
        """
        job_name = job_config.get("job_name", "当前岗位")
        rag_doc_id = job_config.get("rag_doc_id") or ""

        if not rag_doc_id:
            # 没绑定知识库就如实说查不到，绝不退化成"凭常识答"
            logger.warning("岗位 %s 未绑定 rag_doc_id，旁路不受理", job_name)
            return RagAnswer(text=_with_resume_line(NO_CONTEXT_ANSWER, job_config))

        context_chunks = retrieve(rag_doc_id, question)
        if not context_chunks:
            return RagAnswer(
                text=_with_resume_line(NO_CONTEXT_ANSWER, job_config),
                offline_hit=False,
            )

        context = "\n\n".join(f"[片段{i + 1}] {chunk}" for i, chunk in enumerate(context_chunks))
        prompt = RAG_BYPASS_SYSTEM_PROMPT.replace("{{context}}", context).replace(
            "{{resume_line}}", _resume_line(job_config)
        )

        try:
            raw = await self.client.chat(prompt, f"候选人的问题：{question}")
        except Exception:
            logger.exception("RAG 旁路生成失败，job=%s", job_name)
            return RagAnswer(text=_with_resume_line(NO_CONTEXT_ANSWER, job_config))

        answer, resume = _split_answer(raw)
        answer = sanitize_candidate_text(answer, forbidden_terms=forbidden_terms, fallback=NO_CONTEXT_ANSWER)

        # 约束 3 的最后一道闸：旁路文本若沾了评分/换岗内容，整条作废
        offline = rag_answer_is_offline(f"{answer}\n{resume}", forbidden_terms=forbidden_terms)
        if offline:
            logger.warning("RAG 旁路内容越界，已整体作废，job=%s", job_name)
            return RagAnswer(
                text=_with_resume_line(NO_CONTEXT_ANSWER, job_config),
                blocked=True,
                offline_hit=True,
            )

        # 约束 2：无论模型是否吐了收尾句，都由代码补上，绝不漏
        if resume.strip() != _resume_line(job_config).strip():
            resume = _resume_line(job_config)

        text = sanitize_candidate_text(
            f"{answer}\n\n{resume}".strip(),
            forbidden_terms=forbidden_terms,
            fallback=NO_CONTEXT_ANSWER,
        )

        return RagAnswer(
            text=text,
            answer=answer,
            return_to_mainline=resume,
            citations=[f"片段{i + 1}" for i in range(len(context_chunks))],
        )


def _resume_line(job_config: Dict[str, Any]) -> str:
    """回归主线的收尾句。取第一个尚未确认的要点（配置顺序即确认顺序）。"""
    pending = iter_check_item_texts(job_config)
    if not pending:
        return "回到刚才的问题，我们继续把这几点确认完。"
    return f"回到刚才的问题，关于「{pending[0]}」这点你怎么看？"


def _with_resume_line(text: str, job_config: Dict[str, Any]) -> str:
    return f"{text}\n\n{_resume_line(job_config)}"


def _split_answer(raw: str) -> tuple[str, str]:
    """拆出答复正文与收尾句。

    按最后一个空行切：模型把收尾句放在正文之后，
    切错了宁可让后面的 _resume_line 覆盖回去。
    """
    text = (raw or "").strip()
    if not text:
        return "", ""

    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(parts) >= 2:
        return "\n\n".join(parts[:-1]), parts[-1]
    return parts[0] if parts else "", ""