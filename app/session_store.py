import json
import sqlite3
import uuid
from typing import Any, Dict, Optional

from app.paths import data_root

DB_PATH = data_root() / "resume_sessions.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns() -> None:
    with _connect() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "answers" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN answers TEXT DEFAULT '[]'")
        if "last_question" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_question TEXT")
        if "final_decision" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN final_decision TEXT")
        if "summary" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN summary TEXT")
        if "chat_history" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN chat_history TEXT DEFAULT '[]'")
        conn.commit()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                candidate_name TEXT,
                job_name TEXT,
                resume_json TEXT,
                candidate_summary TEXT,
                answers TEXT DEFAULT '[]',
                last_question TEXT,
                final_decision TEXT,
                summary TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'created'
            )
            """
        )
        conn.commit()
    _ensure_columns()


def create_session(candidate_name: str, job_name: str, resume_json: Dict[str, Any], candidate_summary: str) -> str:
    init_db()
    session_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, candidate_name, job_name, resume_json, candidate_summary, answers, last_question, final_decision, summary, created_at, status)
            VALUES (?, ?, ?, ?, ?, '[]', NULL, NULL, NULL, datetime('now'), 'created')
            """,
            (
                session_id,
                candidate_name,
                job_name,
                json.dumps(resume_json, ensure_ascii=False),
                candidate_summary,
            ),
        )
        conn.commit()
    return session_id


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_id, candidate_name, job_name, resume_json, candidate_summary, answers, last_question, final_decision, summary, chat_history, created_at, status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["resume_json"] = json.loads(result["resume_json"]) if result["resume_json"] else {}
    result["answers"] = json.loads(result["answers"]) if result["answers"] else []
    result["chat_history"] = json.loads(result["chat_history"]) if result["chat_history"] else []
    return result


def update_session_status(session_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE sessions SET status = ? WHERE session_id = ?", (status, session_id))
        conn.commit()


def save_session_answers(session_id: str, answers: list[str], last_question: str, status: str = "in_progress", chat_history: list = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET answers = ?, last_question = ?, status = ?, chat_history = ? WHERE session_id = ?",
            (json.dumps(answers, ensure_ascii=False), last_question, status, json.dumps(chat_history or [], ensure_ascii=False), session_id),
        )
        conn.commit()


def save_session_result(session_id: str, final_decision: str, summary: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET final_decision = ?, summary = ?, status = 'finished' WHERE session_id = ?",
            (final_decision, summary, session_id),
        )
        conn.commit()


def list_sessions() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT session_id, candidate_name, job_name, status, created_at, final_decision, summary FROM sessions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]
