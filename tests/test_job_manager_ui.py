"""HR 后台【岗位管理】子页面的离线测试。

覆盖 app/gradio_ui.py 里的岗位管理 handler：
生成 / 保存（新增与编辑）/ 删除 / 列表渲染 / 弹窗分发，
以及支撑「历史报告不随配置改动」的岗位配置快照。

不发起真实模型调用：把 gradio_ui.JobConfigGenerator 换成固定输出的假类。
不碰真实数据库：把 job_config_store / session_store 的 DB_PATH 都猴补丁到临时文件。

Gradio 组件接线不在这里测 —— 组件由 build_*_ui() 构建时校验。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gradio as gr

import app.gradio_ui as ui
import app.job_config_store as store
import app.session_store as ss
from app.assessment_service import AssessmentService

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def run(coro):
    return asyncio.run(coro)


def valid_config(job_key: str = "ui_demo") -> dict:
    """一份能通过 strict 校验的配置。改动它就能造出各种非法变体。"""
    return {
        "job_key": job_key,
        "job_name": "教育销售顾问",
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


class FakeGenerator:
    """顶掉真的 LLM 调用。generate() 返回固定配置。

    故意**不理会**传进来的 job_key —— 模型就是这样不听话的，
    UI 侧必须自己把 key 钉死，不能指望参数传下去就一定生效。
    """

    last_jd = ""
    last_job_key = ""
    payload: dict = {}

    async def generate(self, jd_text: str, *, job_key: str | None = None) -> dict:
        FakeGenerator.last_jd = jd_text
        FakeGenerator.last_job_key = job_key or ""
        return json.loads(json.dumps(FakeGenerator.payload, ensure_ascii=False))


class TempDB:
    """把两个 store 的 DB_PATH 指到临时文件，退出时还原。

    gradio_ui 是 `from ... import save_job_config` 这样拿函数的，
    但这些函数内部走各自模块级的 DB_PATH，所以只改模块变量就够，
    不用逐个替换 ui 里的引用。
    """

    def __init__(self, tmp: str, name: str = "jobmanager.db"):
        self.path = str(Path(tmp) / name)
        self.original = [store.DB_PATH, ss.DB_PATH]

    def __enter__(self) -> "TempDB":
        store.DB_PATH = ss.DB_PATH = self.path
        store.init_db()
        ss.init_db()
        return self

    def __exit__(self, *exc) -> None:
        store.DB_PATH, ss.DB_PATH = self.original


def _visible(update) -> bool:
    """从 gr.update(...) 里取出 visible。"""
    return bool(update.get("visible")) if isinstance(update, dict) else False


# ---------------------------------------------------------------------------
# 1. 上传 JD → 生成配置
# ---------------------------------------------------------------------------

def test_generate() -> None:
    section("1. 上传 JD 生成配置")
    with tempfile.TemporaryDirectory() as tmp:
        jd_path = Path(tmp) / "jd.txt"
        jd_text = "岗位名称：教育销售顾问\n负责课程销售转化。"
        jd_path.write_text(jd_text, encoding="utf-8")

        original_generator = ui.JobConfigGenerator
        FakeGenerator.payload = {"config": valid_config("generated_demo"), "errors": [], "warnings": []}
        ui.JobConfigGenerator = FakeGenerator
        try:
            with TempDB(tmp):
                config_json, status_html, raw_jd, clear_file = run(ui.generate_job_config(str(jd_path)))

                parsed = json.loads(config_json)
                check("生成结果是合法 JSON", isinstance(parsed, dict))
                check("带回了 job_key", parsed["job_key"] == "generated_demo", str(parsed.get("job_key")))
                check("JD 原文回传给保存步骤用", raw_jd == jd_text, raw_jd[:40])
                check("模型收到的是文件里的 JD", FakeGenerator.last_jd == jd_text, FakeGenerator.last_jd[:40])
                check("生成后自动做了 schema 校验", "校验通过" in status_html, status_html[:160])
                check("返回了清空文件框的更新指令", isinstance(clear_file, dict), str(type(clear_file)))

                # 生成只预览，绝不落库
                keys = [c["job_key"] for c in store.list_job_configs()]
                check("生成不落库", "generated_demo" not in keys, str(keys))
        finally:
            ui.JobConfigGenerator = original_generator


def test_generate_auto_validation() -> None:
    section("2. 生成后自动校验（不通过要当场报出来）")
    with tempfile.TemporaryDirectory() as tmp:
        jd_path = Path(tmp) / "jd.txt"
        jd_path.write_text("随便一份 JD", encoding="utf-8")

        original_generator = ui.JobConfigGenerator
        broken = valid_config("broken_demo")
        del broken["job_name"]
        FakeGenerator.payload = {"config": broken, "errors": [], "warnings": []}
        ui.JobConfigGenerator = FakeGenerator
        try:
            with TempDB(tmp):
                _, status_html, _, _ = run(ui.generate_job_config(str(jd_path)))
                check("缺必填字段当场报不通过", "校验不通过" in status_html and "job_name" in status_html, status_html[:200])
                check("告诉 HR 会被拦截", "保存将被拦截" in status_html, status_html[:200])
                check("告诉 HR 去哪改", "高级" in status_html, status_html[:200])

                # 软性坑只提示不拦：词表为空但规则用到了 exp_tags
                soft = valid_config("soft_demo")
                soft["exp_keywords"] = []
                FakeGenerator.payload = {"config": soft, "errors": [], "warnings": ["规则用到了空词表"]}
                _, status_html, _, _ = run(ui.generate_job_config(str(jd_path)))
                check("软性坑只提示不拦", "校验通过" in status_html and "提示" in status_html, status_html[:200])
        finally:
            ui.JobConfigGenerator = original_generator


def test_generate_pins_job_key() -> None:
    section("3. 编辑时锁死 job_key")
    with tempfile.TemporaryDirectory() as tmp:
        jd_path = Path(tmp) / "jd2.txt"
        jd_path.write_text("新版 JD", encoding="utf-8")

        original_generator = ui.JobConfigGenerator
        # 模型不听话，自己猜了个别的 key
        FakeGenerator.payload = {"config": valid_config("model_guessed_key"), "errors": [], "warnings": []}
        ui.JobConfigGenerator = FakeGenerator
        try:
            with TempDB(tmp):
                store.save_job_config(valid_config("real_key"), source_type="manual")
                config_json, _, _, _ = run(ui.regenerate_job_config(str(jd_path), "real_key"))
                parsed = json.loads(config_json)
                check("key 被钉回原值", parsed["job_key"] == "real_key", parsed["job_key"])
                check("原 key 也传给了生成器", FakeGenerator.last_job_key == "real_key", FakeGenerator.last_job_key)
                check("job_name 允许跟着新 JD 变", parsed["job_name"] == "教育销售顾问", parsed["job_name"])
                check("重新生成不落库", [c["job_key"] for c in store.list_job_configs()] == ["real_key"])
        finally:
            ui.JobConfigGenerator = original_generator


def test_generate_rejects_bad_input() -> None:
    section("4. 生成的输入校验")
    with tempfile.TemporaryDirectory() as tmp:
        original_generator = ui.JobConfigGenerator
        ui.JobConfigGenerator = FakeGenerator
        try:
            try:
                run(ui.generate_job_config(None))
                check("未上传文件时报错", False)
            except gr.Error:
                check("未上传文件时报错", True)

            bad_path = Path(tmp) / "jd.md"
            bad_path.write_text("岗位名称：测试", encoding="utf-8")
            try:
                run(ui.generate_job_config(str(bad_path)))
                check("不支持的格式被拒", False)
            except gr.Error:
                check("不支持的格式被拒", True)

            empty_path = Path(tmp) / "empty.txt"
            empty_path.write_text("   \n  ", encoding="utf-8")
            try:
                run(ui.generate_job_config(str(empty_path)))
                check("空文件被拒", False)
            except gr.Error:
                check("空文件被拒", True)

            try:
                run(ui.regenerate_job_config(str(empty_path), ""))
                check("没在编辑态时重新生成报错", False)
            except gr.Error:
                check("没在编辑态时重新生成报错", True)
        finally:
            ui.JobConfigGenerator = original_generator


# ---------------------------------------------------------------------------
# 5. 新增保存
# ---------------------------------------------------------------------------

def test_save_new_job() -> None:
    section("5. 新增岗位保存")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            config_json = json.dumps(valid_config("save_demo"), ensure_ascii=False)

            status, modal, table, keys = ui.save_new_job(config_json, "这是 JD 原文")
            check("首次保存提示新增", "已新增岗位" in status and "save_demo" in status, status[:200])
            check("保存成功才关弹窗", not _visible(modal), str(modal))
            record = store.get_job_config("save_demo")
            check("岗位已入库", record is not None)
            check("来源记为 AI 生成", record["source_type"] == "auto_llm", record["source_type"])
            check("JD 原文一并存下", record["raw_jd"] == "这是 JD 原文", str(record["raw_jd"]))
            check("返回的表格里能看到它", any(row[0] == "教育销售顾问" for row in table))
            check("行号→key 映射同步更新", keys == ["save_demo"], str(keys))

            # 同 key 再存一次 = 覆盖，提示语换成「已更新」
            renamed = valid_config("save_demo")
            renamed["job_name"] = "教育销售顾问（改）"
            status, _, _, _ = ui.save_new_job(json.dumps(renamed, ensure_ascii=False), None)
            check("重复保存提示更新", "已更新岗位" in status, status[:200])
            check("配置被覆盖", store.get_job_config("save_demo")["job_name"] == "教育销售顾问（改）")

            # 非法配置不落库，弹窗不许关
            bad = valid_config("bad_demo")
            bad["score_rules"]["add_score"][0]["field"] = "salary"  # 不在封闭词表里
            status, modal, _, _ = ui.save_new_job(json.dumps(bad, ensure_ascii=False), None)
            check("非法配置提示保存失败", "保存失败" in status, status[:200])
            check("非法配置的错误逐条列出", status.count("<li>") >= 1, status[:200])
            check("非法配置没有落库", store.get_job_config("bad_demo") is None)
            check("非法配置弹窗保持打开", _visible(modal), str(modal))

            # 坏 JSON 同样不落库
            status, modal, _, _ = ui.save_new_job("{not json", None)
            check("坏 JSON 提示失败", "不是合法 JSON" in status, status[:200])
            check("坏 JSON 弹窗保持打开", _visible(modal), str(modal))


# ---------------------------------------------------------------------------
# 6. 编辑保存
# ---------------------------------------------------------------------------

def test_save_edited_job() -> None:
    section("6. 编辑岗位保存（覆盖）")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            store.save_job_config(valid_config("edit_demo"), source_type="manual")

            # 正常编辑：改分值和岗位名
            edited = valid_config("edit_demo")
            edited["job_name"] = "教育销售顾问（改）"
            edited["score_rules"]["add_score"][0]["score"] = 5
            status, modal, _, keys = ui.save_edited_job(
                json.dumps(edited, ensure_ascii=False), "新版 JD 原文", "edit_demo"
            )
            check("编辑保存提示更新", "已更新岗位" in status, status[:200])
            check("编辑保存成功才关弹窗", not _visible(modal), str(modal))
            saved = store.get_job_config("edit_demo")
            check("分值已覆盖", saved["config"]["score_rules"]["add_score"][0]["score"] == 5)
            check("岗位名跟着新 JD 变", saved["job_name"] == "教育销售顾问（改）")
            check("JD 原文更新为新版", saved["raw_jd"] == "新版 JD 原文", str(saved["raw_jd"]))
            check("没有多出第二个岗位", keys == ["edit_demo"], str(keys))

            # 关键：JSON 编辑器里 key 被手滑改了，也不能新建岗位
            tampered = valid_config("model_guessed_key")
            status, _, _, keys = ui.save_edited_job(
                json.dumps(tampered, ensure_ascii=False), None, "edit_demo"
            )
            check("编辑时 key 被钉死", store.get_job_config("edit_demo") is not None)
            check("模型猜的 key 没有落库", store.get_job_config("model_guessed_key") is None)
            check("列表仍然只有一个岗位", keys == ["edit_demo"], str(keys))

            # 坏 JSON 保持弹窗打开
            _, modal, _, _ = ui.save_edited_job("{oops", None, "edit_demo")
            check("坏 JSON 时弹窗保持打开", _visible(modal), str(modal))


# ---------------------------------------------------------------------------
# 7. 删除
# ---------------------------------------------------------------------------

def test_delete() -> None:
    section("7. 删除岗位")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            store.save_job_config(valid_config("del_demo"), source_type="auto_llm")

            status, modal, table, keys = ui.delete_job_from_ui("del_demo")
            check("删除成功提示", "已删除岗位" in status, status[:200])
            check("删除成功才关弹窗", not _visible(modal), str(modal))
            check("默认列表里消失了", "del_demo" not in [c["job_key"] for c in store.list_job_configs()])
            check("软删：include_deleted 仍可查",
                  store.get_job_config("del_demo", include_deleted=True) is not None)
            check("返回的表格里没有了", not any(row[0] == "教育销售顾问" for row in table))
            check("行号→key 映射同步清空", keys == [], str(keys))

            for label, arg in (("删除不存在的岗位", "not_exist"), ("没有待删除岗位", None)):
                try:
                    ui.delete_job_from_ui(arg)
                    check(f"{label}时报错", False)
                except gr.Error:
                    check(f"{label}时报错", True)


# ---------------------------------------------------------------------------
# 8. 列表渲染
# ---------------------------------------------------------------------------

def test_job_table() -> None:
    section("8. 岗位列表渲染")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            store.save_job_config(valid_config("t_ai"), source_type="auto_llm")
            manual = valid_config("t_manual")
            manual["job_name"] = "课程顾问"
            store.save_job_config(manual, source_type="manual")
            store.save_job_config(valid_config("t_gone"), source_type="manual")
            store.delete_job_config("t_gone")

            rows, keys = ui._job_table()
            check("行数与 key 数一致", len(rows) == len(keys), f"{len(rows)} vs {len(keys)}")
            check("表头没有 job_key", "job_key" not in ["岗位名称", "来源", "创建时间", "编辑", "删除"])
            check("job_key 不出现在任何一格里", all(key not in str(row) for key in keys for row in rows))
            check("AI 来源显示为中文", rows[keys.index("t_ai")][1] == "AI 生成", rows[keys.index("t_ai")][1])
            check("手动来源显示为中文", rows[keys.index("t_manual")][1] == "手动", rows[keys.index("t_manual")][1])
            check("岗位名取自 job_name", rows[keys.index("t_manual")][0] == "课程顾问")
            check("编辑列有可点内容", rows[0][3] == "编辑")
            check("删除列有可点内容", rows[0][4] == "删除")
            check("软删的岗位不在列表里", "t_gone" not in keys, str(keys))


def test_select_job_action() -> None:
    section("9. 点列表分发表")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            first = valid_config("row_a")
            first["job_name"] = "岗位甲"
            second = valid_config("row_b")
            second["job_name"] = "岗位乙"
            store.save_job_config(first, source_type="auto_llm", raw_jd="甲方原始 JD 内容")
            store.save_job_config(second, source_type="manual")

            rows, keys = ui._job_table()
            order = [r[0] for r in rows]
            check("表格顺序可预期", order == ["岗位甲", "岗位乙"], str(order))

            def select(row, col):
                ev = gr.SelectData(target=None, data={"index": [row, col], "value": "x"})
                return ui.select_job_action(ev, keys)

            # 点【编辑】列
            job_key, raw_jd, edit_vis, info, editor, status, _, del_vis, _ = select(0, ui.COL_EDIT)
            check("点编辑 → 返回该行 job_key", job_key == keys[0], f"{job_key} vs {keys}")
            check("点编辑 → 编辑弹窗打开", _visible(edit_vis))
            check("点编辑 → 删除弹窗不开", not _visible(del_vis))
            check("点编辑 → 带出原始 JD", raw_jd == "甲方原始 JD 内容", raw_jd)
            check("点编辑 → 弹窗里展示原始 JD", "甲方原始 JD 内容" in info, info[:200])
            check("点编辑 → 说清历史分数不变", "历史测评报告的分数不会变" in info, info[:300])
            check("点编辑 → 编辑器预填现有配置", json.loads(editor)["job_key"] == keys[0])
            check("点编辑 → 状态区清空", status == "")

            # 点【删除】列
            job_key, _, edit_vis, _, _, _, _, del_vis, del_info = select(1, ui.COL_DELETE)
            check("点删除 → 返回该行 job_key", job_key == keys[1], f"{job_key} vs {keys}")
            check("点删除 → 删除弹窗打开", _visible(del_vis))
            check("点删除 → 编辑弹窗不开", not _visible(edit_vis))
            check("点删除 → 二次确认说清不可用于改 JD", "不是修改 JD 的方式" in del_info.replace("——", "-"), del_info[:300])

            # 点非操作列 / 行号越界：什么都不弹
            for label, args in (("点姓名列", (0, ui.COL_NAME)), ("点来源列", (1, ui.COL_SOURCE)),
                                ("点创建时间列", (0, ui.COL_CREATED)), ("行号越界", (99, ui.COL_EDIT))):
                job_key, _, edit_vis, _, _, _, _, del_vis, _ = select(*args)
                check(f"{label}不弹窗", not _visible(edit_vis) and not _visible(del_vis) and job_key == "")


def test_edit_info_without_raw_jd() -> None:
    section("10. 无原始 JD 的老岗位")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            store.save_job_config(valid_config("legacy"), source_type="manual")
            record = store.get_job_config("legacy")
            check("确认前提：老岗位确实没存 raw_jd", not record["raw_jd"], str(record["raw_jd"]))

            ev = gr.SelectData(target=None, data={"index": [0, ui.COL_EDIT], "value": "编辑"})
            _, _, _, info, _, _, _, _, _ = ui.select_job_action(ev, ["legacy"])
            check("明说没有原始 JD", "没有留存原始 JD" in info, info[:200])
            check("并给出下一步指引", "请直接上传新版 JD" in info, info[:300])


# ---------------------------------------------------------------------------
# 11. 配置快照：历史报告不跟着改
# ---------------------------------------------------------------------------

class FakeAssessmentAgent:
    """顶掉 AssessmentAgent。finish() 只用到这两个方法。"""

    async def evaluate(self, job_config, resume_tags, chat_history):
        return {
            "willing_for_target_job": True,
            "conversation_traits": [],
            "check_results": [],
            "traits": {},
            "hr_summary": "",
        }

    async def match_alternate_role_mapping(self, job_config, resume_tags, traits):
        return []


class FakeCandidateMsgAgent:
    async def generate(self, job_name, check_results, willing):
        return "感谢您参加本次测评。"


def test_config_snapshot() -> None:
    section("11. 岗位配置快照")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            original = valid_config("snap_demo")
            original["exp_keywords"] = ["课程销售"]
            store.save_job_config(original, source_type="manual")

            tags = {"degree": "本科", "exp_tags": ["课程销售"]}
            session_id = ss.create_session("张三", "教育销售顾问", tags, "摘要", job_key="snap_demo")

            service = AssessmentService()
            service.assessment = FakeAssessmentAgent()
            service.candidate_msg = FakeCandidateMsgAgent()
            run(service.finish(session_id))

            session = ss.get_session(session_id)
            check("finish() 把配置快照存下来了",
                  isinstance(session.get("job_config_snapshot"), dict)
                  and session["job_config_snapshot"].get("job_key") == "snap_demo",
                  str(session.get("job_config_snapshot"))[:80])
            check("快照里的分值与当时一致",
                  session["job_config_snapshot"]["score_rules"]["add_score"][0]["score"] == 3)

            def objective_of(sid):
                report = service.build_report(sid)
                return report["objective_layer"]["total_score"], report["objective_layer"]["match_level"]

            before = objective_of(session_id)
            check("报告能正常打开", before[0] == 3, str(before))

            # HR 改配置：阈值全改、分值翻倍
            changed = valid_config("snap_demo")
            changed["score_rules"]["add_score"][0]["score"] = 9
            changed["level_threshold"] = {"high_min": 20, "mid_min": 15}
            changed["dialog_check_items"] = ["全新的考察点"]
            store.save_job_config(changed, source_type="manual")

            after = objective_of(session_id)
            check("改完配置后历史报告分数不变", before == after, f"{before} -> {after}")
            check("新规则确实生效在库里",
                  store.get_job_config("snap_demo")["config"]["score_rules"]["add_score"][0]["score"] == 9)

            # 岗位下线后历史报告仍要能打开
            store.delete_job_config("snap_demo")
            after_delete = objective_of(session_id)
            check("岗位软删后历史报告仍可打开", after_delete == before, f"{after_delete}")


def test_snapshot_fallback_for_legacy_sessions() -> None:
    section("12. 加快照之前的老会话")
    with tempfile.TemporaryDirectory() as tmp:
        with TempDB(tmp):
            store.save_job_config(valid_config("legacy_demo"), source_type="manual")
            # exp_tags 要真的命中规则，否则实时重算出来是 0 分，断言不出差别
            tags = {"degree": "本科", "exp_tags": ["课程销售"]}
            old_session = ss.create_session("李四", "教育销售顾问", tags, "摘要", job_key="legacy_demo")
            # 不传快照 —— 模拟这次改动之前入库的历史会话
            ss.save_assessment_result(
                old_session,
                objective={"total_score": 3, "match_level": "低匹配", "score_breakdown": []},
                subjective={}, final_decision_text="推进", hr_summary="", candidate_msg="",
            )
            check("老会话确实没有快照", not ss.get_session(old_session).get("job_config_snapshot"))

            report = AssessmentService().build_report(old_session)
            check("老会话退回实时配置重算，不报错",
                  report["objective_layer"]["total_score"] == 3,
                  str(report["objective_layer"]))


def main() -> int:
    print("=" * 66)
    print("HR 后台 · 岗位管理子页面")
    print("=" * 66)
    for test in (
        test_generate,
        test_generate_auto_validation,
        test_generate_pins_job_key,
        test_generate_rejects_bad_input,
        test_save_new_job,
        test_save_edited_job,
        test_delete,
        test_job_table,
        test_select_job_action,
        test_edit_info_without_raw_jd,
        test_config_snapshot,
        test_snapshot_fallback_for_legacy_sessions,
    ):
        test()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：")
        for name in FAIL:
            print(f"  - {name}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())