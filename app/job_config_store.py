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
from app.paths import data_root, resource_path

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
    # raw_jd 必须在这里查出来：岗位管理页面要显示每个岗位的 JD 原文，
    # 之前这里没查，页面只能显示一个空列，HR 看到的永远是「JD 为空」。
    sql = (
        "SELECT id, job_key, job_name, source_type, raw_jd, review_status, "
        "create_time, update_time "
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


def _load_jd_sections(config_dir) -> Dict[str, str]:
    """读 job_intro.md，按 ===岗位名=== 切成 {岗位名: JD 正文}。

    JD 原文的权威来源就是仓库里的 job_intro.md —— 岗位配置本来就是从它
    种出来的，让配置表回填同一份 JD，才能保证「岗位管理里看到的 JD」
    和「测评端读到的岗位介绍」是同一份东西，不会各说各话。

    读不到就返回空字典：JD 回填是补齐信息，不是启动的前提条件，
    不能因为这个文件缺失就把整个服务搞崩。
    """
    candidates: List[Path] = []
    try:
        candidates.append(Path(resource_path("job_intro.md")))
    except (OSError, TypeError, ValueError):
        pass
    try:
        candidates.append(Path(config_dir).parent / "job_intro.md")
    except (TypeError, ValueError):
        pass

    for path in candidates:
        try:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            # 延迟 import：knowledge_base 模块顶层会碰数据库，
            # 放在函数里 import 才不会和这里的读写逻辑搅在一起。
            from app.knowledge_base import _parse_marked_sections

            sections = _parse_marked_sections(content)
        except Exception:  # noqa: BLE001 - 解析器缺失/改动都不能影响启动
            return {}
        if sections:
            return sections
    return {}


def _pick_jd(sections: Dict[str, str], *names: str) -> str:
    """按候选岗位名依次找 JD 原文，第一个非空命中就返回。

    候选顺序：库里存的名字在前（HR 可能在后台改过岗位名），
    配置文件里的名字在后。
    """
    for name in names:
        if not name:
            continue
        text = (sections.get(name) or "").strip()
        if text:
            return text
    return ""


def _backfill_raw_jd(job_key: str, jd_text: str) -> bool:
    """只补 raw_jd 一列，不碰 config_json。返回是否真的补上了。

    已经有 JD 的行不动 —— HR 亲手传上去的 JD 原文优先级最高，
    job_intro.md 只负责补空。
    """
    if not jd_text:
        return False
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE job_config_table
            SET raw_jd = ?, update_time = ?
            WHERE job_key = ? AND deleted_at IS NULL
              AND (raw_jd IS NULL OR TRIM(raw_jd) = '')
            """,
            (jd_text, _now(), job_key),
        )
        conn.commit()
        return cursor.rowcount > 0


def seed_from_files(config_dir, *, overwrite: bool = False) -> List[str]:
    """把配置目录下的 *.json 导入库。默认幂等：已存在则跳过。

    用于把 job_configs/ 里手工维护的配置（含教培两份初始实例）灌进库。

    JD 原文的来源分三级，优先级从高到低：
      1. JSON 文件里的 raw_jd 字段（人工在文件里显式写的，最权威）；
      2. job_intro.md 里对应岗位那一节（岗位介绍原文）；
      3. 空 —— 没有就空着，HR 可以在后台补。

    overwrite=True 会用文件内容覆盖库里的同名配置并重置为 reviewed。
    只在「改了 job_configs/*.json 想让它生效」时用 —— 默认跳过是为了
    避免覆盖 HR 在后台做过的调整。
    """
    init_db()
    from pathlib import Path

    imported: List[str] = []
    jd_sections = _load_jd_sections(config_dir)
    for path in sorted(Path(config_dir).glob("*.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(config, dict):
            continue
        job_key = config.get("job_key")
        if not job_key:
            continue

        # raw_jd 只存原文，不进 config_json —— 配置本体是给打分链路用的
        # 结构化数据，往里塞一整段 JD 原文会污染它。
        file_jd = config.pop("raw_jd", None)
        file_jd = file_jd.strip() if isinstance(file_jd, str) else ""

        existing = get_job_config(job_key)
        intro_jd = _pick_jd(
            jd_sections,
            (existing or {}).get("job_name"),
            config.get("job_name"),
        )

        if existing and not overwrite:
            # 已经在库里了：不能用文件里的 config_json 覆盖 HR 在后台改过的东西，
            # 但 JD 原文是空的话可以顺手补上 —— 只动 raw_jd 一列。
            _backfill_raw_jd(job_key, file_jd or intro_jd)
            continue

        if existing and not file_jd:
            # 强制覆盖配置时，不用岗位介绍去盖掉 HR 亲手维护的 JD 原文。
            # raw_jd 传 None 表示「这次不动 JD」。
            jd_to_write = None
        else:
            jd_to_write = file_jd or intro_jd or None

        # 文件里的配置视为人工维护，默认直接生效
        save_job_config(
            config,
            source_type="manual",
            review_status="reviewed",
            raw_jd=jd_to_write,
        )
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