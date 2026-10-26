"""备选岗位（alternate_role_mapping）形状归一化的回归测试。

背景：JD → 配置这条主流程里，模型实测会只吐岗位名
（["初中数学备课组长"]），而校验器只认
{backup_job_key, backup_job_name, match_condition}。形状对不上时校验直接报错，
整份配置存不进库，「上传 JD → 自动生成 → 保存」这条主流程就断了。

这里锁住两件事：
1. 归一化把各种形状漂移都收敛成规范形状，且不留垃圾条目。
2. 归一化后的配置能通过 strict 校验（= 能真的存进库）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.job_schema import (
    BACKUP_CONDITION_FIELD,
    BACKUP_KEY_FIELD,
    BACKUP_NAME_FIELD,
    count_backups_without_conditions,
    is_valid_job_key,
    normalize_backups,
    normalize_job_config,
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


def test_canonical_passthrough() -> None:
    section("1. 规范形状原样保留")
    entry = {
        BACKUP_KEY_FIELD: "education_erjiang",
        BACKUP_NAME_FIELD: "初中二讲老师",
        BACKUP_CONDITION_FIELD: ["有公立校初中教学经历"],
    }
    out = normalize_backups([entry])
    check("长度不变", len(out) == 1, str(out))
    check("内容原样保留", out[0] == entry, str(out))


def test_string_entries() -> None:
    section("2. 模型只给岗位名（实测最常见的漂移）")
    out = normalize_backups(["初中数学备课组长", "初中数学班主任"])
    check("两条都收下", len(out) == 2, str(out))
    check(
        "字段名补齐",
        all(
            set(b) == {BACKUP_KEY_FIELD, BACKUP_NAME_FIELD, BACKUP_CONDITION_FIELD}
            for b in out
        ),
        str(out),
    )
    check(
        "岗位名保留",
        [b[BACKUP_NAME_FIELD] for b in out] == ["初中数学备课组长", "初中数学班主任"],
        str(out),
    )
    check(
        "key 是合法 job_key",
        all(is_valid_job_key(b[BACKUP_KEY_FIELD]) for b in out),
        str(out),
    )
    check(
        "key 互不重复",
        len({b[BACKUP_KEY_FIELD] for b in out}) == 2,
        str(out),
    )
    check(
        "命中条件按岗位名兜了一条",
        all(b[BACKUP_CONDITION_FIELD] for b in out),
        str(out),
    )
    check(
        "兜的条件用模型自己的措辞",
        "初中数学备课组长" in out[0][BACKUP_CONDITION_FIELD][0],
        str(out),
    )


def test_alias_drift() -> None:
    section("3. 字段名漂移（模型写成 job_key / conditions）")
    out = normalize_backups(
        [{"job_key": "edu_x", "job_name": "初中二讲老师", "conditions": ["有教学经历"]}]
    )
    check("长度不变", len(out) == 1, str(out))
    check("key 归位", out[0][BACKUP_KEY_FIELD] == "edu_x", str(out))
    check("name 归位", out[0][BACKUP_NAME_FIELD] == "初中二讲老师", str(out))
    check("条件归位", out[0][BACKUP_CONDITION_FIELD] == ["有教学经历"], str(out))


def test_invalid_key_repaired() -> None:
    section("4. 非法 key（中文 / 开头是数字）被修成合法 key")
    out = normalize_backups(
        [
            {BACKUP_KEY_FIELD: "初中二讲老师", BACKUP_NAME_FIELD: "初中二讲老师"},
            {BACKUP_KEY_FIELD: "9teacher", BACKUP_NAME_FIELD: "实习老师"},
        ]
    )
    check(
        "key 全部合法",
        all(is_valid_job_key(b[BACKUP_KEY_FIELD]) for b in out),
        str(out),
    )


def test_junk_dropped() -> None:
    section("5. 垃圾条目被丢掉（连岗位名都没有的留不下）")
    out = normalize_backups([None, 123, {}, "", "   ", {"backup_job_key": "a_b"}])
    check(
        "只留下有岗位名/可派生名的那条",
        len(out) == 1 and out[0][BACKUP_KEY_FIELD] == "a_b",
        str(out),
    )


def test_duplicate_keys() -> None:
    section("6. 同一条配置里 key 不能重复")
    out = normalize_backups(
        [
            {BACKUP_KEY_FIELD: "same_key", BACKUP_NAME_FIELD: "岗位A"},
            {BACKUP_KEY_FIELD: "same_key", BACKUP_NAME_FIELD: "岗位B"},
        ]
    )
    check("两条都在", len(out) == 2, str(out))
    check(
        "key 被去重",
        len({b[BACKUP_KEY_FIELD] for b in out}) == 2,
        str(out),
    )


def test_normalize_job_config_wires_it() -> None:
    section("7. normalize_job_config 把归一化接上了")
    config = normalize_job_config(
        {
            "job_key": "demo",
            "job_name": "示例岗",
            "alternate_role_mapping": ["初中数学备课组长"],
        }
    )
    backups = config["alternate_role_mapping"]
    check("归一化已生效", len(backups) == 1 and isinstance(backups[0], dict), str(backups))
    check("字段名是规范名", set(backups[0]) == {
        BACKUP_KEY_FIELD, BACKUP_NAME_FIELD, BACKUP_CONDITION_FIELD
    }, str(backups))


def test_survives_strict_validation() -> None:
    section("8. 归一化后的配置能过 strict 校验（= 真的存得进库）")
    config = normalize_job_config(
        {
            "job_key": "demo",
            "job_name": "示例岗",
            "job_profile": {
                "responsibilities": ["讲课"],
                "work_time": ["周一至周五"],
                "salary": ["面议"],
            },
            "dialog_check_items": ["是否接受晚间仍在岗"],
            "exp_keywords": ["讲课"],
            "score_rules": {
                "add_score": [
                    {
                        "id": "r1",
                        "field": "exp_tags",
                        "op": "has_any",
                        "value": ["讲课"],
                        "score": 3,
                        "desc": "有讲课经历",
                    }
                ],
                "minus_score": [],
            },
            "level_threshold": {"high_min": 5, "mid_min": 3},
            "alternate_role_mapping": ["初中数学备课组长"],
        }
    )
    errors = validate_job_config(config)
    check("strict 校验通过", not errors, "; ".join(errors))


def test_count_missing_conditions() -> None:
    section("9. 缺命中条件的条数统计（生成结果要据此提醒 HR）")
    check("字符串条目算缺条件", count_backups_without_conditions(["岗位A"]) == 1)
    check(
        "规范条目不算缺",
        count_backups_without_conditions(
            [
                {
                    BACKUP_KEY_FIELD: "a_b",
                    BACKUP_NAME_FIELD: "岗位A",
                    BACKUP_CONDITION_FIELD: ["有教学经历"],
                }
            ]
        )
        == 0,
    )
    check("空数组算 0", count_backups_without_conditions([]) == 0)


def main() -> int:
    print("=" * 66)
    print("备选岗位形状归一化")
    print("=" * 66)
    for test in (
        test_canonical_passthrough,
        test_string_entries,
        test_alias_drift,
        test_invalid_key_repaired,
        test_junk_dropped,
        test_duplicate_keys,
        test_normalize_job_config_wires_it,
        test_survives_strict_validation,
        test_count_missing_conditions,
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
