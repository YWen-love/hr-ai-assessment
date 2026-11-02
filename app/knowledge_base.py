from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.paths import data_root, resource_path

logger = logging.getLogger(__name__)

_SECTION_PATTERN = re.compile(r"^===(.+?)===\s*$", re.MULTILINE)
_RULE_CHUNK_SIZE = 1800


# 这个模块现在只做两件事：
#   1. 把 job_intro.md / screen_rules.md 按 ===岗位名=== 切开（_parse_marked_sections）；
#   2. 把切出来的内容写进 rag_knowledge.db（initialize_knowledge_base）。
#
# 它原本还提供一套「岗位名单读接口」（list_jobs / get_job_knowledge /
# jobs_needing_rules / save_screening_rules），让代码能反过来问知识库
# 「有哪些岗位」。那套接口是这次「两份岗位名单打架」的祸根 ——
# 测评链路只认岗位配置表，知识库里的岗位名却是另一份静态快照，
# HR 在岗位管理里新建的岗位永远不会出现在里面。两份答案摆在一起，
# 谁也说不清 HR 看到的下拉到底以哪份为准。
#
# 读接口已删：岗位名单只有一个来源，就是 job_config_table。
# 要按岗位名找配置，走 app.job_config_store.resolve_job_by_name。


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
