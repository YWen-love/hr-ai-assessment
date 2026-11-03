"""岗位显示名（job_name）唯一性的回归测试。

背景：岗位配置表上原本只有 job_key 的唯一索引，job_name 谁都能重复。
HR 在后台新增岗位时，只要 job_key 是新的，哪怕岗位名和已有的岗位一模一样，
也照样新增成功 —— 于是「教育销售顾问」这种名字库里能躺着好几条。

这不是「多了几行重复数据」的小事，按名字反查配置时
（get_job_config_by_name / resolve_job_by_name）只会拿到其中一条
（最新更新的那条），测评有可能拿错岗位去打分；HR 在下拉框里看到两个
一模一样的岗位名也分不清谁是谁。

所以重名必须在**写入时**就拦住，提示「当前岗位已存在」。

这里守住八条线：
  1. 新增重名被拦住：抛 JobNameTakenError，且不落库；
  2. 编辑自己不受影响：同一个 job_key 换个配置再存，名字不变也能存；
  3. 空白差异算重名：「教育销售 顾问」「　教育销售顾问　」和
     「教育销售顾问」是同一个岗位名（半角空格、全角空格都算）；
  4. 软删的岗位不挡路：删掉的岗位名可以重新用；
  5. 数据库层面有唯一索引兜底：并发 / 裸 SQL 写入也进不来；
  6. UI 提示说人话：出现「当前岗位已存在」，而不是让 HR 去改 JSON；
  7. API 层返回 409：不是配置写坏了，是名字要换一个；
  8. 种子化遇到重名跳过而不是把服务启动搞崩。

不碰真实数据库：job_config_store 的 DB_PATH 猴补丁到临时文件。
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
from app.job_config_store import JobNameTakenError

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def valid_config(job_key: str, job_name: str) -> dict:
    """一份能通过校验的配置。job_key / job_name 可改，用来造重名。"""
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


def alive_count() -> int:
    """库里活着的岗位数。"""
    return len(jcs.list_job_configs())


original_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    jcs.DB_PATH = Path(tmp) / "job_configs.db"

    try:
        section("1. 新增重名岗位被拦住")

        jcs.save_job_config(valid_config("sales_a", "教育销售顾问"), source_type="manual")
        check("第一个岗位建好了", alive_count() == 1, str(alive_count()))

        raised = None
        try:
            jcs.save_job_config(valid_config("sales_b", "教育销售顾问"), source_type="manual")
        except JobNameTakenError as exc:
            raised = exc

        check("重名新增被拦住（抛 JobNameTakenError）", raised is not None)
        check(
            "异常里带上了冲突的岗位名",
            raised is not None and raised.job_name == "教育销售顾问",
            repr(getattr(raised, "job_name", None)),
        )
        check(
            "异常里带上了已占用的 job_key",
            raised is not None and raised.existing_job_key == "sales_a",
            repr(getattr(raised, "existing_job_key", None)),
        )
        check("重名的岗位没有落库", alive_count() == 1, str(alive_count()))
        check("库里查不到那个重名的 job_key", jcs.get_job_config("sales_b") is None)

        section("2. 编辑自己不受重名拦截")

        # 同一个 job_key、名字不变，只是改了配置 —— 这是最常见的保存路径，
        # 要是被自己的名字挡住，HR 就没法改任何岗位了。
        edited = valid_config("sales_a", "教育销售顾问")
        edited["job_profile"]["salary"] = "7000-12000"
        saved = jcs.save_job_config(edited, source_type="manual")
        check("编辑自己保存成功", saved["job_name"] == "教育销售顾问", str(saved.get("job_name")))
        check("编辑没有多出行", alive_count() == 1, str(alive_count()))
        check("改动确实写进去了", saved["config"]["job_profile"]["salary"] == "7000-12000", str(saved["config"]["job_profile"]))

        section("3. 空白差异（含全角空格）算重名")

        # HR 手打岗位名，前后空格、词中间的空格都可能不一致。
        # 「初中学习顾问」和「初中 学习顾问」在业务上是同一个岗位，
        # 不能因为一个空格就把它们变成两个岗位。
        for label, name in (
            ("中间多一个半角空格", "教育销售 顾问"),
            ("前后半角空格", " 教育销售顾问 "),
            ("前后全角空格 U+3000", "　教育销售顾问　"),
            ("中间全角空格", "教育销售　顾问"),
        ):
            hit = None
            try:
                jcs.save_job_config(valid_config("sales_tmp", name), source_type="manual")
            except JobNameTakenError as exc:
                hit = exc
            check(f"{label}算重名", hit is not None)
        check("上面四次都没落库", alive_count() == 1, str(alive_count()))

        section("4. 软删的岗位不挡新岗位")

        check("删除成功", jcs.delete_job_config("sales_a"))
        check("删完没有活着的岗位了", alive_count() == 0, str(alive_count()))
        check("软删的行还在（历史测评要指得到）", jcs.get_job_config("sales_a", include_deleted=True) is not None)

        # 名字被释放了，可以重新用
        jcs.save_job_config(valid_config("sales_c", "教育销售顾问"), source_type="manual")
        check("同名岗位可以重建", alive_count() == 1, str(alive_count()))
        check("重建的是新 key", jcs.get_job_config("sales_c") is not None)

        section("5. 数据库层面有 job_name 唯一索引兜底")

        # 主要防线是 save_job_config 里的检查；这条索引管的是「代码没走那条路」
        # 的写入 —— 并发、裸 SQL、老脚本。
        conn = sqlite3.connect(jcs.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='job_config_table'"
            ).fetchall()
        finally:
            conn.close()
        index_names = {row[0] for row in rows}
        check("job_key 唯一索引还在", "idx_job_config_key_alive" in index_names, str(index_names))
        check("job_name 唯一索引建起来了", "idx_job_config_name_alive" in index_names, str(index_names))

        # 裸 SQL 直接插一条重名 —— 唯一索引必须挡下来
        conn = sqlite3.connect(jcs.DB_PATH)
        try:
            conn.execute(
                "INSERT INTO job_config_table "
                "(job_key, job_name, source_type, config_json, review_status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("raw_sql_dup", "教育销售顾问", "manual", "{}", "pending"),
            )
            conn.commit()
            blocked = False
        except sqlite3.IntegrityError:
            blocked = True
        finally:
            # 必须显式 close：with sqlite3.connect(...) 只管提交/回滚，
            # 不关连接 —— 临时目录会被这个还开着的句柄锁住，删不掉。
            conn.close()
        check("裸 SQL 插重名被唯一索引挡下", blocked)

        section("6. UI 提示说人话（不是让 HR 去改 JSON）")

        import app.gradio_ui as ui

        dup_text = json.dumps(valid_config("ui_dup", "教育销售顾问"), ensure_ascii=False)
        notice, key = ui._persist_config(dup_text, None)
        check("UI 返回了提示", bool(notice), repr(notice))
        check("提示里有「当前岗位已存在」", "当前岗位已存在" in notice, repr(notice)[:200])
        check("提示里有冲突的岗位名", "教育销售顾问" in notice, repr(notice)[:200])
        check("没落库（返回的 key 是空串）", key == "", repr(key))
        check("库里没有那个 UI 提交的岗位", jcs.get_job_config("ui_dup") is None)

        # 换个名字就应该能存进去
        ok_text = json.dumps(valid_config("ui_ok", "课程顾问"), ensure_ascii=False)
        ok_notice, ok_key = ui._persist_config(ok_text, None)
        check("不重名时正常保存", ok_key == "ui_ok", repr(ok_key))
        check("保存成功的提示是绿色", "已新增岗位" in ok_notice, repr(ok_notice)[:200])

        section("7. API 层返回 409（不是 422）")

        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        check(
            "main.py 引入了 JobNameTakenError",
            "JobNameTakenError" in main_source,
        )

        # 用 AST 找 except JobNameTakenError 分支里的 HTTPException 状态码，
        # 比文本匹配稳：缩进、换行、变量名都不影响判断。
        import ast

        tree = ast.parse(main_source)
        taken_codes: list[int] = []
        config_codes: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            name = caught.id if isinstance(caught, ast.Name) else getattr(caught, "attr", "")
            if name not in ("JobNameTakenError", "JobConfigError"):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (isinstance(func, ast.Name) and func.id == "HTTPException"):
                    continue
                code = None
                for keyword in call.keywords:
                    if keyword.arg == "status_code":
                        code = keyword.value.value
                if code is None:
                    continue
                (taken_codes if name == "JobNameTakenError" else config_codes).append(code)

        check(
            "重名走 409",
            taken_codes and all(code == 409 for code in taken_codes),
            f"JobNameTakenError 分支的状态码：{taken_codes}",
        )
        check(
            "至少两处 API 端点处理了重名（保存 + 复制）",
            len(taken_codes) >= 2,
            f"{len(taken_codes)} 处",
        )
        check(
            "配置写坏仍然是 422（两回事，提示语不一样）",
            config_codes and all(code == 422 for code in config_codes),
            f"JobConfigError 分支的状态码：{config_codes}",
        )

        section("8. 种子化遇到重名跳过，而不是把服务搞崩")

        # 模拟：HR 在后台建了一个叫「人力实习生」的岗位，
        # 而 job_configs/ 里也有一份同名配置。种子化不能因此启动失败。
        jcs.DB_PATH = Path(tmp) / "seed.db"
        jcs.init_db()
        jcs.save_job_config(valid_config("intern_manual", "人力实习生"), source_type="manual")

        imported = jcs.seed_from_files(ROOT / "job_configs")
        check("种子化没有抛异常", True)
        check(
            "撞名的那个岗位被跳过",
            "hr_recruitment_intern" not in imported,
            str(imported),
        )
        check(
            "HR 自己建的那个岗位还在",
            jcs.get_job_config("intern_manual") is not None,
        )
        check(
            "撞名的配置没被写进库",
            jcs.get_job_config("hr_recruitment_intern") is None,
        )
        check("另外两个岗位照常种子化", len(imported) == 2, str(imported))
    finally:
        jcs.DB_PATH = original_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
