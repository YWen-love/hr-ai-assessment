"""打分规则分组名漂移 + 占位模板兜底提示的回归测试。

背景：模型返回的 score_rules 实测会漂三种形状：
1. 扁平数组（不分 add/minus）—— 已由 normalize_score_rules 收。
2. 分组名写成别名（{"加分项": [...]}、{"add": [...]}）——
   合并逻辑只认 add_score / minus_score 时，模型写的规则被整份丢掉。
3. 规则一条都用不上 —— 套上占位模板。模板里每条规则的 value 都是空的，
   永远不会命中，这个岗位对所有候选人的分数都一样。
   不把这件事说出来，HR 会以为模型已经配好了规则。

这里锁住：
1. 分组名别名被收敛，模型的规则一条不丢。
2. 规范分组名优先于别名。
3. 走占位模板时，提示里必须说清楚「永远不会命中」。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.job_generator import (
    DEFAULT_SCORE_RULES,
    JobConfigGenerator,
    _merge_score_rules,
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


def rule(rule_id: str = "r1", value: list | None = None) -> dict:
    return {
        "id": rule_id,
        "field": "exp_tags",
        "op": "has_any",
        "value": value if value is not None else ["分层教学"],
        "score": 5,
        "desc": "有分层教学经验",
    }


class FakeClient:
    """假模型客户端：返回固定 JSON，不发真实请求。"""

    def __init__(self, config: dict):
        self.config = config

    async def chat_json(self, system_prompt: str, user_prompt: str, **kwargs) -> dict:
        return json.loads(json.dumps(self.config, ensure_ascii=False))


def model_output(score_rules) -> dict:
    return {
        "job_key": "junior_math_teacher",
        "job_name": "初中数学教师",
        "job_profile": {
            "responsibilities": ["负责初中数学课堂教学"],
            "work_time": ["周一至周五 9:00-18:00"],
            "salary": ["面议"],
        },
        "dialog_check_items": [{"text": "是否接受晚间 10 点后仍在岗"}],
        "exp_keywords": ["分层教学"],
        "score_rules": score_rules,
        "level_threshold": {"high_min": 20, "mid_min": 12},
        "alternate_role_mapping": [],
    }


def test_chinese_group_alias() -> None:
    section("1. 中文分组名收敛（模型写的 加分项 / 扣分项）")
    merged, note = _merge_score_rules(
        {"加分项": [rule()], "扣分项": [rule("m1", ["无教学经验"])]}
    )
    check(
        "加分项 → add_score",
        [r["id"] for r in merged["add_score"]] == ["r1"],
        str(merged),
    )
    check(
        "扣分项 → minus_score",
        [r["id"] for r in merged["minus_score"]] == ["m1"],
        str(merged),
    )
    check("没有兜底提示", note == "", note)


def test_english_group_alias() -> None:
    section("2. 英文分组名收敛（模型写的 add / minus）")
    merged, note = _merge_score_rules(
        {"add": [rule()], "minus": [rule("m1", ["无教学经验"])]}
    )
    check("add → add_score", [r["id"] for r in merged["add_score"]] == ["r1"], str(merged))
    check("minus → minus_score", [r["id"] for r in merged["minus_score"]] == ["m1"], str(merged))
    check("没有兜底提示", note == "", note)


def test_canonical_group_wins() -> None:
    section("3. 规范分组名优先于别名")
    merged, _ = _merge_score_rules(
        {"add_score": [rule("canonical")], "加分项": [rule("alias")]}
    )
    check(
        "用规范分组",
        [r["id"] for r in merged["add_score"]] == ["canonical"],
        str(merged),
    )


def test_flat_list_kept() -> None:
    section("4. 扁平数组按「全是加分项」处理")
    merged, note = _merge_score_rules([rule()])
    check("规则没丢", [r["id"] for r in merged["add_score"]] == ["r1"], str(merged))
    check("没有兜底提示", note == "", note)


def test_unusable_rules_are_loud() -> None:
    section("5. 规则用不上时，兜底提示必须说清楚")
    merged, note = _merge_score_rules({"rules": [rule()]})
    check("套上了占位模板", merged == DEFAULT_SCORE_RULES, str(merged))
    check("提示非空", bool(note), note)
    check("提示说明了永远不会命中", "永远不会命中" in note, note)
    check("提示说明了要人工补全", "手工补全" in note, note)


def test_missing_rules_are_loud() -> None:
    section("6. 模型没给打分规则时同样要说清楚")
    merged, note = _merge_score_rules(None)
    check("套上了占位模板", merged == DEFAULT_SCORE_RULES, str(merged))
    check("提示非空", bool(note), note)
    check("提示说明了永远不会命中", "永远不会命中" in note, note)


def test_warning_reaches_hr() -> None:
    section("7. 提示一路走到 HR 看到的 warnings 里")
    gen = JobConfigGenerator()
    gen.client = FakeClient(model_output({"rules": [rule()]}))
    result = asyncio.run(gen.generate("岗位名称：初中数学教师\n负责初中数学课堂教学。"))
    warnings = result["warnings"]
    check("warnings 里有兜底提示", any("永远不会命中" in w for w in warnings), str(warnings))
    check(
        "兜底提示排在最前面",
        bool(warnings) and "永远不会命中" in warnings[0],
        str(warnings[:2]),
    )

    gen2 = JobConfigGenerator()
    gen2.client = FakeClient(model_output({"add_score": [rule()], "minus_score": []}))
    result2 = asyncio.run(gen2.generate("岗位名称：初中数学教师\n负责初中数学课堂教学。"))
    check(
        "规则可用时不给兜底提示",
        not any("永远不会命中" in w for w in result2["warnings"]),
        str(result2["warnings"]),
    )
    check(
        "规则可用时模型写的值没被换掉",
        result2["config"]["score_rules"]["add_score"][0]["value"] == ["分层教学"],
        str(result2["config"]["score_rules"]),
    )


def main() -> int:
    test_chinese_group_alias()
    test_english_group_alias()
    test_canonical_group_wins()
    test_flat_list_kept()
    test_unusable_rules_are_loud()
    test_missing_rules_are_loud()
    test_warning_reaches_hr()

    print(f"\n{'=' * 66}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  FAIL {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
