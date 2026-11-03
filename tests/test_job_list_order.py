"""岗位列表顺序的回归测试。

背景：岗位列表按「最近动过的在前」排。这句话听着简单，实现上踩过坑。

update_time 原来是秒级时间戳 —— HR 连续保存两个岗位（后台点两下，
或者种子化一次导入一批），这两个岗位的 update_time 完全一样。
排序只有 ORDER BY update_time DESC 的话，谁在前就成了 SQLite 说了算：
它先扫一遍表，再拿扫出来的顺序做稳定排序。

于是顺序取决于「查询计划」而不是「产品需求」。实测：
刚加了一条 job_name 唯一索引之后，扫描方式从按行号扫变成按名字扫，
两条同时保存的岗位就从「岗位甲、岗位乙」翻成了「岗位乙、岗位甲」——
代码一行没改，顺序变了。

现在定死两件事：
  1. update_time 改成亚秒级（_now_precise），正常路径下压根不会撞时间；
  2. ORDER BY update_time DESC, id DESC —— 兜底次序写死，
     同一时刻保存的后建的排前面，这是产品定的，不是 SQLite 定的。

这里守住三条线：
  1. 列表顺序是「最近动过的在前」，改一个旧岗位它就跳到最前；
  2. 时间戳撞在一起（秒级历史数据、并发写入）时，顺序依然是确定的；
  3. 换索引 / 换查询计划，顺序不能翻脸。

不碰真实数据库：job_config_store 的 DB_PATH 猴补丁到临时文件。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.job_config_store as jcs

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def valid_config(job_key: str, job_name: str) -> dict:
    """一份能通过校验的配置。"""
    return {
        "job_key": job_key,
        "job_name": job_name,
        "job_profile": {
            "responsibilities": ["课程销售转化"],
            "work_time": "周一至周六 9:00-18:00",
            "salary": "6000-10000",
        },
        "dialog_check_items": [
            "是否接受周末排班",
            "是否接受课程销售性质",
        ],
        "exp_keywords": ["课程销售", "招生", "续费"],
        "score_rules": {
            "add_score": [
                {
                    "id": "sales_exp",
                    "field": "exp_tags",
                    "op": "has_any",
                    "value": ["课程销售"],
                    "score": 3,
                    "desc": "有课程销售相关经历",
                }
            ],
            "minus_score": [],
        },
        "level_threshold": {"high_min": 5, "mid_min": 3},
        "alternate_role_mapping": [],
    }


def names() -> list[str]:
    """当前活着的岗位，按列表顺序。"""
    return [item["job_name"] for item in jcs.list_job_configs()]


original_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    jcs.DB_PATH = Path(tmp) / "job_configs.db"

    try:
        section("1. 列表顺序：最近动过的在前")

        jcs.save_job_config(valid_config("ord_a", "岗位甲"), source_type="manual")
        jcs.save_job_config(valid_config("ord_b", "岗位乙"), source_type="manual")
        jcs.save_job_config(valid_config("ord_c", "岗位丙"), source_type="manual")
        check(
            "后保存的排前面",
            names() == ["岗位丙", "岗位乙", "岗位甲"],
            str(names()),
        )

        # 改一个最老的岗位，它就该跳到最前面 —— 这是「最近动过的在前」的意思
        jcs.save_job_config(valid_config("ord_a", "岗位甲"), source_type="manual")
        check(
            "改动过的旧岗位跳到最前",
            names() == ["岗位甲", "岗位丙", "岗位乙"],
            str(names()),
        )

        # 读两次顺序得一样
        check("连读两次顺序一致", names() == names())

        section("2. 时间戳撞在一起时，顺序也是确定的")

        # 正常保存走的是亚秒级时间戳，同一秒里保存两个岗位也不会撞。
        rows = jcs.list_job_configs()
        stamps = [item["update_time"] for item in rows]
        check(
            "update_time 是亚秒级的（带小数部分）",
            all("." in stamp for stamp in stamps),
            str(stamps),
        )
        check(
            "几个岗位的 update_time 互不相同",
            len(set(stamps)) == len(stamps),
            str(stamps),
        )

        # 但历史数据是秒级的，并发写入也可能撞 —— 撞了顺序也得是确定的。
        # 直接写三行 update_time 完全一样的数据，走 list_job_configs 读。
        conn = sqlite3.connect(jcs.DB_PATH)
        try:
            conn.executemany(
                "INSERT INTO job_config_table "
                "(job_key, job_name, source_type, config_json, review_status, create_time, update_time) "
                "VALUES (?, ?, 'manual', '{}', 'pending', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                [
                    ("tie_a", "撞时间A"),
                    ("tie_b", "撞时间B"),
                    ("tie_c", "撞时间C"),
                ],
            )
            conn.commit()
        finally:
            # 必须显式 close：with sqlite3.connect(...) 只管提交/回滚，
            # 不关连接 —— 临时目录会被这个还开着的句柄锁住，删不掉。
            conn.close()

        tie_order = names()
        check(
            "时间戳相同按 id 倒序（后建的在前）",
            tie_order[-3:] == ["撞时间C", "撞时间B", "撞时间A"],
            str(tie_order),
        )
        check(
            "时间戳更新的排前面",
            tie_order[:3] == ["岗位甲", "岗位丙", "岗位乙"],
            str(tie_order),
        )

        section("3. 换索引不能把顺序翻过来（查询计划不是产品需求）")

        # 这一节在一个独立的小库上做，手工控制索引，
        # 好把「顺序由查询计划决定」这件事复现出来。
        tie_db = Path(tmp) / "tie.db"
        conn = sqlite3.connect(tie_db)
        try:
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
                "INSERT INTO job_config_table "
                "(job_key, job_name, source_type, config_json, review_status, create_time, update_time) "
                "VALUES (?, ?, 'manual', '{}', 'pending', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                # 名字故意按「行号顺序 ≠ 字典序」插：
                # 一(U+4E00) < 三(U+4E09) < 二(U+4E8C)。
                # 这样扫表（按行号）和走名字索引（按字典序）出来的顺序必然不同，
                # 「顺序被查询计划翻了」才测得出来。
                [("t_a", "第二岗"), ("t_b", "第一岗"), ("t_c", "第三岗")],
            )
            conn.commit()

            # 现在的排序：update_time 相同就按 id 倒序 —— 全序，不看查询计划。
            new_sql = (
                "SELECT job_name FROM job_config_table WHERE deleted_at IS NULL "
                "ORDER BY update_time DESC, id DESC"
            )
            # 之前的排序：只按 update_time —— 时间戳一撞，顺序就由扫描顺序决定。
            old_sql = (
                "SELECT job_name FROM job_config_table WHERE deleted_at IS NULL "
                "ORDER BY update_time DESC"
            )

            def run(sql: str) -> list[str]:
                return [row[0] for row in conn.execute(sql).fetchall()]

            before_new = run(new_sql)
            before_old = run(old_sql)

            # 加上 job_name 唯一索引（就是上线时加的那条）
            conn.execute(
                "CREATE UNIQUE INDEX idx_job_config_name_alive "
                "ON job_config_table(job_name) WHERE deleted_at IS NULL"
            )
            conn.commit()

            after_new = run(new_sql)
            after_old = run(old_sql)

            check(
                "现在的排序：加索引前后一样",
                before_new == after_new,
                f"{before_new} -> {after_new}",
            )
            check(
                # id 是 1=第二岗 2=第一岗 3=第三岗，id 倒序 = 第三岗、第一岗、第二岗
                "现在的排序：时间戳相同时按 id 倒序（不是按名字）",
                after_new == ["第三岗", "第一岗", "第二岗"],
                str(after_new),
            )
            check(
                "（对照）之前的排序确实会被索引翻脸 —— 这就是翻车现场",
                before_old != after_old,
                f"{before_old} -> {after_old}",
            )
        finally:
            conn.close()
    finally:
        jcs.DB_PATH = original_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
