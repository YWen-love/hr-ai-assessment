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
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.job_schema import normalize_job_config, split_issues, validate_job_config
from app.paths import data_root, resource_path

# 来源优先级（从低到高）：
#   auto_llm   —— 模型纯根据 JD 生成的，最低优先级，开发者手动 json 可覆盖
#   key_points —— HR 在工作台输入打分关键点后模型生成的
#   manual     —— 开发者写在 job_configs/*.json 里的，或 HR 手动编辑 JSON 保存的，最高
VALID_SOURCE_TYPES = ("auto_llm", "key_points", "manual")
VALID_REVIEW_STATUS = ("pending", "reviewed")

TABLE_NAME = "job_config_table"

logger = logging.getLogger(__name__)

DB_PATH = data_root() / "job_configs.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class JobConfigError(ValueError):
    """配置非法。调用方应把 errors 原样返回给 HR。"""

    def __init__(self, errors: List[str]):
        super().__init__("；".join(errors))
        self.errors = errors


class JobNameTakenError(Exception):
    """岗位显示名（job_name）已经被别的岗位占用了。

    job_name 是 HR 唯一看得见的岗位标识 —— 测评页下拉、报告标题、
    候选人链接里全是它。两个岗位同名，HR 分不清哪个是哪个；更糟的是
    按名字反查配置时只会拿到其中一条（见 get_job_config_by_name），
    测评有可能拿错岗位去打分。

    所以重名必须在写入时就拦住，而不是靠事后人工去重。

    刻意不继承 JobConfigError：那是「配置写得不对」，会让 HR 去改 JSON；
    这件事是「名字被占了」，改个名字就好 —— 两回事，提示也不一样。
    """

    def __init__(self, job_name: str, existing_job_key: str = ""):
        self.job_name = job_name
        self.existing_job_key = existing_job_key
        self.message = f"岗位已存在：{job_name}"
        super().__init__(self.message)


class JobResolveError(Exception):
    """按岗位名定位测评岗位失败。

    reason 区分失败原因，调用方据此给 HR 不同提示（同一件事只给一种说法）：

      BLANK   —— 没选岗位；
      MISSING —— 配置表里没有这个岗位名（从没配过）；
      DELETED —— 配置表里有，但被 HR 软删了；
      BROKEN  —— 配置在，内容却不可用（config_json 坏了）。
    """

    BLANK = "blank"
    MISSING = "missing"
    DELETED = "deleted"
    BROKEN = "broken"

    def __init__(self, reason: str, job_name: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.job_name = job_name


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """开一个到岗位配置库的连接，退出 with 块时关掉。

    sqlite3.Connection 自带的 __exit__ 只提交/回滚，不会 close()
    —— 连接会一直挂着：Windows 上 .db 文件被锁住（临时目录删不掉），
    长时间跑还会漏句柄。所以这里自己管关闭，事务语义保持不变。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_job_name(name: str) -> str:
    """岗位显示名的比较口径：去掉所有空白（含全角空格 U+3000）。

    HR 在表单里手打岗位名，前后空格、词中间的空格都可能不一致。
    「初中学习顾问」和「初中 学习顾问」在业务上是同一个岗位，
    不能因为一个空格就把它们变成两个岗位 —— 那正是重名要拦的事。
    """
    return re.sub(r"\s+", "", name or "")


def _find_name_conflict(
    conn: sqlite3.Connection, job_name: str, exclude_job_key: str
) -> Optional[sqlite3.Row]:
    """找出「另一个活着的岗位」是不是已经用了这个名字。

    exclude_job_key 是正在保存的那一行的 key —— 它自己叫这个名字不算冲突
    （改配置但不改名字是正常的保存路径）。

    逐行比对而不是拼 SQL：job_name 的比较口径是「去掉所有空白」，
    这个口径没法直接写进 SQL 条件；而岗位数量是几十级别，全表扫一次
    完全不心疼。
    """
    target = _normalize_job_name(job_name)
    if not target:
        return None
    rows = conn.execute(
        "SELECT job_key, job_name FROM job_config_table WHERE deleted_at IS NULL"
    ).fetchall()
    for row in rows:
        if row["job_key"] == exclude_job_key:
            continue
        if _normalize_job_name(row["job_name"]) == target:
            return row
    return None


# job_name 唯一索引建不出来时（存量库里有重名），记下这个库路径，
# 别每次保存岗位都去全表扫一遍重名、每次都刷一条 warning。
_name_index_failed_for: set[str] = set()


def _ensure_name_unique_index(conn: sqlite3.Connection) -> None:
    """给活行加一条 job_name 唯一索引（兜底）。

    主要防线是 save_job_config 里的重名检查；这条索引管的是
    「代码没走那条路」的写入 —— 并发、裸 SQL、老脚本。

    存量库里如果已经有重名的行，CREATE UNIQUE INDEX 会直接报错。
    这时候不动存量数据（删岗位是 HR 的决定，不是程序该替人做的），
    只记一条 warning，重名照旧由保存时的检查拦住。
    """
    db_key = str(DB_PATH)
    if db_key in _name_index_failed_for:
        return

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_job_config_name_alive'"
    ).fetchone()
    if exists:
        return

    duplicate = conn.execute(
        "SELECT job_name FROM job_config_table WHERE deleted_at IS NULL "
        "GROUP BY job_name HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        _name_index_failed_for.add(db_key)
        logger.warning(
            "岗位配置表里已存在重名岗位（如「%s」），未建立 job_name 唯一索引；"
            "重名仍会在保存时被拦住。",
            duplicate["job_name"],
        )
        return

    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_job_config_name_alive
            ON job_config_table(job_name) WHERE deleted_at IS NULL
            """
        )
    except sqlite3.IntegrityError:
        _name_index_failed_for.add(db_key)
        logger.warning(
            "建立 job_name 唯一索引失败（存量数据里有重名），重名仍会在保存时被拦住。"
        )


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
        _ensure_name_unique_index(conn)
        conn.commit()


def _now() -> str:
    """秒级时间戳：给人看的 —— 创建时间会直接显示在岗位列表里。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_precise() -> str:
    """亚秒级时间戳：给「最近更新」排序用的。

    岗位列表按「最近动过的在前」排。秒级精度下，同一秒里保存的两个岗位
    时间戳一模一样，谁在前就成了 SQLite 随便定的（之前靠扫描顺序凑巧稳定，
    后来加了个索引就翻了 —— 顺序不是产品定的，是查询计划定的）。

    亚秒级 + 显式次序兜底，顺序才是真的可预期。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    """校验后写入。非法配置抛 JobConfigError，不落库。

    岗位显示名撞了已有的岗位抛 JobNameTakenError（同样不落库）——
    job_name 在业务上必须唯一，理由见那个异常类的说明。
    """
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
        # 重名拦截：job_name 是 HR 唯一看得见的岗位标识，
        # 两个岗位同名就分不清谁是谁，按名字反查时也只会拿到其中一条
        # （见 get_job_config_by_name）。编辑自己（同一个 job_key）不算撞名。
        conflict = _find_name_conflict(conn, normalized["job_name"], job_key)
        if conflict is not None:
            raise JobNameTakenError(normalized["job_name"], conflict["job_key"])

        existing = conn.execute(
            "SELECT id, create_time, raw_jd FROM job_config_table "
            "WHERE job_key = ? AND deleted_at IS NULL",
            (job_key,),
        ).fetchone()

        try:
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
                        _now_precise(),
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
                        _now_precise(),
                    ),
                )
                job_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            # 撞上唯一索引：多半是并发写入时另一个请求抢先把同名岗位建了。
            # 只有确认确实是重名才翻译成 JobNameTakenError，
            # 其它完整性错误原样抛出去，别把真问题说成重名。
            conflict = _find_name_conflict(conn, normalized["job_name"], job_key)
            if conflict is not None:
                raise JobNameTakenError(normalized["job_name"], conflict["job_key"]) from exc
            raise
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

    HR 上传表单的下拉框显示的是中文岗位名，但测评链路全程用 job_key 做
    内部标识。这里提供 job_name → config 的反查，让 /hr/parse-link 能在
    创建会话时把 job_key 一并存进去。

    取第一条命中：job_name 在业务上应唯一 —— save_job_config 写入时会拦重名，
    但表上并没有 job_name 的唯一约束（只有 job_key 有），老库里可能已经躺着
    几条重名的脏数据（那是重名拦截上线之前留下的）。真撞上了就取最新更新的
    那条，避免测评链路因脏数据整条崩掉。
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM job_config_table WHERE job_name = ? AND deleted_at IS NULL "
            "ORDER BY update_time DESC, id DESC LIMIT 1",
            (job_name,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def _find_deleted_by_name(job_name: str) -> Optional[Dict[str, Any]]:
    """按岗位名找一条软删除的行（最近更新的那条）。"""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM job_config_table WHERE job_name = ? AND deleted_at IS NOT NULL "
            "ORDER BY update_time DESC, id DESC LIMIT 1",
            (job_name,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def resolve_job_by_name(job_name: str) -> Dict[str, Any]:
    """按岗位显示名定位「测评要用的那个岗位配置」。只认岗位配置表。

    为什么只有这一张表算数：测评链路从头到尾读的就是它
    （assessment_service → get_job_config_object → job_config_table），
    配置表里没有的岗位一定测不了 —— 配置表里有的岗位就一定能测。

    之前这里还叠了一道知识库（rag_knowledge.db）的闸：知识库里查不到这个岗位
    就直接 400。那套知识库是 job_intro.md 的静态快照，岗位管理里新建的岗位
    永远不会出现在里面。结果是 HR 明明配好了岗位，点生成链接却被拦下，
    提示还让他「选择知识库中的有效岗位」——两份名单并存，就有两套互相打架的
    答案，HR 拿哪份都对不上。知识库已经整个退役（app/knowledge_base.py 已删），
    这道闸也一并没了。

    失败抛 JobResolveError，reason 见类定义；调用方按 reason 给一句话，
    不要自己再猜一套判断逻辑。
    """
    name = (job_name or "").strip()
    if not name:
        raise JobResolveError(JobResolveError.BLANK)

    record = get_job_config_by_name(name)
    if record is None:
        # 没命中活行，看看是不是被 HR 软删了 ——
        # 软删的行还在表里、名字也还在。HR 看到的是「我明明删了它，
        # 怎么还在报错」，这两种情况必须分开说。
        if _find_deleted_by_name(name) is not None:
            raise JobResolveError(JobResolveError.DELETED, name)
        raise JobResolveError(JobResolveError.MISSING, name)

    config = record.get("config")
    if not isinstance(config, dict) or not config.get("score_rules") or not config.get("job_profile"):
        # 配置在，内容却不能用（多半是 config_json 写坏了）。
        # 这种状态测出来的结果没有意义，直说比硬测一轮强。
        raise JobResolveError(JobResolveError.BROKEN, name)
    return record


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
    # 次序必须写死：光 ORDER BY update_time DESC 的话，同一秒保存的两个岗位
    # 时间戳一样，谁在前由 SQLite 的查询计划决定（换条索引就翻脸）。
    # id DESC 兜底：同一时刻保存的，后建的那个排前面。
    sql += " ORDER BY update_time DESC, id DESC"
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
                (_now(), _now_precise(), job_key),
            )
        conn.commit()
        return cursor.rowcount > 0


def is_job_soft_deleted(job_key: str) -> bool:
    """这个 job_key 是不是被 HR 删过（当前只剩软删除的行）。

    软删的行 deleted_at 非空，但表上可能同时还活着一个同名 key ——
    唯一索引 idx_job_config_key_alive 只约束 alive 行，所以「删掉之后
    HR 又手工重建」是允许的，这种情况岗位不算被删。

    判断口径因此是「有没有活着的行」：
      - 有活行 → 没删，走正常更新；
      - 只有死行 → 删过，种子化不许把它复活。
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS alive "
            "FROM job_config_table WHERE job_key = ?",
            (job_key,),
        ).fetchone()
    if not row or not row["total"]:
        return False
    return not row["alive"]


def update_job_config_meta(job_key: str, *, review_status: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """只改元信息（目前只有复核状态）。人工复核动作走这里。"""
    init_db()
    if review_status is not None and review_status not in VALID_REVIEW_STATUS:
        raise JobConfigError([f"review_status 必须是 {VALID_REVIEW_STATUS} 之一"])
    with _connect() as conn:
        conn.execute(
            "UPDATE job_config_table SET review_status = COALESCE(?, review_status), update_time = ? WHERE job_key = ?",
            (review_status, _now_precise(), job_key),
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


_SECTION_PATTERN = re.compile(r"^===(.+?)===\s*$", re.MULTILINE)


def _parse_marked_sections(content: str) -> Dict[str, str]:
    """把 ===岗位名=== 标记的正文切成 {岗位名: 正文}。

    job_intro.md 就是这种写法：每个岗位一段，用 ===初中学习顾问=== 这样的
    标记隔开。这段解析原先住在 app/knowledge_base.py 里，知识库整个退役之后
    它就只剩这一个用户 —— 跟着唯一的用户走，别再单开一个模块。

    段内还可能按「岗位名称：xxx」再切一刀（一个岗位段落里写了好几个岗位），
    这种子块优先用它自己的名字，前面的引言仍归外层标记。
    """
    markers = list(_SECTION_PATTERN.finditer(content))
    sections: Dict[str, str] = {}
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
            (jd_text, _now_precise(), job_key),
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

    被 HR 软删过的岗位不会被种子化复活：删了就是删了，要恢复得 HR
    自己在后台重新新增（见 is_job_soft_deleted）。
    """
    init_db()
    from pathlib import Path

    imported: List[str] = []
    skipped_deleted: List[str] = []
    skipped_duplicate: List[str] = []
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
        if existing is None and is_job_soft_deleted(job_key):
            # HR 在岗位管理里删过这个岗位。删除是软删（历史测评记录还按 job_key
            # 指着它，物理删会留悬空引用），但对 HR 来说「删了就是删了」——
            # 之前这里看不见软删的行，save_job_config() 会 INSERT 一条同名活行，
            # 于是重启一次岗位就诈尸，重新出现在测评端的下拉里。
            skipped_deleted.append(job_key)
            continue

        intro_jd = _pick_jd(
            jd_sections,
            (existing or {}).get("job_name"),
            config.get("job_name"),
        )

        existing_source = (existing or {}).get("source_type")

        # 优先级覆盖规则：
        #   - 库里没有 → 直接新增
        #   - 库里是 auto_llm（模型纯 JD 生成，最低优先级）→ 用开发者 json 覆盖，
        #     source_type 升为 manual
        #   - 库里是 key_points 或 manual（更高优先级）→ 不覆盖配置，只补 raw_jd
        #   - overwrite=True 强制覆盖所有（运维用，正常启动不走）
        if existing and not overwrite and existing_source != "auto_llm":
            # 库里已经是更高优先级的来源，不能用文件里的 config_json 覆盖，
            # 但 JD 原文是空的话可以顺手补上 —— 只动 raw_jd 一列。
            _backfill_raw_jd(job_key, file_jd or intro_jd)
            continue

        # overwrite=True 或 existing_source == "auto_llm" 时：
        # 先比对文件 config 和库里 config_json，内容没变就跳过，避免无谓的覆盖操作。
        if existing and overwrite:
            existing_json = (existing.get("config_json") or "").strip()
            new_json = json.dumps(config, ensure_ascii=False, sort_keys=True)
            existing_sorted = ""
            try:
                existing_sorted = json.dumps(json.loads(existing_json), ensure_ascii=False, sort_keys=True)
            except (ValueError, TypeError):
                pass
            if existing_sorted == new_json:
                # 内容没变，跳过
                _backfill_raw_jd(job_key, file_jd or intro_jd)
                continue

        if existing and not file_jd:
            # 强制覆盖配置时，不用岗位介绍去盖掉 HR 亲手维护的 JD 原文。
            # raw_jd 传 None 表示「这次不动 JD」。
            jd_to_write = None
        else:
            jd_to_write = file_jd or intro_jd or None

        # 文件里的配置视为人工维护，默认直接生效
        # 库里已有记录时：
        #   - 原 source_type 是 auto_llm（模型纯 JD 生成）→ 开发者 json 覆盖，升为 manual
        #   - 原 source_type 是 key_points/manual → 保留（更高优先级不动）
        # overwrite=True 强制覆盖时也保留原 source_type，不重置
        try:
            if existing:
                promoted_source = (
                    "manual" if existing_source == "auto_llm"
                    else existing_source
                )
                save_job_config(
                    config,
                    source_type=promoted_source,
                    review_status=existing.get("review_status", "reviewed"),
                    raw_jd=jd_to_write,
                )
            else:
                save_job_config(
                    config,
                    source_type="manual",
                    review_status="reviewed",
                    raw_jd=jd_to_write,
                )
        except JobNameTakenError as exc:
            # 文件里的岗位名和库里某个岗位撞了。种子化不该因为这个把
            # 服务启动搞崩（那是 HR 自己在后台建出来的同名岗位），
            # 跳过并记一条 warning，重名照旧由后台保存时的检查拦住。
            skipped_duplicate.append(f"{job_key}（撞上「{exc.job_name}」）")
            continue
        imported.append(job_key)

    if skipped_deleted:
        logger.info(
            "跳过的已删除岗位（HR 曾删除，种子化不复活）：%s", ", ".join(skipped_deleted)
        )
    if skipped_duplicate:
        logger.warning(
            "跳过的重名岗位配置（job_name 已被别的岗位占用）：%s",
            ", ".join(skipped_duplicate),
        )
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


def retire_legacy_knowledge_db() -> bool:
    """删掉退役的知识库文件 rag_knowledge.db。

    知识库（app/knowledge_base.py）已经整个退役：它把 job_intro.md 灌进
    rag_knowledge.db，却从来没人读它 —— 一份只写不读的库。

    但它写进去的 jobs 表是一份「知识库里的岗位名单」，跟配置表那份天然对不上
    （HR 在岗位管理里新建的岗位永远不会出现在里面）。留着这个文件，就等于
    在服务器上留着一份会误导人的第二份名单。

    启动时自动清一次，不留需要人手执行的迁移步骤：文件不存在就什么都不做，
    删不掉（权限/占用）也只记日志、不影响服务启动。

    Returns:
        True 表示确实删掉了文件；False 表示本来就没有或删不掉。
    """
    legacy_db = data_root() / "rag_knowledge.db"
    try:
        legacy_db.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("清理退役的知识库文件失败：%s", legacy_db)
        return False
    return True
