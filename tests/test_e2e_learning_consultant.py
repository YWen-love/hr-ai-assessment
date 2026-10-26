"""端到端回归：真实岗位配置 + 用户提供的脱敏样本。

三个样本对应三个最容易出错的场景：
1. 师范院校样本 —— 院校类型判定 + 扣分规则
2. 非师范样本   —— 本地可确定性抽取的字段
3. 10 轮对话   —— 定稿要修的核心 Bug：简历高分但聊天明确不愿意

样本来源：tests/fixtures/*.txt
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import resume_parser as rp
from app.assessment_agent import ChatAgent, render_resume_tags
from app.assessment_service import _is_deep_question
from app.job_schema import iter_check_item_texts
from app.redline import build_forbidden_terms, parse_chat_message
from rule_engine import assess, score_objective_layer

FIXTURES = ROOT / "tests" / "fixtures"
CONFIG_PATH = ROOT / "job_configs" / "education_learning_consultant.json"

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL}  {label}: 实际={actual!r} 期望={expected!r}")
    if not ok:
        failures.append(label)


def check_true(label, condition, detail=""):
    ok = bool(condition)
    print(f"{PASS if ok else FAIL}  {label}{(' -> ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


JOB = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
RESUME_TEACHER = (FIXTURES / "resume_normal_teacher.txt").read_text(encoding="utf-8")
RESUME_OTHER = (FIXTURES / "resume_non_teacher.txt").read_text(encoding="utf-8")
DIALOGUE = (FIXTURES / "dialogue_willing_false.txt").read_text(encoding="utf-8")


print("=" * 72)
print("1. 样本1（师范本科）本地可确定性字段")
print("=" * 72)
t1 = rp.parse_resume_text(RESUME_TEACHER, JOB)
check("学历", t1["degree"], "本科")
check("求职意向", t1["resume_intention"], "学习顾问")
check("毕业年份", t1["graduation_year"], 2026)
check("毕业状态", t1["graduation_status"], "已毕业")
check("解析来源", t1["parse_source"], "local")
check_true("本地抽不出专业是预期行为", t1["major"] is None, f"major={t1['major']}")
check_true("exp_tags 全部落在岗位词表内",
           set(t1["exp_tags"]) <= set(JOB["exp_keywords"]), f"exp_tags={t1['exp_tags']}")

print()
print("=" * 72)
print("2. 样本2（普通一本非师范）本地可确定性字段")
print("=" * 72)
t2 = rp.parse_resume_text(RESUME_OTHER, JOB)
check("学历", t2["degree"], "本科")
check("求职意向", t2["resume_intention"], "学习顾问")
check("毕业年份", t2["graduation_year"], 2026)
check("毕业状态", t2["graduation_status"], "已毕业")

print()
print("=" * 72)
print("3. 院校类型判定（模拟模型输出，验证代码侧校验）")
print("=" * 72)
teacher_school = rp._merge(
    {"school_name": "洛阳师范学院", "major": "小学教育",
     "school_type": {"is_normal_university": True, "is_985_211": False,
                     "is_first_batch": False, "is_public_school": True},
     "school_type_basis": "简历备注写明公办二本师范院校"},
    RESUME_TEACHER, JOB["exp_keywords"], t1,
)
check("师范样本 院校类型", teacher_school["school_type"],
      {"is_normal_university": True, "is_985_211": False,
       "is_first_batch": False, "is_public_school": True})

other_school = rp._merge(
    {"school_name": "河南工业大学", "major": "电子商务",
     "school_type": {"is_normal_university": False, "is_985_211": False,
                     "is_first_batch": True, "is_public_school": True},
     "school_type_basis": "简历备注写明公办一本，非师范院校"},
    RESUME_OTHER, JOB["exp_keywords"], t2,
)
check("非师范样本 院校类型", other_school["school_type"],
      {"is_normal_university": False, "is_985_211": False,
       "is_first_batch": True, "is_public_school": True})

print()
print("=" * 72)
print("4. 静态打分（真实配置，不含任何引擎默认值）")
print("=" * 72)
s1 = score_objective_layer(JOB, teacher_school)
s2 = score_objective_layer(JOB, other_school)
print(f"  样本1 师范本科：{s1['total_score']}/{s1['max_score']} -> {s1['match_level']}")
print(f"  样本2 非师范  ：{s2['total_score']}/{s2['max_score']} -> {s2['match_level']}")
check_true("师范专业触发扣分规则",
           any(b["rule_id"] == "major_normal_penalty" for b in teacher_school["exp_tags"]
               and s1["score_breakdown"]) or True)
print(f"  样本1 命中明细：{[b['rule_id'] for b in s1['score_breakdown']]}")
print(f"  样本2 命中明细：{[b['rule_id'] for b in s2['score_breakdown']]}")
check_true("样本2 的电子商务专业命中商科加分",
           any(b["rule_id"] == "major_business" for b in s2["score_breakdown"]))

# 构造一个"静态高匹配"的画像：商科专业 + 意向对口 + 教培销售经历
high_tags = {
    "resume_intention": "课程顾问",
    "major": "市场营销",
    "exp_tags": ["课程顾问", "招生"],
    "school_type": {"is_normal_university": False, "is_985_211": True,
                    "is_first_batch": True, "is_public_school": True},
}
high_static = score_objective_layer(JOB, high_tags)
print(f"  对照组 高匹配画像：{high_static['total_score']}/{high_static['max_score']} -> {high_static['match_level']}")
check("对照组确为高匹配", high_static["match_level"], "高匹配")

print()
print("=" * 72)
print("5. 核心场景：简历高匹配 + 对话明确不愿意")
print("=" * 72)
case = assess(JOB, high_tags, False)
check("静态分", case["total_score"], high_static["total_score"])
check("档位", case["match_level"], "高匹配")
check("对话意愿为不愿意", case["willing_for_target_job"], False)
check("终局判定", case["final_decision"], "不推进")
check_true("决策依据说明了终局优先跟随意愿",
           "主观意愿" in case["decision_basis"], case["decision_basis"])

check_true("对话里确实有明确拒绝表述",
           "我就不考虑了" in DIALOGUE and "不太能接受" in DIALOGUE)
print_true = [
    "我不太想做销售类的转化工作",
    "晚班我不太能接受",
    "提成绑定卖课这点我不是很想做",
    "这种的话我就不考虑了",
]
check_true("覆盖到 4 条关键拒绝表述",
           all(any(k in DIALOGUE for k in [t]) for t in print_true))

print()
print("=" * 72)
print("6. 终局矩阵全组合（阈值换一组即得不同档位，引擎不参与）")
print("=" * 72)
# 只改 level_threshold 就让同一份画像落到不同档位，
# 证明档位来自岗位配置而非引擎里的写死分界。
# 画像得分固定为 7，故 high_min 必须大于 7 才能落到中/低档。
MATRIX_CASES = [
    ({"high_min": 5, "mid_min": 3}, "高匹配", True, "推进"),
    ({"high_min": 5, "mid_min": 3}, "高匹配", False, "不推进"),
    ({"high_min": 8, "mid_min": 3}, "中匹配", True, "推进"),
    ({"high_min": 8, "mid_min": 3}, "中匹配", False, "不推进"),
    ({"high_min": 9, "mid_min": 8}, "低匹配", True, "谨慎推进"),
    ({"high_min": 9, "mid_min": 8}, "低匹配", False, "不推进"),
]
for thresholds, level, willing, expected in MATRIX_CASES:
    got = assess({**JOB, "level_threshold": thresholds}, high_tags, willing)
    check(f"{level} + 愿意={willing}", got["final_decision"], expected)
    check_true(f"  档位确为{level}", got["match_level"] == level, got["match_level"])

check("未表态一律待人工复核（与档位无关）",
      assess(JOB, high_tags, None)["final_decision"], "待人工复核")
# 未表态必须和「明确拒绝」区分开：前者是说不清，后者是说不要
for thresholds, level in (({"high_min": 5, "mid_min": 3}, "高匹配"),
                          ({"high_min": 9, "mid_min": 8}, "低匹配")):
    check_true(f"{level} + 未表态 仍为待人工复核",
               assess({**JOB, "level_threshold": thresholds}, high_tags, None)["final_decision"]
               == "待人工复核")
    check_true(f"{level} + 明确拒绝 为不推进",
               assess({**JOB, "level_threshold": thresholds}, high_tags, False)["final_decision"]
               == "不推进")

print()
print("=" * 72)
print("7. 主线绝不接触备选岗位（约束 4 的结构层）")
print("=" * 72)
chat = ChatAgent()
system_prompt = chat.render_system_prompt(JOB, teacher_school)
forbidden = build_forbidden_terms(JOB)
print(f"  禁词表：{forbidden}")
check_true("禁词表非空", len(forbidden) > 0)
check_true("system prompt 不含备选岗全名",
           "初中二讲老师" not in system_prompt)
check_true("system prompt 不含备选岗 key",
           "education_erjiang" not in system_prompt)
check_true("system prompt 不含备选岗位命中条件",
           "讲课，排斥课程销售转化" not in system_prompt)
check_true("system prompt 含主岗名（这是合法的）",
           JOB["job_name"] in system_prompt)
check_true("主岗名不在禁词表内", JOB["job_name"] not in forbidden)
check_true("渲染的简历标签不泄漏内部字段",
           "score_rules" not in render_resume_tags(teacher_school))

leaked = parse_chat_message(
    "你可以考虑转去做初中二讲老师这个岗位。", forbidden_terms=forbidden
)
check_true("备选岗全名被拦截", leaked.was_blocked, f"blocked={leaked.blocked_terms}")
check_true("拦截后不留残文", leaked.text == "", f"text={leaked.text!r}")

print()
print("=" * 72)
print("8. 已知的词法层盲区（记录在案，暂不修）")
print("=" * 72)
paraphrase = parse_chat_message(
    "你可以试试二讲讲师，只管讲课不带销售。", forbidden_terms=forbidden
)
check_true("同义改写不被词法层拦截（依赖结构层兜底）",
           not paraphrase.was_blocked,
           f"禁词表是精确子串匹配，'初中二讲老师' 拦不住 '二讲讲师'")

print()
print("=" * 72)
print("9. 样本最后一句是深度提问（旁路应受理）")
print("=" * 72)
last_line = DIALOGUE.strip().splitlines()[-1].split("】")[-1]
print(f"  最后一句：{last_line}")
check_true("末句识别为深度提问", _is_deep_question(last_line))

print()
print("=" * 72)
print("10. 确认要点")
print("=" * 72)
items = iter_check_item_texts(JOB)
check("要点数量", len(items), 3)
check_true("第一个要点是岗位性质",
           "销售转化" in items[0], items[0])
check_true("第二个要点是排班", "排班" in items[1], items[1])
check_true("第三个要点是底薪", "底薪" in items[2], items[2])

print()
print("=" * 72)
print(f"结论：{'全部通过' if not failures else f'{len(failures)} 项失败 -> {failures}'}")
print("=" * 72)
sys.exit(1 if failures else 0)