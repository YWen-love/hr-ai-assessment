from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from app.paths import data_root, resource_path

if TYPE_CHECKING:
    from app.assessment_agent import AssessmentAgent

logger = logging.getLogger(__name__)

_SECTION_PATTERN = re.compile(r"^===(.+?)===\s*$", re.MULTILINE)
_RULE_CHUNK_SIZE = 1800


@dataclass(frozen=True)
class JobKnowledge:
    job_name: str
    job_intro: str
    screening_rules: str


def _database_path() -> Path:
    return data_root() / "rag_knowledge.db"


def _is_valid_sqlite(path: Path) -> bool:
    """检查文件是否合法 SQLite（防止脏文件 / 损坏文件卡住启动）"""
    if not path.exists():
        return False
    if path.stat().st_size < 16:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(16).startswith(b"SQLite format 3\x00")
    except OSError:
        return False


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    database_path = _database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists() and not _is_valid_sqlite(database_path):
        logger.warning("Found invalid SQLite file at %s, recreating...", database_path)
        database_path.unlink(missing_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_marked_sections(content: str) -> dict[str, str]:
    markers = list(_SECTION_PATTERN.finditer(content))
    sections: dict[str, str] = {}
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
        name = marker.group(1).strip()
        body = content[start:end].strip()
        named_entries = list(
            re.finditer(r"(?m)^\s*岗位名称\s*[:：]\s*(.+?)\s*$", body)
        )
        if named_entries:
            prefix = body[: named_entries[0].start()].strip()
            if name and prefix:
                sections[name] = prefix
            for entry_index, entry in enumerate(named_entries):
                entry_end = (
                    named_entries[entry_index + 1].start()
                    if entry_index + 1 < len(named_entries)
                    else len(body)
                )
                entry_name = entry.group(1).strip()
                entry_body = body[entry.start() : entry_end].strip()
                if entry_name and entry_body:
                    sections[entry_name] = entry_body
        elif name and body:
            sections[name] = body
    return sections


def initialize_knowledge_base() -> None:
    intro_path = resource_path("job_intro.md")
    rules_path = resource_path("screen_rules.md")
    intro_sections = _parse_marked_sections(intro_path.read_text(encoding="utf-8"))
    legacy_rules = _parse_marked_sections(rules_path.read_text(encoding="utf-8"))

    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_name TEXT PRIMARY KEY,
                intro_hash TEXT NOT NULL,
                rules_for_intro_hash TEXT NOT NULL DEFAULT '',
                source_rules_hash TEXT NOT NULL DEFAULT 'untracked',
                updated_at TEXT NOT NULL
            )
            """
        )
        job_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
        }
        if "source_rules_hash" not in job_columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN source_rules_hash TEXT NOT NULL DEFAULT 'untracked'"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                job_name TEXT NOT NULL REFERENCES jobs(job_name) ON DELETE CASCADE,
                document_type TEXT NOT NULL CHECK(document_type IN ('job_intro', 'screening_rules')),
                content TEXT NOT NULL,
                PRIMARY KEY(job_name, document_type)
            )
            """
        )

        job_names = list(intro_sections)
        if job_names:
            placeholders = ", ".join("?" for _ in job_names)
            connection.execute(
                f"DELETE FROM knowledge_documents WHERE job_name NOT IN ({placeholders})",
                job_names,
            )
            connection.execute(
                f"DELETE FROM jobs WHERE job_name NOT IN ({placeholders})",
                job_names,
            )
        else:
            connection.execute("DELETE FROM knowledge_documents")
            connection.execute("DELETE FROM jobs")

        for job_name, job_intro in intro_sections.items():
            intro_hash = _hash_text(job_intro)
            source_rules = legacy_rules.get(job_name, "")
            source_rules_hash = _hash_text(source_rules)
            existing = connection.execute(
                """
                SELECT intro_hash, rules_for_intro_hash, source_rules_hash
                FROM jobs WHERE job_name = ?
                """,
                (job_name,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_name, intro_hash, rules_for_intro_hash,
                        source_rules_hash, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_name,
                        intro_hash,
                        intro_hash if source_rules else "",
                        source_rules_hash,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_documents VALUES (?, 'job_intro', ?)",
                    (job_name, job_intro),
                )
                connection.execute(
                    "INSERT INTO knowledge_documents VALUES (?, 'screening_rules', ?)",
                    (job_name, source_rules),
                )
            elif existing["source_rules_hash"] == "untracked":
                connection.execute(
                    """
                    UPDATE jobs
                    SET intro_hash = ?, source_rules_hash = ?,
                        rules_for_intro_hash = CASE WHEN ? != '' THEN ? ELSE rules_for_intro_hash END,
                        updated_at = ?
                    WHERE job_name = ?
                    """,
                    (
                        intro_hash,
                        source_rules_hash,
                        source_rules,
                        intro_hash,
                        datetime.now(timezone.utc).isoformat(),
                        job_name,
                    ),
                )
                connection.execute(
                    """
                    UPDATE knowledge_documents SET content = ?
                    WHERE job_name = ? AND document_type = 'job_intro'
                    """,
                    (job_intro, job_name),
                )
                if source_rules:
                    connection.execute(
                        """
                        UPDATE knowledge_documents SET content = ?
                        WHERE job_name = ? AND document_type = 'screening_rules'
                        """,
                        (source_rules, job_name),
                    )
            elif existing["source_rules_hash"] != source_rules_hash:
                connection.execute(
                    """
                    UPDATE jobs
                    SET intro_hash = ?, source_rules_hash = ?,
                        rules_for_intro_hash = ?, updated_at = ?
                    WHERE job_name = ?
                    """,
                    (
                        intro_hash,
                        source_rules_hash,
                        intro_hash if source_rules else "",
                        datetime.now(timezone.utc).isoformat(),
                        job_name,
                    ),
                )
                connection.execute(
                    """
                    UPDATE knowledge_documents SET content = ?
                    WHERE job_name = ? AND document_type = 'job_intro'
                    """,
                    (job_intro, job_name),
                )
                connection.execute(
                    """
                    UPDATE knowledge_documents SET content = ?
                    WHERE job_name = ? AND document_type = 'screening_rules'
                    """,
                    (source_rules, job_name),
                )
            elif existing["intro_hash"] != intro_hash:
                connection.execute(
                    """
                    UPDATE jobs SET intro_hash = ?, updated_at = ?
                    WHERE job_name = ?
                    """,
                    (intro_hash, datetime.now(timezone.utc).isoformat(), job_name),
                )
                connection.execute(
                    """
                    UPDATE knowledge_documents SET content = ?
                    WHERE job_name = ? AND document_type = 'job_intro'
                    """,
                    (job_intro, job_name),
                )


def list_jobs(*, ready_only: bool = False) -> list[str]:
    query = """
        SELECT jobs.job_name
        FROM jobs
        JOIN knowledge_documents AS intro
          ON intro.job_name = jobs.job_name AND intro.document_type = 'job_intro'
        LEFT JOIN knowledge_documents AS rules
          ON rules.job_name = jobs.job_name AND rules.document_type = 'screening_rules'
    """
    if ready_only:
        query += """
            WHERE rules.content IS NOT NULL
              AND rules.content != ''
              AND jobs.rules_for_intro_hash = jobs.intro_hash
        """
    query += " ORDER BY jobs.job_name"
    try:
        with _connect() as connection:
            return [row["job_name"] for row in connection.execute(query).fetchall()]
    except sqlite3.DatabaseError as exc:
        logger.warning("list_jobs failed (%s); returning empty list", exc)
        return []


def get_job_knowledge(job_name: str, *, require_rules: bool = True) -> JobKnowledge:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT jobs.job_name, jobs.intro_hash, jobs.rules_for_intro_hash,
                   intro.content AS job_intro, rules.content AS screening_rules
            FROM jobs
            JOIN knowledge_documents AS intro
              ON intro.job_name = jobs.job_name AND intro.document_type = 'job_intro'
            LEFT JOIN knowledge_documents AS rules
              ON rules.job_name = jobs.job_name AND rules.document_type = 'screening_rules'
            WHERE jobs.job_name = ?
            """,
            (job_name,),
        ).fetchone()

    if row is None:
        raise KeyError(f"Unknown job: {job_name}")
    screening_rules = row["screening_rules"] or ""
    if require_rules and (
        not screening_rules or row["rules_for_intro_hash"] != row["intro_hash"]
    ):
        raise RuntimeError(f"Screening rules are not ready for job: {job_name}")
    return JobKnowledge(job_name, row["job_intro"], screening_rules)


def jobs_needing_rules() -> list[JobKnowledge]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT jobs.job_name, intro.content AS job_intro
            FROM jobs
            JOIN knowledge_documents AS intro
              ON intro.job_name = jobs.job_name AND intro.document_type = 'job_intro'
            LEFT JOIN knowledge_documents AS rules
              ON rules.job_name = jobs.job_name AND rules.document_type = 'screening_rules'
            WHERE rules.content IS NULL
               OR rules.content = ''
               OR jobs.rules_for_intro_hash != jobs.intro_hash
            ORDER BY jobs.job_name
            """
        ).fetchall()
    return [JobKnowledge(row["job_name"], row["job_intro"], "") for row in rows]


def save_screening_rules(job_name: str, screening_rules: str) -> None:
    rules = screening_rules.strip()
    if not rules:
        raise ValueError(f"Generated screening rules are empty for job: {job_name}")
    with _connect() as connection:
        row = connection.execute(
            "SELECT intro_hash FROM jobs WHERE job_name = ?",
            (job_name,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_name}")
        connection.execute(
            """
            INSERT INTO knowledge_documents (job_name, document_type, content)
            VALUES (?, 'screening_rules', ?)
            ON CONFLICT(job_name, document_type) DO UPDATE SET content = excluded.content
            """,
            (job_name, rules),
        )
        connection.execute(
            """
            UPDATE jobs SET rules_for_intro_hash = ?, updated_at = ?
            WHERE job_name = ?
            """,
            (row["intro_hash"], datetime.now(timezone.utc).isoformat(), job_name),
        )


async def generate_missing_screening_rules(agent: AssessmentAgent) -> None:
    for job in jobs_needing_rules():
        try:
            rules = await agent.generate_screening_rules(job.job_name, job.job_intro)
            save_screening_rules(job.job_name, rules)
        except Exception:
            logger.exception("Could not prepare screening rules for job %s", job.job_name)
