"""将 job_config_table 从 resume_sessions.db 迁移到独立的 job_configs.db。

背景：岗位配置（元数据，低频变更）与候选人会话（运行时数据，高频增长）
原来共用 resume_sessions.db，现拆分为两个独立文件，便于分别备份与归档。

用法：
    python scripts/migrate_job_configs_db.py

设计原则（遵循 SQLite 分库迁移经验）：
1. 幂等：可重复执行，已迁移的记录不会重复插入（按 job_key 去重）。
2. 非破坏：只从源库读、往目标库写，不删除源库任何数据。
3. 目标明确：源/目标路径从运行时模块的 DB_PATH 派生，不硬编码。
4. 校验：迁移后比对两边行数，不一致则报错退出。
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.paths import data_root  # noqa: E402

SOURCE_DB = data_root() / "resume_sessions.db"
TARGET_DB = data_root() / "job_configs.db"

TABLE = "job_config_table"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def main() -> None:
    print(f"源库: {SOURCE_DB}")
    print(f"目标库: {TARGET_DB}")

    if not SOURCE_DB.exists():
        print(f"源库不存在，无需迁移（{SOURCE_DB}）。")
        return

    src = sqlite3.connect(SOURCE_DB)
    src.row_factory = sqlite3.Row

    if not table_exists(src, TABLE):
        print(f"源库中没有 {TABLE} 表，无需迁移。")
        src.close()
        return

    # 读取源表全部列名（兼容未来增列）
    columns = [r[1] for r in src.execute(f"PRAGMA table_info({TABLE})").fetchall()]
    col_list = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)

    source_rows = src.execute(f"SELECT {col_list} FROM {TABLE}").fetchall()
    print(f"源库 {TABLE} 共 {len(source_rows)} 条记录。")

    # 目标库建表（复用 job_config_store.init_db 的建表逻辑）
    from app.job_config_store import init_db as init_target  # noqa: E402

    init_target()

    dst = sqlite3.connect(TARGET_DB)
    dst.row_factory = sqlite3.Row

    # 幂等插入：按 job_key 去重，已存在的跳过
    inserted = 0
    skipped = 0
    for row in source_rows:
        job_key = row["job_key"]
        existing = dst.execute(
            f"SELECT 1 FROM {TABLE} WHERE job_key = ?", (job_key,)
        ).fetchone()
        if existing:
            skipped += 1
            continue
        values = [row[col] for col in columns]
        dst.execute(
            f"INSERT INTO {TABLE} ({col_list}) VALUES ({placeholders})",
            values,
        )
        inserted += 1
    dst.commit()

    target_count = dst.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    print(f"迁移完成：新增 {inserted} 条，跳过（已存在）{skipped} 条。")
    print(f"目标库当前共 {target_count} 条记录。")

    # 校验：目标库的 job_key 集合应包含源库全部 job_key
    source_keys = {r["job_key"] for r in source_rows}
    target_keys = {
        r["job_key"] for r in dst.execute(f"SELECT job_key FROM {TABLE}").fetchall()
    }
    missing = source_keys - target_keys
    if missing:
        print(f"ERROR: 以下 job_key 未成功迁移：{missing}", file=sys.stderr)
        sys.exit(1)

    print("校验通过：源库所有 job_key 均已在目标库中。")
    print("\n注意：源库 resume_sessions.db 中的 job_config_table 表保留未删，")
    print("可在确认目标库运行正常后手动清理。")

    src.close()
    dst.close()


if __name__ == "__main__":
    main()
