"""岗位配置拆库迁移的回归测试。

背景：job_config_table 原先和候选人会话共用 resume_sessions.db，后来拆到
独立的 job_configs.db。老库里可能躺着 HR 在后台配好的岗位。

现在服务启动时会自动跑 migrate_legacy_configs()（见 main.py 的 lifespan），
这样「换了代码忘了手动跑 scripts/migrate_job_configs_db.py」就不会让
HR 打开后台发现岗位凭空消失。

这里守住三条线：
  1. 幂等：重复执行不会重复插入，第二次全部跳过；
  2. 非破坏：源库一行都不动，表也留着；
  3. 容错：源库不存在 / 没有这张表 / 源目标同一个库，都不能把启动搞崩。

不碰真实数据库：session_store / job_config_store 的 DB_PATH 猴补丁到临时文件。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.job_config_store as jcs
import app.session_store as ss

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def make_legacy_db(path: Path, rows) -> None:
    """造一个「老版」resume_sessions.db，里面有 job_config_table。"""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE job_config_table (
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
    conn.executemany(
        "INSERT INTO job_config_table (job_key, job_name, config_json) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


original_session_db = ss.DB_PATH
original_job_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    legacy_db = tmp / "resume_sessions.db"
    target_db = tmp / "job_configs.db"

    make_legacy_db(
        legacy_db,
        [
            ("job_math_teacher", "初中数学老师", json.dumps({"job_key": "job_math_teacher"})),
            ("job_advisor", "学习顾问", json.dumps({"job_key": "job_advisor"})),
        ],
    )

    ss.DB_PATH = str(legacy_db)
    jcs.DB_PATH = str(target_db)

    try:
        section("1. 老库里的岗位配置被搬到新库")

        result = jcs.migrate_legacy_configs()
        check("搬迁条数为 2", result["migrated"] == 2, str(result))
        check("跳过条数为 0", result["skipped"] == 0, str(result))

        moved = jcs.get_job_config("job_math_teacher")
        check("按 job_key 能查到", moved is not None)
        check("岗位名对得上", moved and moved["job_name"] == "初中数学老师", str(moved))
        by_name = jcs.get_job_config_by_name("学习顾问")
        check("按岗位名也能查到", by_name is not None)
        check("按名查到的 job_key 对", by_name and by_name["job_key"] == "job_advisor", str(by_name))

        section("2. 幂等：再跑一次不会重复插入")

        again = jcs.migrate_legacy_configs()
        check("第二次新增 0 条", again["migrated"] == 0, str(again))
        check("第二次全部跳过", again["skipped"] == 2, str(again))
        conn = sqlite3.connect(target_db)
        count = conn.execute("SELECT COUNT(*) FROM job_config_table").fetchone()[0]
        conn.close()
        check("新库仍然只有 2 条", count == 2, str(count))

        section("3. 非破坏：源库一行没动")

        conn = sqlite3.connect(legacy_db)
        src_count = conn.execute("SELECT COUNT(*) FROM job_config_table").fetchone()[0]
        src_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_config_table'"
        ).fetchone()
        conn.close()
        check("源库仍然有 2 条", src_count == 2, str(src_count))
        check("源库的表还留着", src_table is not None)

        section("4. 容错：源库不存在 / 没有这张表 / 源目标同一个库")

        missing = jcs.migrate_legacy_configs(source_db=str(tmp / "not_exist.db"))
        check("源库不存在时不报错", isinstance(missing, dict))
        check("源库不存在时新增 0 条", missing["migrated"] == 0, str(missing))

        empty_db = tmp / "empty.db"
        sqlite3.connect(empty_db).close()
        empty = jcs.migrate_legacy_configs(source_db=str(empty_db))
        check("源库没有该表时不报错", isinstance(empty, dict))
        check("源库没有该表时新增 0 条", empty["migrated"] == 0, str(empty))

        same = jcs.migrate_legacy_configs(source_db=str(target_db))
        check("源目标同一个库时直接跳过", same["migrated"] == 0, str(same))
        check("源目标同一个库时不报错", isinstance(same, dict))

        section("5. 新库已有的同名配置不会被老库覆盖")

        # 直接改新库里的那一行（绕开 save_job_config 的字段校验），
        # 模拟「新库里已经有一条 HR 改过的版本」。
        conn = sqlite3.connect(target_db)
        conn.execute(
            "UPDATE job_config_table SET job_name = ? WHERE job_key = ?",
            ("学习顾问（新）", "job_advisor"),
        )
        conn.commit()
        conn.close()

        result2 = jcs.migrate_legacy_configs()
        check("已有配置被跳过", result2["skipped"] >= 1, str(result2))
        check("第二次仍然新增 0 条", result2["migrated"] == 0, str(result2))
        keep = jcs.get_job_config("job_advisor")
        check("新库里的版本没被改", keep and keep["job_name"] == "学习顾问（新）", str(keep))
    finally:
        ss.DB_PATH = original_session_db
        jcs.DB_PATH = original_job_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
