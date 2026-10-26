"""打分规则（score_rules）形状归一化的回归测试。

背景：JD → 配置这条主流程里，模型实测把 score_rules 吐成一个**扁平数组**，
而合并逻辑只认 {"add_score": [...], "minus_score": [...]}。形状对不上时
模型写的规则被整份丢掉，只剩一组 value 为空、永远不会命中的占位规则 ——
岗位照样存进库，但打分会永远给 0。

另一类漂移是 op 与 value 的形状不匹配：引擎里 equals / not_equals 把 value
当**单个**字面量比，模型写成一个数组（想表达「在这一组里 / 不在这一组里」）
时条件对所有候选人恒真，等于给每个人扣一次分。

这里锁住三件事：
1. 扁平数组被收敛成规范形状，模型的规则一条不丢。
2. op 与 value 形状不匹配时收敛成集合语义，且求值结果符合意图。
3. 收敛后的配置能通过 strict 校验（= 能真的存进库）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.job_schema import (
    evaluate_rule,
    normalize_job_config,
    normalize_score_rules,
    validate_job_config,
)

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(
        f"  {'OK  ' if condition else 'FAIL'} {name}"
        + (f"  <- {detail}" if not condition and detail else "")
    )


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def _rule(field: str, op: str, value, score: int = 5, desc: str = "说明", rid: str = "r1") -> dict:
    return {"id": rid, "field": field, "op": op, "value": value, "score": score, "desc": desc}


def test_flat_list_is_kept() -> None:
    section("1. 扁平数组（实测最常见的漂移）")
    rules = [
        _rule("exp_tags", "has_any", ["授课", "代课"], 5),
        _rule("major", "contains_any", ["数学"], 3),
    ]
    out = normalize_score_rules(rules)
    check("收敛成 add_score 分组", "add_score" in out, str(out))
    check("规则一条不丢", len(out.get("add_score") or []) == 2, str(out))
    check(
        "value 原样保留（不再是空占位）",
        (out["add_score"][0].get("value") or []) == ["授课", "代课"],
        str(out),
    )
    check(
        "全部规则都有可命中的 value",
        all(r.get("value") for r in out.get("add_score") or []),
        str(out),
    )


def test_op_value_shape_converges() -> None:
    section("2. op 与 value 形状不匹配")
    rules = {
        "add_score": [],
        "minus_score": [
            _rule("degree", "not_equals", ["本科", "硕士研究生"], 8),
        ],
    }
    out = normalize_score_rules(rules)
    fixed = out["minus_score"][0]
    check("not_equals + 数组 → not_in", fixed.get("op") == "not_in", str(fixed))
    check("value 保持原样", fixed.get("value") == ["本科", "硕士研究生"], str(fixed))

    # 求值语义：本科学历不该被扣分，大专学历应该被扣分
    check(
        "本科候选人不再被扣分",
        not evaluate_rule(fixed, {"degree": "本科"}),
        "not_in 语义反了",
    )
    check(
        "大专候选人被扣分",
        evaluate_rule(fixed, {"degree": "大专"}),
        "not_in 语义反了",
    )

    eq = normalize_score_rules(
        {"add_score": [_rule("degree", "equals", ["本科", "硕士研究生"], 2)]}
    )
    check(
        "equals + 数组 → in",
        eq["add_score"][0].get("op") == "in",
        str(eq),
    )
    check(
        "本科学历命中 in",
        evaluate_rule(eq["add_score"][0], {"degree": "本科"}),
        "in 语义反了",
    )
    check(
        "大专学历不命中 in",
        not evaluate_rule(eq["add_score"][0], {"degree": "大专"}),
        "in 语义反了",
    )


def test_single_value_ops_untouched() -> None:
    section("3. 单值操作符不受影响")
    out = normalize_score_rules(
        {"add_score": [_rule("degree", "not_equals", "本科", 1)]}
    )
    check("not_equals + 单值保持原 op", out["add_score"][0].get("op") == "not_equals", str(out))
    out2 = normalize_score_rules({"add_score": [_rule("degree", "equals", "本科", 1)]})
    check("equals + 单值保持原 op", out2["add_score"][0].get("op") == "equals", str(out2))


def test_normalize_job_config_wiring() -> None:
    section("4. normalize_job_config 接线")
    config = {
        "job_key": "junior_high_math_teacher",
        "job_name": "初中数学教师",
        "job_profile": {
            "responsibilities": ["课堂教学"],
            "nature": "全职",
            "work_time": ["工作日"],
            "rest_schedule": ["周末双休"],
            "salary": ["面议"],
            "hard_gates": ["本科及以上"],
            "soft_traits": ["耐心"],
        },
        "dialog_check_items": ["是否接受晚间加班"],
        "exp_keywords": ["授课", "代课"],
        "score_rules": [
            _rule("exp_tags", "has_any", ["授课"], 5),
            _rule("degree", "not_equals", ["本科"], 8, rid="r2"),
        ],
        "level_threshold": {"high_min": 3, "mid_min": 1},
        "alternate_role_mapping": [],
    }
    out = normalize_job_config(config)
    add = out.get("score_rules", {}).get("add_score") or []
    check("扁平数组被归一化到 add_score", len(add) == 2, str(out.get("score_rules")))
    check(
        "扁平数组里的 op 同样被收敛",
        add[1].get("op") == "not_in",
        str(add),
    )

    # 分组写明时，扣分项留在扣分组，op 形状照样收敛
    grouped = normalize_job_config(
        {
            **config,
            "score_rules": {
                "add_score": [_rule("exp_tags", "has_any", ["授课"], 5)],
                "minus_score": [_rule("degree", "not_equals", ["本科"], 8, rid="r2")],
            },
        }
    )
    minus = grouped.get("score_rules", {}).get("minus_score") or []
    check("扣分项单独成组", len(minus) == 1, str(grouped.get("score_rules")))
    check("扣分项 op 收敛为 not_in", minus[0].get("op") == "not_in", str(minus))


def test_normalized_passes_strict_validation() -> None:
    section("5. 归一化后能通过 strict 校验")
    config = {
        "job_key": "junior_high_math_teacher",
        "job_name": "初中数学教师",
        "job_profile": {
            "responsibilities": ["课堂教学"],
            "nature": "全职",
            "work_time": ["工作日"],
            "rest_schedule": ["周末双休"],
            "salary": ["面议"],
            "hard_gates": ["本科及以上"],
            "soft_traits": ["耐心"],
        },
        "dialog_check_items": ["是否接受晚间加班"],
        "exp_keywords": ["授课", "代课"],
        "score_rules": [
            _rule("exp_tags", "has_any", ["授课"], 5),
            _rule("degree", "not_equals", ["本科"], 8, rid="r2"),
        ],
        "level_threshold": {"high_min": 3, "mid_min": 1},
        "alternate_role_mapping": [],
    }
    normalized = normalize_job_config(config)
    issues = validate_job_config(normalized, strict=True)
    hard = [i for i in issues if not i.startswith("[警告]")]
    check("没有结构性错误", not hard, "；".join(hard))


def main() -> int:
    test_flat_list_is_kept()
    test_op_value_shape_converges()
    test_single_value_ops_untouched()
    test_normalize_job_config_wiring()
    test_normalized_passes_strict_validation()

    print(f"\n{'=' * 66}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  FAIL {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
