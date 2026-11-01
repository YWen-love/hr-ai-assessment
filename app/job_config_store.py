"""岗位配置表（job_config_table）的读写。

与候选人会话表（resume_sessions.db）分库存储：
- job_configs.db：岗位配置元数据（低频变更、可从 job_configs/*.json 种子化重建）
- resume_sessions.db：候选人会话运行时数据（高频增长、不可重建）

两张表通过 job_key 逻辑关联，sessions 表已冗余存储 job_name，
统计查询不依赖 SQL 联表，因此分库不影响业务查询。

迁移沿用 _ensure_columns 模式，不引入额外迁移框架。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.job_schema import normalize_job_config, split_issues, validate_job_config
from app.paths import data_root

VALID_SOURCE_TYPES = ("auto_llm", "manual")
VALID_REVIEW_STATUS = ("pending", "reviewed")

TABLE_NAME = "job_config_table"

DB_PATH = data_root() / "job_configs.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class JobConfigError(ValueError):
    """配置非法。调用方应把 errors 原样返回给 HR。"""

    def __init__(self, errors: List[str]):
        super().__init__("；".join(errors))
        self.errors = errors


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_config_table (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT NOT NULL,
                job_name TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                raw_jd TEXT,
                config_json TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'pending',
                deleted_at TEXT,
                create_time TEXT DEFAULT (datetime('now')),
                update_time TEXT DEFAULT (datetime('now'))
            )
            """
        )
        # 唯一索引必须带 deleted_at 才能支持软删后重建同 key
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_job_config_key_alive
            ON job_config_table(job_key) WHERE deleted_at IS NULL
            """
        )
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    record = dict(row)
    try:
        record["config"] = json.loads(record.pop("config_json"))
    except (ValueError, TypeError):
        record["config"] = {}
    record["raw_jd"] = record.get("raw_jd")
    record.pop("deleted_at", None)
    return record


def save_job_config(
    config: Dict[str, Any],
    *,
    source_type: str = "manual",
    raw_jd: Optional[str] = None,
    review_status: str = "pending",
) -> Dict[str, Any]:
    """校验后写入。非法配置抛 JobConfigError，不落库。"""
    init_db()
    if source_type not in VALID_SOURCE_TYPES:
        raise JobConfigError([f"source_type 必须是 {VALID_SOURCE_TYPES} 之一"])

    normalized = normalize_job_config(config)
    # 只按「必须修」的那段拦截。带 [警告] 前缀的是建议确认项，
    # 拦下来 HR 就只能靠手改 JSON 才能把岗位建起来。
    errors, _ = split_issues(validate_job_config(normalized))
    if errors:
        raise JobConfigError(errors)

    # 必须写回 normalized：normalize_job_config 返回的是 dict(config) 的浅拷贝，
    # 写原对象不会影响真正入库的那份，会导致 config_json 里的 review_status
    # 与 review_status 列不一致。
    normalized["review_status"] = review_status
    job_key = normalized["job_key"]

    with _connect() as conn:
        existing = conn.execute(
            "SELECT id, create_time, raw_jd FROM job_config_table "
            "WHERE job_key = ? AND deleted_at IS NULL",
            (job_key,),
        ).fetchone()

        if existing:
            # raw_jd 的三态语义：
            #   None → 「这次不动 JD 原文」，沿用库里存着的那份
            #   ""   → 「JD 原文改成空的」（HR 亲手清空了 JD）
            #   文本 → 「JD 原文换成这份」
            # 之前不管传什么都直接 SET raw_jd = ?，于是「不动」和「清空」
            # 变成同一件事：改一次规则，原始 JD 就被抹成 NULL，再也追不回来。
            effective_raw_jd = existing["raw_jd"] if raw_jd is None else raw_jd
            conn.execute(
                """
                UPDATE job_config_table
                SET job_name = ?, source_type = ?, raw_jd = ?, config_json = ?,
                    review_status = ?, update_time = ?
                WHERE id = ?
                """,
                (
                    normalized["job_name"],
                    source_type,
                    effective_raw_jd,
                    json.dumps(normalized, ensure_ascii=False),
                    review_status,
                    _now(),
                    existing["id"],
                ),
            )
            job_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO job_config_table
                    (job_key, job_name, source_type, raw_jd, config_json, review_status, create_time, update_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_key,
                    normalized["job_name"],
                    source_type,
                    raw_jd,
                    json.dumps(normalized, ensure_ascii=False),
                    review_status,
                    _now(),
                    _now(),
                ),
            )
            job_id = cursor.lastrowid
        conn.commit()

    return get_job_config(job_key)  # type: ignore[return-value]


def get_job_config(job_key: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
    """按 job_key 取配置。测评链路的主入口。"""
    init_db()
    sql = "SELECT * FROM job_config_table WHERE job_key = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with _connect() as conn:
        row = conn.execute(sql, (job_key,)).fetchone()
    return _row_to_dict(row) if row else None


def get_job_config_object(job_key: str) -> Optional[Dict[str, Any]]:
    """只要 config 本体，不要外层元信息 —— 打分/对话链路用这个，省一次解包。"""
    record = get_job_config(job_key)
    return record["config"] if record else None


def get_job_config_by_name(job_name: str) -> Optional[Dict[str, Any]]:
    """按岗位显示名（job_name）取配置。

    HR 上传表单的下拉框显示的是中文岗位名（来自 knowledge_base.list_jobs），
    但测评链路全程用 job_key 做内部标识。这里提供 job_name → config 的反查，
    让 /hr/parse-link 能在创建会话时把 job_key 一并存进去。

    取第一条命中：job_name 在业务上应唯一，但表上没加唯一约束，
    真出现重名时取最新更新的那条，避免测评链路因脏数据整条崩掉。
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM job_config_table WHERE job_name = ? AND deleted_at IS NULL "
            "ORDER BY update_time DESC LIMIT 1",
            (job_name,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_job_configs(*, include_deleted: bool = False) -> List[Dict[str, Any]]:
    init_db()
    sql = (
        "SELECT id, job_key, job_name, source_type, review_status, create_time, update_time "
        "FROM job_config_table"
    )
    if not include_deleted:
        sql += " WHERE deleted_at IS NULL"
    sql += " ORDER BY update_time DESC"
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def delete_job_config(job_key: str, *, hard: bool = False) -> bool:
    """默认软删：历史测评记录通过 job_key 关联岗位，物理删除会留下悬空引用。"""
    init_db()
    with _connect() as conn:
        if hard:
            cursor = conn.execute(
                "DELETE FROM job_config_table WHERE job_key = ?", (job_key,)
            )
        else:
            cursor = conn.execute(
                "UPDATE job_config_table SET deleted_at = ?, update_time = ? WHERE job_key = ? AND deleted_at IS NULL",
                (_now(), _now(), job_key),
            )
        conn.commit()
        return cursor.rowcount > 0


def update_job_config_meta(job_key: str, *, review_status: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """只改元信息（目前只有复核状态）。人工复核动作走这里。"""
    init_db()
    if review_status is not None and review_status not in VALID_REVIEW_STATUS:
        raise JobConfigError([f"review_status 必须是 {VALID_REVIEW_STATUS} 之一"])
    with _connect() as conn:
        conn.execute(
            "UPDATE job_config_table SET review_status = COALESCE(?, review_status), update_time = ? WHERE job_key = ?",
            (review_status, _now(), job_key),
        )
        conn.commit()
        # 必须同步 config_json 里的同一字段：测评链路读的是 get_job_config_object()，
        # 也就是这份 blob。只改列的话，HR 点了「已复核」，打分链路看到的仍是 pending。
        if review_status is not None:
            row = conn.execute(
                "SELECT config_json FROM job_config_table WHERE job_key = ?", (job_key,)
            ).fetchone()
            if row:
                try:
                    config = json.loads(row["config_json"])
                except (ValueError, TypeError):
                    config = None
                if isinstance(config, dict):
                    config["review_status"] = review_status
                    conn.execute(
                        "UPDATE job_config_table SET config_json = ? WHERE job_key = ?",
                        (json.dumps(config, ensure_ascii=False), job_key),
                    )
        conn.commit()
    return get_job_config(job_key)


def seed_from_files(config_dir, *, overwrite: bool = False) -> List[str]:
    """把配置目录下的 *.json 导入库。默认幂等：已存在则跳过。

    用于把 job_configs/ 里手工维护的配置（含教培两份初始实例）灌进库。

    overwrite=True 会用文件内容覆盖库里的同名配置并重置为 reviewed。
    只在「改了 job_configs/*.json 想让它生效」时用 —— 默认跳过是为了
    避免覆盖 HR 在后台做过的调整。
    """
    init_db()
    from pathlib import Path

    imported: List[str] = []
    for path in sorted(Path(config_dir).glob("*.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        job_key = config.get("job_key") if isinstance(config, dict) else None
        if not job_key:
            continue
        if get_job_config(job_key) and not overwrite:
            continue
        # 文件里的配置视为人工维护，默认直接生效
        save_job_config(config, source_type="manual", review_status="reviewed")
        imported.append(job_key)
    return imported


def migrate_legacy_configs(*, source_db: Optional[str] = None) -> Dict[str, Any]:
    """把旧库 resume_sessions.db 里的 job_config_table 搬到本库。

    背景：岗位配置原先和候选人会话共用 resume_sessions.db，后来拆到
    独立的 job_configs.db。老库里可能还躺着 HR 在后台配好的岗位。

    这个函数在服务启动时自动跑一次（见 main.py 的 lifespan），
    免得「换了代码忘了跑迁移脚本」→ HR 打开后台发现岗位全没了。

    设计原则（与 scripts/migrate_job_configs_db.py 一致）：
    1. 幂等：按 job_key 去重，已在目标库的直接跳过，可重复执行。
    2. 非破坏：只读源库，不删源库任何数据。
    3. 静默容错：源库不存在 / 没有这张表 / 结构对不上，都不影响服务启动。

    Returns:
        {"migrated": 新增条数, "skipped": 已存在跳过条数, "source": 源库路径}
    """
    # 延迟 import：源库路径以 session_store 的 DB_PATH 为准，
    # 这样测试改 ss.DB_PATH 就能把迁移指向临时库。
    from app.session_store import DB_PATH as SESSION_DB_PATH

    source = Path(source_db) if source_db else Path(SESSION_DB_PATH)
    result: Dict[str, Any] = {"migrated": 0, "skipped": 0, "source": str(source)}

    if not source.exists() or source.resolve() == Path(DB_PATH).resolve():
        return result

    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error:
        return result

    try:
        src.row_factory = sqlite3.Row
        has_table = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        ).fetchone()
        if has_table is None:
            return result

        source_cols = [r[1] for r in src.execute(f"PRAGMA table_info({TABLE_NAME})")]
        if "job_key" not in source_cols:
            return result

        init_db()
        with _connect() as dst:
            target_cols = {r[1] for r in dst.execute(f"PRAGMA table_info({TABLE_NAME})")}
            # 只搬两边都有的列：老库将来若加了新列，不会把启动搞崩
            cols = [c for c in source_cols if c in target_cols]
            if not cols:
                return result
            col_list = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)

            for row in src.execute(f"SELECT {col_list} FROM {TABLE_NAME}").fetchall():
                job_key = row["job_key"]
                if not job_key:
                    continue
                exists = dst.execute(
                    f"SELECT 1 FROM {TABLE_NAME} WHERE job_key = ?", (job_key,)
                ).fetchone()
                if exists:
                    result["skipped"] += 1
                    continue
                try:
                    dst.execute(
                        f"INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({placeholders})",
                        [row[c] for c in cols],
                    )
                    result["migrated"] += 1
                except sqlite3.IntegrityError:
                    # 并发启动或唯一索引打架：跳过即可，不影响服务
                    result["skipped"] += 1
            dst.commit()
    except sqlite3.Error:
        return result
    finally:
        src.close()

    return result