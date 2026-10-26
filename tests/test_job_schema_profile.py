"""job_profile 键名归一化 + 生成 Prompt 装配的回归测试。

背景：JD → 配置这条主流程里，Prompt 只用中文描述 job_profile 的字段
（「职责、性质、工作时间……」），模型就把中文直接当键名写出来
（"职责": [...]）。运行时按 responsibilities 取值，取不到就是空 ——
内容明明在，报告里却显示「（未配置）」，HR 也看不出是模型写错了键名。

这里锁住三件事：
1. 中文键名被收敛成规范英文键，内容不丢。
2. 规范键已存在时不被别名顶掉。
3. 生成 Prompt 里的占位符全部被替换（不留 {{...}} 残渣）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.job_schema import (
    job_profile_table_for_prompt,
    normalize_job_config,
    normalize_job_profile,
    score_rule_table_for_prompt,
)
from app.job_generator import _build_config
from prompts import JD_TO_CONFIG_SYSTEM_PROMPT

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(
        f"  {'OK  ' if condition else 'FAIL'} {name}"
        + (f"  <- {detail}" if not condition and detail else "")
    )


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def test_chinese_keys_converge() -> None:
    section("1. 中文键名收敛（实测模型写法）")
    profile = {
        "职责": ["课堂教学", "作业批改"],
        "性质": [],
        "工作时间": [],
        "休息": ["周末双休"],
        "薪资": [],
        "硬性门槛": ["本科及以上学历"],
        "软性素质": ["耐心"],
    }
    out = normalize_job_profile(profile)
    check(
        "职责 → responsibilities",
        out.get("responsibilities") == ["课堂教学", "作业批改"],
        str(out),
    )
    check("性质 → nature", "nature" in out, str(out))
    check("工作时间 → work_time", "work_time" in out, str(out))
    check("休息 → rest_schedule", out.get("rest_schedule") == ["周末双休"], str(out))
    check("薪资 → salary", "salary" in out, str(out))
    check("硬性门槛 → hard_gates", out.get("hard_gates") == ["本科及以上学历"], str(out))
    check("软性素质 → soft_traits", out.get("soft_traits") == ["耐心"], str(out))
    check("没有残留中文键", not any(k in out for k in profile), str(out))


def test_english_variant_keys_converge() -> None:
    section("2. 英文近义键收敛（实测模型写法）")
    # 实测：模型不写中文键时，会自己「翻译」成近义英文键
    profile = {
        "responsibilities": ["课堂教学"],
        "working_hours": ["周一至周五 9:00-18:00"],
        "rest": ["周末双休"],
        "hard_requirements": ["本科及以上"],
        "soft_qualities": ["耐心"],
    }
    out = normalize_job_profile(profile)
    check(
        "working_hours → work_time",
        out.get("work_time") == ["周一至周五 9:00-18:00"],
        str(out),
    )
    check("rest → rest_schedule", out.get("rest_schedule") == ["周末双休"], str(out))
    check("hard_requirements → hard_gates", out.get("hard_gates") == ["本科及以上"], str(out))
    check("soft_qualities → soft_traits", out.get("soft_traits") == ["耐心"], str(out))
    check(
        "没有残留英文变体键",
        not any(k in out for k in ("working_hours", "rest", "hard_requirements", "soft_qualities")),
        str(out),
    )
    check("规范键没被顶掉", out.get("responsibilities") == ["课堂教学"], str(out))


def test_canonical_key_wins() -> None:
    section("3. 规范键已存在时不被别名顶掉")
    out = normalize_job_profile(
        {"responsibilities": ["规范键的值"], "职责": ["别名的值"]}
    )
    check("规范键优先", out.get("responsibilities") == ["规范键的值"], str(out))


def test_unknown_keys_kept() -> None:
    section("4. 表外键保留")
    out = normalize_job_profile({"职责": ["a"], "自定义字段": ["b"]})
    check("表外键不丢", out.get("自定义字段") == ["b"], str(out))


def test_normalize_job_config_wiring() -> None:
    section("5. normalize_job_config 接线")
    out = normalize_job_config(
        {"job_key": "k", "job_name": "n", "job_profile": {"职责": ["课堂教学"]}}
    )
    check(
        "job_profile 走归一化",
        out.get("job_profile", {}).get("responsibilities") == ["课堂教学"],
        str(out.get("job_profile")),
    )


def test_generator_wiring() -> None:
    section("6. 生成器接线")
    config = _build_config(
        {"job_name": "初中数学教师", "job_profile": {"职责": ["课堂教学"]}},
        jd_hint="岗位名称：初中数学教师",
        job_key=None,
    )
    check(
        "生成结果里的职责落到规范键",
        config.get("job_profile", {}).get("responsibilities") == ["课堂教学"],
        str(config.get("job_profile")),
    )


def test_prompt_has_no_leftover_placeholder() -> None:
    section("7. Prompt 占位符全部替换")
    prompt = (
        JD_TO_CONFIG_SYSTEM_PROMPT
        .replace("{{field_table}}", "FIELD")
        .replace("{{operator_table}}", "OPERATOR")
        .replace("{{backup_table}}", "BACKUP")
        .replace("{{score_rule_table}}", score_rule_table_for_prompt())
        .replace("{{job_profile_table}}", job_profile_table_for_prompt())
    )
    check("没有残留 {{...}}", "{{" not in prompt, prompt[prompt.find("{{") - 60:][:200])
    check("字段表已渲染", "responsibilities" in prompt)
    check("打分规则表已渲染", "add_score" in prompt)


def main() -> int:
    test_chinese_keys_converge()
    test_english_variant_keys_converge()
    test_canonical_key_wins()
    test_unknown_keys_kept()
    test_normalize_job_config_wiring()
    test_generator_wiring()
    test_prompt_has_no_leftover_placeholder()

    print(f"\n{'=' * 66}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  FAIL {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
