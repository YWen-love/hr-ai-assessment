"""会话持久化。

一份 sessions 记录同时承载三层数据：
1. 输入：简历标签、对话历史
2. 客观层结论：total_score / match_level / score_breakdown（由 rule_engine 算）
3. 主观层结论：conversation_traits / willing_for_target_job（由 traits 抽取）
4. 视图隔离：hr_summary 与 candidate_msg 分开存，候选人只看后者

设计约束：**分数不存模型的返回值**，只存后端按配置重算的结果。
model_score 仅作为对照留档，用于观察模型自评分与实际规则的偏差。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from app.paths import data_root

DB_PATH = data_root() / "resume_sessions.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# JSON 列：读写时自动 dumps/loads
_JSON_COLUMNS = ("resume_json", "answers", "chat_history", "score_breakdown",
                 "conversation_traits", "evidence_json", "backup_recommend_keys",
                 "resume_tags", "rag_events", "job_config_snapshot")

# 读回来时 fallback 应该是 {} 而不是 [] 的列
_DICT_JSON_COLUMNS = ("resume_json", "resume_tags", "conversation_traits",
                      "evidence_json", "job_config_snapshot")

# (列名, SQL 类型, 默认值) —— _ensure_columns 按这个表做幂等补列
_ASSESSMENT_COLUMNS = (
    ("job_key", "TEXT", None),
    ("total_score", "INTEGER", None),
    ("match_level", "TEXT", None),
    ("score_breakdown", "TEXT", "[]"),
    ("willing_for_target_job", "INTEGER", None),
    ("conversation_traits", "TEXT", None),
    ("evidence_json", "TEXT", None),
    ("model_score", "REAL", None),
    ("backup_recommend_keys", "TEXT", "[]"),
    ("hr_summary", "TEXT", None),
    ("candidate_msg", "TEXT", None),
    ("resume_tags", "TEXT", None),
    ("rag_events", "TEXT", "[]"),
    ("config_hash", "TEXT", None),
    ("job_config_snapshot", "TEXT", None),
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# 字段改名迁移：traits 的输出键从 willing_for_main_job 改为 willing_for_target_job。
# 列名跟着一起改 —— 数据库列名和 JSON 键名长期不一致的话，
# 半年后没人说得清哪个才是准的。
_RENAMED_COLUMNS = (("willing_for_main_job", "willing_for_target_job"),)


def _ensure_columns() -> None:
    """幂等补列 + 改名迁移。沿用项目既有写法：ALTER TABLE 加字段，老数据自动为 NULL。"""
    with _connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        # 改名要先于补列：否则先补出一个空的 willing_for_target_job，
        # 再改名的目标列已存在，历史数据就永远留在旧列里。
        for old, new in _RENAMED_COLUMNS:
            if old in columns and new not in columns:
                conn.execute(f"ALTER TABLE sessions RENAME COLUMN {old} TO {new}")
                columns.discard(old)
                columns.add(new)
        for name, sql_type, _default in _ASSESSMENT_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type}")
        conn.commit()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                candidate_name TEXT,
                job_name TEXT,
                job_key TEXT,
                resume_json TEXT,
                resume_tags TEXT,
                candidate_summary TEXT,
                answers TEXT DEFAULT '[]',
                chat_history TEXT DEFAULT '[]',
                last_question TEXT,
                final_decision TEXT,
                summary TEXT,
                hr_summary TEXT,
                candidate_msg TEXT,
                total_score INTEGER,
                match_level TEXT,
                score_breakdown TEXT DEFAULT '[]',
                willing_for_target_job INTEGER,
                conversation_traits TEXT,
                evidence_json TEXT,
                model_score REAL,
                backup_recommend_keys TEXT DEFAULT '[]',
                rag_events TEXT DEFAULT '[]',
                config_hash TEXT,
                job_config_snapshot TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'created'
            )
            """
        )
        conn.commit()
    _ensure_columns()


# ---------------------------------------------------------------------------
# JSON 列辅助
# ---------------------------------------------------------------------------

def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _decode(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    for column in _JSON_COLUMNS:
        if column in result:
            fallback = {} if column in _DICT_JSON_COLUMNS else []
            result[column] = _loads(result.get(column), fallback)

    # SQLite 没有布尔类型，读出来统一还原成 bool/None
    willing = result.get("willing_for_target_job")
    if willing is not None:
        result["willing_for_target_job"] = bool(willing)
    return result


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def create_session(
    candidate_name: str,
    job_name: str,
    resume_json: Dict[str, Any],
    candidate_summary: str,
    *,
    job_key: str = "",
) -> str:
    init_db()
    session_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, candidate_name, job_name, job_key,
                resume_json, resume_tags, candidate_summary,
                answers, chat_history, last_question,
                final_decision, summary, created_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '[]', NULL, NULL, NULL, datetime('now'), 'created')
            """,
            (
                session_id,
                candidate_name,
                job_name,
                job_key or None,
                _dumps(resume_json or {}),
                _dumps(resume_json or {}),  # 解析层标签与原始解析结果同源，先同存
                candidate_summary,
            ),
        )
        conn.commit()
    return session_id


def update_session_status(session_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE sessions SET status = ? WHERE session_id = ?", (status, session_id))
        conn.commit()


def save_session_answers(
    session_id: str,
    answers: List[str],
    last_question: str,
    status: str = "in_progress",
    chat_history: Optional[list] = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE sessions
               SET answers = ?, last_question = ?, status = ?, chat_history = ?
             WHERE session_id = ?
            """,
            (
                _dumps(answers or []),
                last_question,
                status,
                _dumps(chat_history or []),
                session_id,
            ),
        )
        conn.commit()


def save_resume_tags(session_id: str, resume_tags: Dict[str, Any]) -> None:
    """简历解析完成后回填打分层标签。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET resume_tags = ? WHERE session_id = ?",
            (_dumps(resume_tags or {}), session_id),
        )
        conn.commit()


def append_rag_event(session_id: str, event: Dict[str, Any]) -> None:
    """追加一条 RAG 旁路事件，供审计：旁路问了什么、答了什么、有没有被拦。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT rag_events FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        events = _loads(row["rag_events"] if row else None, [])
        events.append(event)
        conn.execute(
            "UPDATE sessions SET rag_events = ? WHERE session_id = ?",
            (_dumps(events), session_id),
        )
        conn.commit()


def save_assessment_result(
    session_id: str,
    *,
    objective: Dict[str, Any],
    subjective: Dict[str, Any],
    final_decision_text: str,
    hr_summary: str,
    candidate_msg: str,
    evidence: Optional[Dict[str, Any]] = None,
    backup_recommend_keys: Optional[List[str]] = None,
    model_score: Optional[float] = None,
    config_hash: str = "",
    job_config_snapshot: Optional[Dict[str, Any]] = None,
    status: str = "finished",
) -> None:
    """落库完整测评结论。

    注意 total_score 取自 objective.total_score（后端按 job_config 重算），
    模型自评只写进 model_score 做对照，绝不参与判定。

    job_config_snapshot 存的是**这次测评当时**的完整岗位配置。必须有它：
    HR 之后改岗位配置，历史报告要能原样还原，不能跟着新规则重算。
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE sessions
               SET total_score = ?, match_level = ?, score_breakdown = ?,
                   willing_for_target_job = ?, conversation_traits = ?, evidence_json = ?,
                   model_score = ?, backup_recommend_keys = ?,
                   hr_summary = ?, candidate_msg = ?,
                   final_decision = ?, summary = ?, config_hash = ?,
                   job_config_snapshot = ?, status = ?
             WHERE session_id = ?
            """,
            (
                objective.get("total_score"),
                objective.get("match_level"),
                _dumps(objective.get("score_breakdown") or []),
                _as_sql_bool(subjective.get("willing_for_target_job")),
                _dumps(subjective.get("conversation_traits") or []),
                _dumps(evidence or {}),
                model_score,
                _dumps(backup_recommend_keys or []),
                hr_summary,
                candidate_msg,
                final_decision_text,
                hr_summary,
                config_hash or None,
                _dumps(job_config_snapshot) if job_config_snapshot else None,
                status,
                session_id,
            ),
        )
        conn.commit()


def _as_sql_bool(value: Any) -> Optional[int]:
    if value is None:
        return None
    return 1 if value is True else 0


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

_SELECT_ALL = "SELECT * FROM sessions"

# HR 列表页需要的字段。刻意不 SELECT * —— 会话表会持续加列，
# 列表页把整行拉出来只是浪费带宽。
_LIST_COLUMNS = (
    "session_id", "candidate_name", "job_name", "job_key",
    "total_score", "match_level", "final_decision",
    "willing_for_target_job", "status", "created_at",
)


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            f"{_SELECT_ALL} WHERE session_id = ?", (session_id,)
        ).fetchone()
    return _decode(row) if row else None


def get_candidate_view(session_id: str) -> Optional[Dict[str, Any]]:
    """候选人视图。物理上只返回 candidate_msg + 主岗名，不带分数和备选岗。"""
    session = get_session(session_id)
    if session is None:
        return None
    return {
        "session_id": session["session_id"],
        "job_name": session["job_name"],
        "candidate_msg": session.get("candidate_msg"),
        "status": session.get("status"),
    }


def list_sessions(
    *,
    job_key: Optional[str] = None,
    match_level: Optional[str] = None,
    final_decision: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """HR 会话列表，支持按岗位/档位/终局/状态筛选。"""
    init_db()
    conditions, params = [], []
    for column, value in (
        ("job_key", job_key),
        ("match_level", match_level),
        ("final_decision", final_decision),
        ("status", status),
    ):
        if value:
            conditions.append(f"{column} = ?")
            params.append(value)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    columns = ", ".join(_LIST_COLUMNS)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM sessions{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        willing = item.get("willing_for_target_job")
        if willing is not None:
            item["willing_for_target_job"] = bool(willing)
        results.append(item)
    return results


def stats_by_job() -> List[Dict[str, Any]]:
    """按岗位汇总：投递数 / 各档位人数 / 各终局决策人数。"""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT job_key, job_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN match_level = '高匹配' THEN 1 ELSE 0 END) AS high,
                   SUM(CASE WHEN match_level = '中匹配' THEN 1 ELSE 0 END) AS mid,
                   SUM(CASE WHEN match_level = '低匹配' THEN 1 ELSE 0 END) AS low,
                   SUM(CASE WHEN final_decision = '推进' THEN 1 ELSE 0 END) AS advanced,
                   SUM(CASE WHEN final_decision = '不推进' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN final_decision = '谨慎推进' THEN 1 ELSE 0 END) AS cautious,
                   -- 待人工复核是独立一档，不能并进 rejected：候选人只是没表态，
                   -- 和明确拒绝是两回事，混在一起 HR 会以为招拒比虚高。
                   SUM(CASE WHEN final_decision = '待人工复核' THEN 1 ELSE 0 END) AS need_review
              FROM sessions
             WHERE job_key IS NOT NULL AND job_key != ''
             GROUP BY job_key, job_name
             ORDER BY total DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]