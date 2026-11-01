"""/hr/parse-link 只认岗位配置表，不再叠知识库那道闸。

背景：测评链路从头到尾读的就是岗位配置表 job_config_table
（assessment_service → get_job_config_object → job_config_table），
所以「能不能测」这件事只有这一张表说了算。

可 /hr/parse-link 原来叠了两道闸：
  闸 A（知识库 rag_knowledge.db）：get_job_knowledge(job_name)，
        查不到就 400「请选择知识库中的有效岗位。」
  闸 B（配置表）：get_job_config_by_name(job_name)，查不到就 400。

闸 A 是道假闸：它的返回值压根没人用，只能拒绝、不能放行；而知识库是
job_intro.md / screen_rules.md 的静态快照，岗位管理里新建的岗位永远不会
出现在里面。于是 HR 明明配好了岗位，点生成链接却被拦下，提示还让他去
「知识库」里选 —— 两份名单并存，就有两套互相打架的答案。

这里守住八条线：
  1. 正常岗位：按岗位名能定位到 job_key 和配置；
  2. 没选岗位：BLANK；
  3. 从没配过：MISSING；
  4. 被 HR 软删过：DELETED（和「从没配过」必须分开说）；
  5. 软删的行不复活：活行查询看不见它；
  6. 配置坏了：BROKEN；
  7. 知识库之外的岗位照样能用（岗位管理新建的岗位必须能测）；
  8. main.py 里不再有知识库那道闸，错误提示也不再指向知识库。

不碰真实数据库：job_config_store 的 DB_PATH 猴补丁到临时文件；
main.py 只做源码级检查 + 单独抽出报错映射函数来跑（不 import 整个应用）。
"""

from __future__ import annotations

import ast
import json
import py_compile
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

import app.job_config_store as jcs
from app.job_config_store import JobResolveError

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


CONFIG_DIR = ROOT / "job_configs"


def _resolve_reason(job_name: str) -> str:
    """跑一次解析，返回失败原因；成功则返回空串。"""
    try:
        jcs.resolve_job_by_name(job_name)
    except JobResolveError as error:
        return error.reason
    return ""


def _raw_insert(job_key: str, job_name: str, config_obj, *, source_type: str = "auto_llm") -> None:
    """绕过校验直接塞一行配置进去 —— 模拟存量脏数据 / 老版本写坏的行。"""
    with sqlite3.connect(jcs.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO job_config_table "
            "(job_key, job_name, source_type, raw_jd, config_json, review_status, create_time, update_time) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            (
                job_key,
                job_name,
                source_type,
                None,
                json.dumps(config_obj, ensure_ascii=False),
                "pending",
            ),
        )
        conn.commit()


original_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    jcs.DB_PATH = Path(tmp) / "job_configs.db"

    try:
        # ------------------------------------------------------------------
        section("1. 正常岗位：按岗位名能定位到 job_key 和配置")
        # ------------------------------------------------------------------

        imported = jcs.seed_from_files(CONFIG_DIR)
        check("三个岗位都被种子化", len(imported) == 3, str(imported))

        seeded = jcs.list_job_configs()
        check("列表里有三个岗位", len(seeded) == 3, str(len(seeded)))

        for row in seeded:
            name = row["job_name"]
            record = jcs.resolve_job_by_name(name)
            check(f"「{name}」能解析", record is not None)
            check(f"「{name}」带回了 job_key", bool(record.get("job_key")), str(record.get("job_key")))
            check(
                f"「{name}」带回了配置本体",
                isinstance(record.get("config"), dict) and bool(record["config"].get("score_rules")),
            )
            check(
                f"「{name}」的 job_key 与列表里一致",
                record["job_key"] == row["job_key"],
                f"{record['job_key']} vs {row['job_key']}",
            )

        # 名字前后有空格也应该认（表单下拉偶尔带空白）
        any_name = seeded[0]["job_name"]
        check("岗位名带空格也能解析", jcs.resolve_job_by_name(f"  {any_name}  ") is not None)

        # ------------------------------------------------------------------
        section("2. 没选岗位：BLANK")
        # ------------------------------------------------------------------

        check("空串是 BLANK", _resolve_reason("") == JobResolveError.BLANK)
        check("纯空格是 BLANK", _resolve_reason("   ") == JobResolveError.BLANK)
        check("None 也当没选", _resolve_reason(None) == JobResolveError.BLANK)

        # ------------------------------------------------------------------
        section("3. 从没配过的岗位：MISSING")
        # ------------------------------------------------------------------

        check("没配过的岗位是 MISSING", _resolve_reason("从没存在过的岗位") == JobResolveError.MISSING)

        try:
            jcs.resolve_job_by_name("从没存在过的岗位")
        except JobResolveError as error:
            check("MISSING 带着岗位名", error.job_name == "从没存在过的岗位", error.job_name)
            check("MISSING 的 reason 常量没变", error.reason == "missing", error.reason)

        # ------------------------------------------------------------------
        section("4. 被 HR 软删过的岗位：DELETED（跟「从没配过」分开说）")
        # ------------------------------------------------------------------

        victim = seeded[0]["job_name"]
        victim_key = seeded[0]["job_key"]

        check("软删返回 True", jcs.delete_job_config(victim_key) is True)
        check("软删后是 DELETED", _resolve_reason(victim) == JobResolveError.DELETED, _resolve_reason(victim))
        check("软删后活行查不到", jcs.get_job_config_by_name(victim) is None)
        check("软删后不在岗位列表里", victim not in [c["job_name"] for c in jcs.list_job_configs()])

        try:
            jcs.resolve_job_by_name(victim)
        except JobResolveError as error:
            check("DELETED 带着岗位名", error.job_name == victim, error.job_name)

        # ------------------------------------------------------------------
        section("5. 配置坏了：BROKEN")
        # ------------------------------------------------------------------

        _raw_insert("broken_empty", "配置坏掉的岗位", {})
        check("空配置是 BROKEN", _resolve_reason("配置坏掉的岗位") == JobResolveError.BROKEN)

        _raw_insert("broken_norules", "缺打分的岗位", {"job_profile": {"salary": "面议"}})
        check("缺 score_rules 是 BROKEN", _resolve_reason("缺打分的岗位") == JobResolveError.BROKEN)

        _raw_insert("broken_noprofile", "缺画像的岗位", {"score_rules": {"学历": [{"要求": "本科"}]}})
        check("缺 job_profile 是 BROKEN", _resolve_reason("缺画像的岗位") == JobResolveError.BROKEN)

        # ------------------------------------------------------------------
        section("6. 知识库之外的岗位照样能用")
        # ------------------------------------------------------------------

        # 岗位管理里新建的岗位：知识库里没有它（job_configs/*.json 里也没有），
        # 但配置表里有 —— 它就必须能测。这正是删掉知识库那道闸的理由。
        _raw_insert(
            "hr_new_job",
            "HR 手工新建的岗位",
            {
                "job_key": "hr_new_job",
                "job_name": "HR 手工新建的岗位",
                "job_profile": {"responsibilities": ["干活"], "work_time": "朝九晚六", "salary": "面议"},
                "score_rules": {"学历": [{"要求": "本科及以上"}]},
            },
        )
        record = jcs.resolve_job_by_name("HR 手工新建的岗位")
        check("手工新建的岗位能解析", record is not None)
        check("带回了它自己的 job_key", record and record["job_key"] == "hr_new_job", str(record))
        check("它不在 job_configs/*.json 里", not (CONFIG_DIR / "hr_new_job.json").exists())

        # ------------------------------------------------------------------
        section("7. main.py 里不再有知识库那道闸")
        # ------------------------------------------------------------------

        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        main_tree = ast.parse(main_source)

        imported_names = set()
        for node in ast.walk(main_tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.knowledge_base":
                imported_names.update(alias.name for alias in node.names)
        check("main.py 不再 import get_job_knowledge", "get_job_knowledge" not in imported_names, str(imported_names))
        check("main.py 里没有 get_job_knowledge 的调用", "get_job_knowledge(" not in main_source)
        check("main.py 用了 resolve_job_by_name", "resolve_job_by_name" in main_source)
        check("main.py 不再出现「知识库中的有效岗位」", "知识库中的有效岗位" not in main_source)
        check("main.py 不再出现「筛选规则尚未生成」", "筛选规则尚未生成" not in main_source)

        # 错误提示里不许再提知识库 —— 那是另一份名单。
        # 只看 HTTPException(detail=...) 里真正发给 HR 的那句，
        # 注释和 docstring 里提一嘴历史原因不算。
        detail_texts = []
        for node in ast.walk(main_tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HTTPException"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "detail":
                    continue
                for part in ast.walk(keyword.value):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        detail_texts.append(part.value)
        check("main.py 的报错提示里没有「知识库」", not [t for t in detail_texts if "知识库" in t], str(detail_texts))
        check("main.py 的报错提示里没有「有效岗位」这种含糊说法",
              not [t for t in detail_texts if "有效岗位" in t], str(detail_texts))

        # 端点里没有别的岗位判断残留（原来那段双闸现在只剩一个 resolver 调用）
        endpoint_calls = [
            node.func.id
            for node in ast.walk(main_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        check("端点不再直接调 get_job_config_by_name", "get_job_config_by_name" not in endpoint_calls)
        check("端点只留 resolve_job_by_name 一个岗位入口", "resolve_job_by_name" in endpoint_calls)

        # ------------------------------------------------------------------
        section("8. 报错映射：每种原因只给一种说法")
        # ------------------------------------------------------------------

        # 不 import 整个 main.py（会把 gradio 全拉起来、还会写真实库），
        # 只把报错映射这个函数从源码里抽出来单独跑。
        segment = None
        for node in main_tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_job_resolve_http_error":
                segment = ast.get_source_segment(main_source, node)
                break
        check("main.py 里有 _job_resolve_http_error", segment is not None)

        mapping = {}
        if segment:
            namespace = {"JobResolveError": JobResolveError, "HTTPException": HTTPException}
            exec(compile(ast.parse(segment), "<main._job_resolve_http_error>", "exec"), namespace)
            to_http_error = namespace["_job_resolve_http_error"]

            mapping["blank"] = to_http_error(JobResolveError(JobResolveError.BLANK))
            mapping["missing"] = to_http_error(JobResolveError(JobResolveError.MISSING, "初中学习顾问"))
            mapping["deleted"] = to_http_error(JobResolveError(JobResolveError.DELETED, "初中学习顾问"))
            mapping["broken"] = to_http_error(JobResolveError(JobResolveError.BROKEN, "初中学习顾问"))

        check("BLANK 是 400", mapping["blank"].status_code == 400, str(mapping["blank"].status_code))
        check("BLANK 提示先选岗位", "请先选择岗位" in mapping["blank"].detail, mapping["blank"].detail)

        check("MISSING 是 400", mapping["missing"].status_code == 400, str(mapping["missing"].status_code))
        check("MISSING 说是评分配置不存在", "评分配置不存在" in mapping["missing"].detail, mapping["missing"].detail)
        check("MISSING 带上岗位名", "初中学习顾问" in mapping["missing"].detail, mapping["missing"].detail)

        check("DELETED 是 400", mapping["deleted"].status_code == 400, str(mapping["deleted"].status_code))
        check("DELETED 说已被删除", "已被删除" in mapping["deleted"].detail, mapping["deleted"].detail)
        check("DELETED 提示去岗位管理重新新增", "岗位管理" in mapping["deleted"].detail, mapping["deleted"].detail)

        check("BROKEN 是 503", mapping["broken"].status_code == 503, str(mapping["broken"].status_code))
        check("BROKEN 说配置不可用", "配置不可用" in mapping["broken"].detail, mapping["broken"].detail)

        for key, error in mapping.items():
            check(f"{key} 的提示里没有「知识库」", "知识库" not in error.detail, error.detail)
            check(f"{key} 的提示里没有「有效岗位」这种含糊说法", "有效岗位" not in error.detail, error.detail)

        # ------------------------------------------------------------------
        section("9. main.py 还能正常编译")
        # ------------------------------------------------------------------

        try:
            py_compile.compile(str(ROOT / "main.py"), doraise=True)
            check("main.py 编译通过", True)
        except py_compile.PyCompileError as error:
            check("main.py 编译通过", False, str(error))

        try:
            py_compile.compile(str(ROOT / "app" / "job_config_store.py"), doraise=True)
            check("job_config_store.py 编译通过", True)
        except py_compile.PyCompileError as error:
            check("job_config_store.py 编译通过", False, str(error))
    finally:
        jcs.DB_PATH = original_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
