"""软删除的岗位不能在种子化时复活。

背景：岗位删除走的是软删（deleted_at 非空）—— 历史测评记录按 job_key 关联岗位，
物理删会留悬空引用。但软删只是让行「看不见」，行还在表里：

  - seed_from_files() 里 get_job_config(job_key) 默认过滤掉 deleted_at 非空的行，
    于是它以为「库里没有这个岗位」；
  - save_job_config() 里那条 SELECT 同样带 AND deleted_at IS NULL，
    同样看不见，于是走 INSERT 分支新建一条同名活行；
  - 唯一索引 idx_job_config_key_alive 只约束 alive 行，这条新行插得进去，
    不报错、不告警。

结果就是：HR 在岗位管理里删了岗位 → 重启服务 → 岗位诈尸，重新出现在测评端下拉里，
而且它已经通过了 complete 的校验，看起来像一个正常岗位。

这里守住四条线：
  1. 删了之后看不到：list_job_configs 不含它；
  2. 种子化不复活：seed 之后它还是不回来（overwrite=True 那条分支同样要挡）；
  3. 库里的行数不变：不凭空多出活行，也不把死行改写；
  4. HR 手工重新新增就能回来：新增后活行与死行共存，岗位恢复可见。

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


CONFIG_DIR = ROOT / "job_configs"
VICTIM = "education_erjiang"  # 被 HR 删掉的那个岗位
SURVIVOR = "hr_recruitment_intern"


def _count(job_key: str, *, alive: bool) -> int:
    clause = "deleted_at IS NULL" if alive else "deleted_at IS NOT NULL"
    with sqlite3.connect(jcs.DB_PATH) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM job_config_table WHERE job_key = ? AND {clause}",
            (job_key,),
        ).fetchone()
    return int(row[0])


def alive_rows(job_key: str) -> int:
    return _count(job_key, alive=True)


def dead_rows(job_key: str) -> int:
    return _count(job_key, alive=False)


def dead_row(job_key: str) -> dict:
    """取该 key 最新的软删行，原样返回（用来比对内容有没有被改写）。"""
    with sqlite3.connect(jcs.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM job_config_table WHERE job_key = ? AND deleted_at IS NOT NULL "
            "ORDER BY update_time DESC LIMIT 1",
            (job_key,),
        ).fetchone()
    return dict(row) if row else {}


original_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    jcs.DB_PATH = Path(tmp) / "job_configs.db"

    try:
        section("1. 先把配置种子化进库")

        imported = jcs.seed_from_files(CONFIG_DIR)
        check("三个岗位都被种子化", len(imported) == 3, str(imported))
        check("受害岗位在库里有活行", alive_rows(VICTIM) == 1, str(alive_rows(VICTIM)))
        check("is_job_soft_deleted 对活岗位是 False", jcs.is_job_soft_deleted(VICTIM) is False)
        check("is_job_soft_deleted 对不存在的岗位是 False", jcs.is_job_soft_deleted("__不存在__") is False)

        section("2. HR 在岗位管理里删掉它（软删）")

        check("软删返回 True", jcs.delete_job_config(VICTIM) is True)
        check("列表里没有了", VICTIM not in [c["job_key"] for c in jcs.list_job_configs()])
        check("默认查询查不到", jcs.get_job_config(VICTIM) is None)
        check("include_deleted 能查到（行还在）", jcs.get_job_config(VICTIM, include_deleted=True) is not None)
        check("活行 0 条", alive_rows(VICTIM) == 0, str(alive_rows(VICTIM)))
        check("死行 1 条", dead_rows(VICTIM) == 1, str(dead_rows(VICTIM)))
        check("is_job_soft_deleted 是 True", jcs.is_job_soft_deleted(VICTIM) is True)

        before = dead_row(VICTIM)
        check("死行有 config_json", bool(before.get("config_json")))

        section("3. 重启（重新种子化）不会让它复活")

        again = jcs.seed_from_files(CONFIG_DIR)
        check("它不在导入列表里", VICTIM not in again, str(again))
        check("没有岗位被重新导入", again == [], str(again))
        check("库里仍然没有活行", alive_rows(VICTIM) == 0, str(alive_rows(VICTIM)))
        check("死行还是 1 条（没多没少）", dead_rows(VICTIM) == 1, str(dead_rows(VICTIM)))
        check("它不在岗位列表里", VICTIM not in [c["job_key"] for c in jcs.list_job_configs()])
        check("它不在 include_deleted 列表的活行里", VICTIM not in [c["job_key"] for c in jcs.list_job_configs()])

        after = dead_row(VICTIM)
        check("死行的 config_json 没被改写", after.get("config_json") == before.get("config_json"))
        check("死行的 deleted_at 没被改写", after.get("deleted_at") == before.get("deleted_at"))

        section("4. overwrite=True 这条分支同样挡得住")

        forced = jcs.seed_from_files(CONFIG_DIR, overwrite=True)
        check("强制覆盖也不含它", VICTIM not in forced, str(forced))
        check("强制覆盖后仍没有活行", alive_rows(VICTIM) == 0, str(alive_rows(VICTIM)))
        check("强制覆盖后死行还是 1 条", dead_rows(VICTIM) == 1, str(dead_rows(VICTIM)))
        check("强制覆盖后它仍不在列表里", VICTIM not in [c["job_key"] for c in jcs.list_job_configs()])

        section("5. 别的岗位不受影响")

        check("另一个岗位还在", SURVIVOR in [c["job_key"] for c in jcs.list_job_configs()])
        check("另一个岗位没有死行", dead_rows(SURVIVOR) == 0, str(dead_rows(SURVIVOR)))
        check("另一个岗位活行还是 1 条", alive_rows(SURVIVOR) == 1, str(alive_rows(SURVIVOR)))

        section("6. HR 手工重新新增，它就能回来")

        import json

        config = json.loads((CONFIG_DIR / "education_erjiang.json").read_text(encoding="utf-8"))
        config["job_profile"]["responsibilities"] = ["HR 手工重新新增的职责"]
        jcs.save_job_config(config, source_type="manual", review_status="reviewed")
        check("重新新增后活行 1 条", alive_rows(VICTIM) == 1, str(alive_rows(VICTIM)))
        check("重新新增后死行仍在（历史可追）", dead_rows(VICTIM) == 1, str(dead_rows(VICTIM)))
        check("重新新增后可见", VICTIM in [c["job_key"] for c in jcs.list_job_configs()])
        check("活着的岗位就不算被删", jcs.is_job_soft_deleted(VICTIM) is False)

        section("7. 活着的岗位再种子化也不会重复灌一遍")

        last = jcs.seed_from_files(CONFIG_DIR)
        check("没有岗位被重复导入", last == [], str(last))
        check("活行还是 1 条", alive_rows(VICTIM) == 1, str(alive_rows(VICTIM)))
        check("HR 手工填的内容没被文件覆盖",
              jcs.get_job_config(VICTIM)["config"]["job_profile"]["responsibilities"] == ["HR 手工重新新增的职责"],
              str(jcs.get_job_config(VICTIM)["config"]["job_profile"]["responsibilities"]))
    finally:
        jcs.DB_PATH = original_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
