"""打分引擎通用性回归测试。

证明一件事：引擎不含任何岗位知识。任意两套规则/分值/阈值完全不同、字段完全不相干的
岗位配置，只靠各自 job_config 就能跑出正确结果，全程不需要改引擎代码。

这份测试是新增岗位的护栏：以后谁往引擎里写死业务值，这里的断言会直接失败。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.job_schema import (
    final_decision,
    level_for_score,
    score_rules_for,
    validate_job_config,
)

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL}  {label}: 实际={actual!r} 期望={expected!r}")
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------------------
# 岗位甲：销售向，沿用学习顾问的规则形态（字符串字段 + 列表字段 + bool 字段）
# ---------------------------------------------------------------------------
JOB_SALES = {
    "schema_version": "1.0",
    "job_key": "b2b_sales",
    "job_name": "B端销售代表",
    "job_profile": {
        "responsibilities": ["开拓客户", "维护存量客户"],
        "work_time": ["周一至周五 9:00-18:00"],
        "salary": ["底薪3000加提成"],
    },
    "dialog_check_items": [{"id": "c1", "text": "是否接受外出拜访"}],
    "exp_keywords": ["客户开发", "招投标"],
    "score_rules": {
        "add_score": [
            {"id": "s1", "field": "resume_intention", "op": "contains_any",
             "value": ["销售", "商务"], "score": 3, "desc": "意向为销售方向"},
            {"id": "s2", "field": "exp_tags", "op": "has_any",
             "value": ["客户开发"], "score": 4, "desc": "有客户开发经历"},
            {"id": "s3", "field": "school_type.is_985_211", "op": "is_true",
             "value": True, "score": 1, "desc": "985背景"},
        ],
        "minus_score": [
            {"id": "s4", "field": "major", "op": "contains_any",
             "value": ["师范"], "score": 2, "desc": "师范背景扣分"},
        ],
    },
    "level_threshold": {"high_min": 7, "mid_min": 4},
    "alternate_role_mapping": [],
}

# ---------------------------------------------------------------------------
# 岗位乙：完全不同 —— 字段不同、操作符不同、分值不同、阈值不同（分数上限8）
# ---------------------------------------------------------------------------
JOB_LAB = {
    "schema_version": "1.0",
    "job_key": "lab_technician",
    "job_name": "实验室技术员",
    "job_profile": {
        "responsibilities": ["样品检测", "设备维护"],
        "work_time": ["周一至周五 8:30-17:30"],
        "salary": ["五险一金", "月综合8-12K"],
    },
    "dialog_check_items": [{"id": "c1", "text": "是否接受实验室无菌环境作业"}],
    "exp_keywords": ["理化检验", "色谱"],
    "score_rules": {
        "add_score": [
            {"id": "l1", "field": "major", "op": "in",
             "value": ["化学", "应用化学", "生物技术"], "score": 5, "desc": "化学/生物相关专业"},
            {"id": "l2", "field": "degree", "op": "equals",
             "value": "本科", "score": 2, "desc": "本科学历"},
            {"id": "l3", "field": "exp_tags", "op": "has_all",
             "value": ["理化检验"], "score": 3, "desc": "有理化检验经历"},
        ],
        "minus_score": [],
    },
    "level_threshold": {"high_min": 8, "mid_min": 5},
    "alternate_role_mapping": [],
}

print("=" * 68)
print("1. 两份配置都通过校验（引擎不关心业务内容）")
print("=" * 68)
check("B端销售配置", validate_job_config(JOB_SALES), [])
check("实验室技术员配置", validate_job_config(JOB_LAB), [])

print()
print("=" * 68)
print("2. 同一份简历标签，两个岗位算出完全不同的分数")
print("=" * 68)
# 简历：化学专业本科、有理化检验经历、意向销售、211
SHARED_TAGS = {
    "degree": "本科",
    "major": "应用化学",
    "resume_intention": "销售/商务",
    "exp_tags": ["理化检验", "色谱"],
    "school_type": {"is_985_211": True, "is_normal_university": False,
                    "is_first_batch": True, "is_public_school": True},
}
sales_score, sales_hits = score_rules_for(JOB_SALES, SHARED_TAGS)
lab_score, lab_hits = score_rules_for(JOB_LAB, SHARED_TAGS)
check("B端销售得分", sales_score, 4)      # s1(+3) + s2不命中 + s3(+1)
check("实验室得分", lab_score, 10)        # l1(+5) + l2(+2) + l3(+3)
check("销售命中规则", [h["id"] for h in sales_hits], ["s1", "s3"])
check("实验室命中规则", [h["id"] for h in lab_hits], ["l1", "l2", "l3"])

print()
print("=" * 68)
print("3. 阈值不同 → 同一个分数落在不同档位（引擎不自带分界）")
print("=" * 68)
# 学习顾问阈值 high=5/mid=3，实验室阈值 high=8/mid=5
LEARN = {"level_threshold": {"high_min": 5, "mid_min": 3}}
LAB_T = {"level_threshold": {"high_min": 8, "mid_min": 5}}
for score in (0, 3, 5, 7, 10):
    check(
        f"分数{score:<3} 学习顾问档位",
        level_for_score(LEARN, score),
        "高匹配" if score >= 5 else ("中匹配" if score >= 3 else "低匹配"),
    )
    check(
        f"分数{score:<3} 实验室档位  ",
        level_for_score(LAB_T, score),
        "高匹配" if score >= 8 else ("中匹配" if score >= 5 else "低匹配"),
    )

print()
print("=" * 68)
print("4. 引擎不得自带默认阈值（缺配置必须报错，不能静默套用别的岗位的分界）")
print("=" * 68)
for label, bad in [
    ("缺 level_threshold", {"score_rules": {}}),
    ("缺 high_min", {"level_threshold": {"mid_min": 3}}),
    ("mid >= high", {"level_threshold": {"high_min": 3, "mid_min": 5}}),
]:
    try:
        level_for_score(bad, 7)
        print(f"{FAIL}  {label}: 应当抛错却返回了档位")
        failures.append(label)
    except ValueError as exc:
        print(f"{PASS}  {label}: 正确抛错 -> {exc}")

print()
print("=" * 68)
print("5. 终局矩阵与档位解耦（客观分 + 主观意向）")
print("=" * 68)
check("高匹配+愿意", final_decision("高匹配", True), "推进")
check("高匹配+不愿意（核心Bug场景）", final_decision("高匹配", False), "不推进")
check("低匹配+愿意", final_decision("低匹配", True), "谨慎推进")
check("低匹配+不愿意", final_decision("低匹配", False), "不推进")
check("未知档位兜底", final_decision("未知", True), "不推进")

print()
print("=" * 68)
print(f"结论：{'全部通过' if not failures else f'{len(failures)} 项失败 -> {failures}'}")
print("=" * 68)
sys.exit(1 if failures else 0)