"""JD → 配置生成 + 岗位管理 API 的离线测试。

不发起真实模型调用：把 LLMClient.chat_json 换成一个返回固定 JSON 的假客户端。
本仓库不会自动发起带密钥的外呼请求（见 scripts/live_smoke.py）。

覆盖三段：
1. 生成的配置是否落在 Schema 允许的形状里（字段/操作符封闭词表）
2. 宽松校验的 errors / warnings 分流是否正确
3. 落库、复制、复核、软删这条链的语义
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.job_config_store as store
from app import job_generator
from app.job_generator import JobConfigGenerator
from app.job_schema import (
    FIELD_TYPES,
    is_valid_field,
    normalize_job_config,
    validate_job_config,
)

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


# ---------------------------------------------------------------------------
# 假 LLM 客户端
# ---------------------------------------------------------------------------

MODEL_OUTPUT: dict = {}


class FakeClient:
    async def chat_json(self, system_prompt: str, user_prompt: str, **kwargs) -> dict:
        # 记录收到的 prompt：字段表和操作符表必须真的渲染进去了
        FakeClient.last_system = system_prompt
        FakeClient.last_user = user_prompt
        if isinstance(MODEL_OUTPUT.get("_raise"), Exception):
            raise MODEL_OUTPUT["_raise"]
        return json.loads(json.dumps(MODEL_OUTPUT["config"], ensure_ascii=False))


def generator() -> JobConfigGenerator:
    gen = JobConfigGenerator()
    gen.client = FakeClient()
    return gen


def run(coro):
    return asyncio.run(coro)


def sample_model_output() -> dict:
    """一份「像样但有瑕疵」的模型输出，模拟真实情况。"""
    return {
        "job_key": "sales_ops_specialist",
        "job_name": "销售运营专员",
        "job_profile": {
            "industry": "互联网",
            "responsibilities": ["负责区域销售数据整理与分析", "协助制定销售目标并跟踪达成"],
            "work_time": ["周一至周五 9:00-18:00"],
            "rest_schedule": [],
            "salary": ["底薪 6000 元/月 + 提成"],
            "hard_gates": ["本科及以上"],
            "soft_traits": ["数据分析能力"],
        },
        "dialog_check_items": [
            "候选人是否接受以业绩指标为导向的工作方式",
            "候选人是否接受频繁的数据报表与复盘会议",
            "候选人是否接受月底冲刺的高强度节奏",
        ],
        "exp_keywords": ["销售运营", "数据分析", "Excel", "CRM", "业绩目标"],
        "score_rules": {
            "add_score": [
                {
                    "id": "related_exp",
                    "field": "exp_tags",
                    "op": "has_any",
                    "value": ["销售运营", "CRM"],
                    "score": 3,
                    "desc": "有销售运营或 CRM 使用经历",
                },
                {
                    # 故意：value 留空 —— 该规则永远不会命中，必须报 warning
                    "id": "empty_value_rule",
                    "field": "major",
                    "op": "contains_any",
                    "value": [],
                    "score": 2,
                    "desc": "商科相关专业",
                },
                {
                    # 故意：op 与字段类型不匹配（bool 字段用 contains_any）
                    "id": "bad_op",
                    "field": "school_type.is_985_211",
                    "op": "contains_any",
                    "value": ["是"],
                    "score": 1,
                    "desc": "学历背景",
                },
                {
                    # 故意：幻觉字段，不在封闭词表内
                    "id": "hallucinated_field",
                    "field": "age",
                    "op": "lt",
                    "value": 30,
                    "score": 1,
                    "desc": "年龄要求",
                },
            ],
            "minus_score": [],
        },
        "level_threshold": {"high_min": 5, "mid_min": 2},
        "alternate_role_mapping": [
            {
                "backup_job_key": "customer_success",
                "backup_job_name": "客户成功专员",
                "match_condition": ["有客户维护经验", "沟通能力强"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. 生成结果的形状
# ---------------------------------------------------------------------------

def test_generated_shape() -> None:
    section("1. 生成结果的形状")
    MODEL_OUTPUT.clear()
    MODEL_OUTPUT["config"] = sample_model_output()
    result = run(generator().generate("招聘销售运营专员 1 名……"))

    config = result["config"]
    check("返回 config/errors/warnings 三段", set(result) == {"config", "errors", "warnings"})
    check("job_key 被采纳", config["job_key"] == "sales_ops_specialist", config["job_key"])
    check("review_status 强制为 pending（定稿决策 2）", config["review_status"] == "pending")
    check("source 标记为 auto_llm", config["source"] == "auto_llm")
    check("rag_doc_id 默认等于 job_key（一岗一库）", config["rag_doc_id"] == config["job_key"])

    # 字段表与操作符表必须真的进 prompt，否则模型面对的是没有词表的幻觉空间
    check("prompt 注入了字段表", "exp_tags" in FakeClient.last_system)
    check("prompt 注入了操作符表", "contains_any" in FakeClient.last_system)
    check("prompt 无未渲染占位符", "{{" not in FakeClient.last_system)
    check("JD 原文进了 user prompt", "销售运营专员" in FakeClient.last_user)

    # 幻觉字段被校验器拦下
    fields = [r["field"] for r in config["score_rules"]["add_score"]]
    check("幻觉字段仍留在配置里（交给 HR 决定）", "age" in fields)
    check("age 不在封闭词表内", not is_valid_field("age"))

    # dialog_check_items 被规范化成统一形状
    items = config["dialog_check_items"]
    check("check_items 规范化为对象数组", all(isinstance(i, dict) and i.get("text") for i in items))

    # save 时 strict 校验必须拒绝这份配置 —— 证明预览的 errors 不是摆设
    strict_errors = validate_job_config(config)
    check("strict 校验拦下这份配置", bool(strict_errors), "预览给了 errors 却能直接入库")


# ---------------------------------------------------------------------------
# 2. errors / warnings 分流
# ---------------------------------------------------------------------------

def test_error_warning_split() -> None:
    section("2. errors 与 warnings 的分流")
    MODEL_OUTPUT["config"] = sample_model_output()
    result = run(generator().generate("招聘销售运营专员 1 名……"))
    errors, warnings = result["errors"], result["warnings"]
    joined = " ".join(errors + warnings)

    print("  errors:")
    for e in errors:
        print(f"    - {e}")
    print("  warnings:")
    for w in warnings:
        print(f"    - {w}")

    check("幻觉字段进 errors", any("age" in e for e in errors), joined)
    check("op 类型错配进 errors", any("contains_any 不适用于" in e for e in errors), joined)
    check("空 value 进 warnings（不阻断保存）", any("永远不会命中" in w for w in warnings), joined)
    check("必带人工复核提示", any("人工复核" in w for w in warnings), joined)
    check("warnings 不含 errors 内容", not set(warnings) & set(errors))

    # JD 信息不全（如没有薪资）应为警告而非错误 —— 否则 JD 生成永远失败
    MODEL_OUTPUT["config"] = sample_model_output()
    MODEL_OUTPUT["config"]["job_profile"].pop("salary")
    partial = run(generator().generate("招聘销售运营专员"))
    check(
        "job_profile 缺 salary → warning 而非 error",
        any("job_profile" in w and w.startswith("[警告]") for w in partial["warnings"])
        and not any("job_profile" in e for e in partial["errors"]),
        str(partial),
    )


def test_threshold_warning() -> None:
    section("3. 阈值与满分的关系")
    MODEL_OUTPUT["config"] = sample_model_output()
    MODEL_OUTPUT["config"]["score_rules"]["add_score"] = [
        {"id": "only", "field": "degree", "op": "in", "value": ["本科"], "score": 3, "desc": "学历"}
    ]
    MODEL_OUTPUT["config"]["level_threshold"] = {"high_min": 5, "mid_min": 2}
    result = run(generator().generate("JD"))
    check(
        "满分够不到 high_min → warning",
        any("无法达到" in w for w in result["warnings"]),
        str(result["warnings"]),
    )

    # 空 exp_keywords 的岗位：模型没给关键词，词表为空则 exp_tags 永远命中不了
    MODEL_OUTPUT["config"] = sample_model_output()
    MODEL_OUTPUT["config"]["exp_keywords"] = []
    empty_kw = run(generator().generate("JD"))
    check(
        "exp_keywords 为空 → warning",
        any("exp_keywords" in w for w in empty_kw["warnings"]),
        str(empty_kw["warnings"]),
    )


def test_model_failure() -> None:
    section("4. 模型异常与空输入")
    try:
        run(generator().generate("   "))
        check("空 JD 被拒", False)
    except job_generator.JobGenerationError as exc:
        check("空 JD 被拒", "JD 内容为空" in str(exc), str(exc))

    MODEL_OUTPUT["_raise"] = RuntimeError("503 upstream")
    try:
        run(generator().generate("招聘销售运营专员"))
        check("模型失败抛出 JobGenerationError", False)
    except job_generator.JobGenerationError as exc:
        check("模型失败抛出 JobGenerationError", "模型未能生成配置" in str(exc), str(exc))
    MODEL_OUTPUT.pop("_raise", None)


def test_job_key_fallback() -> None:
    section("5. job_key 回退")
    # 中文岗位名无法直接生成合法 key，必须回退而不是生成非法 key
    MODEL_OUTPUT["config"] = sample_model_output()
    MODEL_OUTPUT["config"]["job_name"] = "销售运营专员"
    MODEL_OUTPUT["config"].pop("job_key")
    result = run(generator().generate("岗位名称：销售运营专员\n负责数据分析"))
    key = result["config"]["job_key"]
    check("中文岗位名回退到 jd_<时间戳>", key.startswith("jd_"), key)
    check("回退的 key 合法", is_valid_field is not None and validate_job_config(
        {**result["config"], "job_key": "bad key!"}) != [], "非法 key 应被拦下")

    # 模型自己给了合法英文 key 就直接用
    MODEL_OUTPUT["config"]["job_key"] = "sales_ops"
    ok = run(generator().generate("JD"))
    check("模型给了合法 key 就直接采纳", ok["config"]["job_key"] == "sales_ops")


# ---------------------------------------------------------------------------
# 6. 落库 / 复制 / 复核 / 软删
# ---------------------------------------------------------------------------

def test_store_lifecycle() -> None:
    section("6. 落库、复制、复核、软删")
    original_db = store.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        store.DB_PATH = str(Path(tmp) / "test.db")
        try:
            MODEL_OUTPUT["config"] = sample_model_output()
            # 用干净配置入库：幻觉字段那版本来就该被拒
            MODEL_OUTPUT["config"]["score_rules"]["add_score"] = [
                MODEL_OUTPUT["config"]["score_rules"]["add_score"][0]
            ]
            MODEL_OUTPUT["config"]["job_key"] = "lifecycle_demo"
            config = run(generator().generate("JD"))["config"]

            record = store.save_job_config(config, source_type="auto_llm", raw_jd="JD 原文")
            check("入库成功", record["job_key"] == "lifecycle_demo")
            check("入库后仍是 pending", record["review_status"] == "pending")
            check(
                "config_json 内的 review_status 与列一致",
                record["config"]["review_status"] == record["review_status"],
                f'{record["config"]["review_status"]} vs {record["review_status"]}',
            )
            check("raw_jd 一并保存", record["raw_jd"] == "JD 原文")

            reviewed = store.update_job_config_meta("lifecycle_demo", review_status="reviewed")
            check("标记复核成功", reviewed["review_status"] == "reviewed")
            check(
                "复核后 config_json 同步更新",
                reviewed["config"]["review_status"] == "reviewed",
                reviewed["config"]["review_status"],
            )

            # 软删：默认软删，历史测评记录仍能按 job_key 关联
            check("软删返回 True", store.delete_job_config("lifecycle_demo"))
            check("软删后默认查不到", store.get_job_config("lifecycle_demo") is None)
            check("include_deleted 可查到", store.get_job_config("lifecycle_demo", include_deleted=True) is not None)
            check("软删后默认列表不含它", "lifecycle_demo" not in [c["job_key"] for c in store.list_job_configs()])
            check("软删后 include_deleted 列表含它",
                  "lifecycle_demo" in [c["job_key"] for c in store.list_job_configs(include_deleted=True)])

            # 软删后同 key 可重建（唯一索引带 deleted_at 才做得到）
            store.save_job_config(config, source_type="auto_llm")
            check("软删后同 key 可重建", store.get_job_config("lifecycle_demo") is not None)
            check("重建后回到 pending", store.get_job_config("lifecycle_demo")["review_status"] == "pending")

            # 非法配置拒绝入库
            try:
                bad = dict(config)
                bad["job_key"] = "Bad Key!"
                store.save_job_config(bad)
                check("非法 job_key 被拒", False)
            except store.JobConfigError:
                check("非法 job_key 被拒", True)

            # seed_from_files 幂等 + overwrite
            with tempfile.TemporaryDirectory() as cfg_dir:
                (Path(cfg_dir) / "a.json").write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                store.DB_PATH = str(Path(tmp) / "seed.db")
                first = store.seed_from_files(cfg_dir)
                check("首次 seed 导入", first == ["lifecycle_demo"], str(first))
                store.update_job_config_meta("lifecycle_demo", review_status="reviewed")
                check("默认幂等：不覆盖已存在", store.seed_from_files(cfg_dir) == [])
                check(
                    "overwrite=True 会覆盖并重置为 reviewed",
                    store.seed_from_files(cfg_dir, overwrite=True) == ["lifecycle_demo"]
                    and store.get_job_config("lifecycle_demo")["review_status"] == "reviewed",
                )
        finally:
            store.DB_PATH = original_db


def main() -> int:
    print("=" * 66)
    print("JD → 岗位配置生成 & 岗位管理")
    print("=" * 66)
    for test in (
        test_generated_shape,
        test_error_warning_split,
        test_threshold_warning,
        test_model_failure,
        test_job_key_fallback,
        test_store_lifecycle,
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