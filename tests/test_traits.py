"""主观层（conversation_traits）单元测试。

不调用 LLM，只验证两件事：
1. Prompt 模板不含任何岗位名词 —— 违反就会让"引擎不含业务知识"这条约束失效
2. 模型输出的清洗逻辑够严 —— 分类非法、意愿值不明确都要被正确处理
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.traits import (
    TRAIT_CATEGORIES,
    TRAIT_EXTRACT_SYSTEM_PROMPT,
    _normalize,
    empty_traits,
    trait_summary,
)

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL}  {label}: 实际={actual!r} 期望={expected!r}")
    if not ok:
        failures.append(label)


print("=" * 68)
print("1. Prompt 模板不得含任何岗位名词（引擎通用性硬约束）")
print("=" * 68)
# 这些词一旦出现在 Prompt 里，就说明业务知识漏进引擎了
FORBIDDEN = [
    "学习顾问", "二讲", "人力实习", "课程顾问", "授课", "代课", "主讲",
    "教培", "教育", "K12", "销售", "教师", "讲课", "师范", "顾问",
]
leaked = [word for word in FORBIDDEN if word in TRAIT_EXTRACT_SYSTEM_PROMPT]
check("Prompt 中的岗位名词", leaked, [])
print(f"  模板占位符: {[w for w in ('{{job_name}}', '{{check_items}}') if w in TRAIT_EXTRACT_SYSTEM_PROMPT]}")

print()
print("=" * 68)
print("2. 偏好分类枚举固定，不允许模型自由发挥")
print("=" * 68)
check("分类数量", len(TRAIT_CATEGORIES), 6)

raw_good = {
    "conversation_traits": [
        {"category": "job_preference", "content": "更想面授课程", "evidence": "我更想面授课程"},
        {"category": "avoidance", "content": "不想接打电话", "evidence": "我不太想接打电话"},
        {"category": "made_up", "content": "不该被保留", "evidence": "x"},
        {"category": "job_preference", "content": "", "evidence": "空内容"},
        "not-a-dict",
    ],
    "willing_for_target_job": True,
    "check_results": [
        {"text": "接受岗位性质", "accepted": True},
        {"text": "缺 accepted 字段", "accepted": "yes"},
    ],
    "reason": "候选人接受了三点确认",
}
result = _normalize(raw_good, "")
check("合法偏好条数", len(result["conversation_traits"]), 2)
check("非法分类已剔除", [t["content"] for t in result["conversation_traits"]],
      ["更想面授课程", "不想接打电话"])
check("意愿值", result["willing_for_target_job"], True)
check("check_results 条数（缺字段的应剔除）", len(result["check_results"]), 1)

print()
print("=" * 68)
print("3. 意愿值不明确时必须为 null，不能猜（终局矩阵依赖它）")
print("=" * 68)
for label, value in [("字符串", "true"), ("空值", None), ("数字", 1), ("中文", "愿意")]:
    out = _normalize({"willing_for_target_job": value}, "")
    check(f"模型返回 {label}", out["willing_for_target_job"], None)

print()
print("=" * 68)
print("4. 坏输入不炸")
print("=" * 68)
for label, bad in [
    ("空对象", {}),
    ("列表", []),
    ("字段为None", {"conversation_traits": None, "willing_for_target_job": None}),
    ("字符串冒充对象", {"conversation_traits": ["偏好描述"]}),
]:
    try:
        out = _normalize(bad, "")
        ok = isinstance(out, dict) and "conversation_traits" in out
        print(f"{PASS if ok else FAIL}  {label}: 归一化为空结构")
        if not ok:
            failures.append(label)
    except Exception as exc:
        print(f"{FAIL}  {label}: 抛异常 {type(exc).__name__}: {exc}")
        failures.append(label)

check("empty_traits 结构", sorted(empty_traits().keys()),
      ["check_results", "conversation_traits", "reason", "willing_for_target_job"])

print()
print("=" * 68)
print("5. 偏好摘要与备选岗粗筛")
print("=" * 68)
traits = [
    {"category": "job_preference", "content": "更想面授课程", "evidence": ""},
    {"category": "avoidance", "content": "排斥销售转化", "evidence": ""},
]
summary = trait_summary(traits)
print(f"  摘要: {summary}")
check("摘要含中文标签", "倾向岗位/职业方向" in summary and "明确排斥的方向" in summary, True)
check("空偏好摘要", trait_summary([]), "")

print()
print("=" * 68)
print(f"结论：{'全部通过' if not failures else f'{len(failures)} 项失败 -> {failures}'}")
print("=" * 68)
sys.exit(1 if failures else 0)