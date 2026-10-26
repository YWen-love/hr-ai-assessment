"""P1 回归测试：简历解析 / 规则委托 / 会话字段 / 旁路闸门（纯本地，不发网络请求）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resume_parser as rp
from app.assessment_agent import render_resume_tags, format_transcript
from app.assessment_service import _is_deep_question
from app.text_match import find_hits, has_trace, tokenize
from rule_engine import assess, score_objective_layer
from resume_parser import detect_exp_tags

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


RESUME = """
张明  男  26岁
教育背景：湖南师范大学 汉语言文学（师范） 本科 2020.09-2024.06
求职意向：教师/授课讲师
实习经历：2023.07-2023.12 某教育机构 助教，负责代课与课后答疑
2022.08-2022.09 某教育机构 课程顾问助理
"""

JOB = {
    "job_key": "education_learning_consultant",
    "job_name": "初中学习顾问",
    "job_profile": {
        "responsibilities": ["添加家长微信", "体验课转化正价课"],
        "work_time": ["晚间工作"],
        "salary": ["底薪3000"],
    },
    "dialog_check_items": [{"id": "c1", "text": "是否接受晚间排班"}],
    "exp_keywords": ["教培销售", "课程顾问", "助教", "代课", "客户开发"],
    "score_rules": {
        "add_score": [
            {"id": "intent", "field": "resume_intention", "op": "contains_any",
             "value": ["教师", "授课"], "score": 2, "desc": "求职意向对口"},
            {"id": "edu", "field": "exp_tags", "op": "has_any",
             "value": ["课程顾问", "助教"], "score": 3, "desc": "有教培经历"},
        ],
        "minus_score": [],
    },
    "level_threshold": {"high_min": 5, "mid_min": 2},
    "alternate_role_mapping": [],
}

print("=" * 70)
print("1. 旧硬编码必须消失")
print("=" * 70)
tags = rp.parse_resume_text(RESUME, JOB)
check_true("不再返回 major='软件工程'", tags["major"] != "软件工程", f"major={tags['major']!r}")
check_true("不再返回 age=27", "age" not in tags)
check_true("不再返回 work_experience='无'", tags["work_experience"] != "无")
check_true("不再返回写死的 risk_flags",
           "专业与岗位不一致" not in tags["risk_flags"], f"risk_flags={tags['risk_flags']}")
check("本地解析学历", tags["degree"], "本科")
check("本地解析毕业年份", tags["graduation_year"], 2024)
check("毕业状态", tags["graduation_status"], "已毕业")

print()
print("=" * 70)
print("2. exp_tags 是岗位驱动的封闭词表")
print("=" * 70)
check("按岗位 exp_keywords 识别经验标签",
      tags["exp_tags"], ["课程顾问", "助教", "代课"])
other_job = {**JOB, "exp_keywords": ["Python", "机器学习"]}
check("换岗位则词表随之改变",
      rp.parse_resume_text(RESUME, other_job)["exp_tags"], [])

print()
print("=" * 70)
print("3. 模型幻觉标签会被代码丢弃")
print("=" * 70)
merged = rp._merge(
    {
        "degree": "本科",
        "major": "汉语言文学",
        "school_type": {"is_normal_university": True, "is_985_211": False,
                        "is_first_batch": False, "is_public_school": True,
                        "is_harvard": True},
        "exp_tag_evidence": [
            {"tag": "课程顾问", "evidence": "某教育机构 课程顾问助理"},
            {"tag": "五星级酒店前台", "evidence": "编的"},
        ],
    },
    RESUME, JOB["exp_keywords"], rp.parse_resume_text(RESUME, JOB),
)
check_true("幻觉标签被丢弃", "五星级酒店前台" not in merged["exp_tags"],
           f"exp_tags={merged['exp_tags']}")
check_true("合法标签被保留", "课程顾问" in merged["exp_tags"])
check_true("院校类型多吐的字段被丢弃",
           "is_harvard" not in merged["school_type"], f"keys={sorted(merged['school_type'])}")
check_true("院校类型缺依据时提示复核",
           any("人工复核" in w for w in merged["parse_warnings"]), f"warnings={merged['parse_warnings']}")

for label, bad in [("模型返回列表", []), ("模型返回字符串", "abc"), ("模型返回 null", None)]:
    merged_bad = rp._merge(bad, RESUME, JOB["exp_keywords"], rp.parse_resume_text(RESUME, JOB))
    check_true(f"{label} 不炸且降级", merged_bad["parse_source"] == "local_fallback")

print()
print("=" * 70)
print("4. 规则引擎完全委托给 job_config")
print("=" * 70)
score = score_objective_layer(JOB, merged)
check("命中两条加分", score["total_score"], 5)
check("按配置阈值划档", score["match_level"], "高匹配")
check_true("明细带上了实际值", score["score_breakdown"][0]["actual_value"] is not None,
           f"breakdown[0]={score['score_breakdown'][0]}")

no_willing = assess(JOB, merged, None)
check("未表态按待人工复核处理", no_willing["final_decision"], "待人工复核")
check("低分且愿意 = 谨慎推进",
      assess({**JOB, "level_threshold": {"high_min": 9, "mid_min": 8}}, merged, True)["final_decision"],
      "谨慎推进")
check("低分且不愿意 = 不推进",
      assess({**JOB, "level_threshold": {"high_min": 9, "mid_min": 8}}, merged, False)["final_decision"],
      "不推进")

print()
print("=" * 70)
print("5. 对话渲染不泄漏内部字段")
print("=" * 70)
rendered = render_resume_tags(merged)
check_true("渲染含专业", "汉语言文学" in rendered)
check_true("渲染含经验标签", "课程顾问" in rendered)
transcript = format_transcript([{"role": "assistant", "content": "你好"},
                                {"role": "user", "content": "接受"}])
check_true("对话记录有角色标签", "HR：你好" in transcript and "候选人：接受" in transcript)
check("空对话记录", format_transcript([]), "（无对话记录）")

print()
print("=" * 70)
print("6. 旁路触发闸门（约束 1）")
print("=" * 70)
for label, text, expected in [
    ("岗位细节深度提问", "请问这个岗位平时晚上一般几点能下班呢？", True),
    ("晋升路径提问", "这个岗位做满一年之后有没有晋升通道？", True),
    ("薪资细节提问", "底薪之外还有没有额外的课时补贴呢？", True),
    ("纯陈述不放行", "我本科刚毕业已经过了英语四级", False),
    ("短句回答不放行", "接受的", False),
    ("反问确认不放行", "好的？", False),
    ("短反问不放行", "能接受吗？", False),
    ("空话不放行", "嗯", False),
    ("无问号的问句表述不放行", "我想了解一下晋升通道", False),
]:
    check(label, _is_deep_question(text), expected)

print()
print("=" * 70)
print("7. 中文匹配器")
print("=" * 70)
check("整词命中", has_trace("课程顾问", "做过课程顾问助理"), True)
check("bigram 兜底（提示性场景）", has_trace("师范院校", "汉语言文学（师范）"), True)
check("单字不做匹配", has_trace("师", "汉语言文学（师范）"), False)
check("无依据", has_trace("客户开发", "汉语言文学（师范）"), False)
check("全角归一化", has_trace("ＣＡＴ", "毕业于 CAT 大学"), True)
check("连词切分", tokenize("授课、代课和主讲", split_conjunctions=True), ["授课", "代课", "主讲"])
check("默认不切连词", tokenize("计算机和软件工程"), ["计算机和软件工程"])
check("批量命中", find_hits(["代课", "客户开发"], "做过代课老师"), ["代课"])

print()
print("=" * 70)
print("8. 打分输入必须高精度：通用片段不得触发经验标签")
print("=" * 70)
# 这组是实测踩到的真实误判：小学教师简历被 bignam 兜底判成有销售经历，
# 白送 3 分。仅靠 "求职意向：学习顾问"、"联系电话"、"小学教育" 三个无关片段。
ADVISORY_TEXT = "求职意向：学习顾问 联系电话：138****2345 专业：小学教育"
# 这三个词靠无关片段被 bigram 兜底捞出来 —— 提示性场景留着它们无害，打分场景必须否掉
for kw in ["课程顾问", "电话销售", "教育培训"]:
    check_true(f"提示性场景仍可命中 {kw}", has_trace(kw, ADVISORY_TEXT, allow_partial=True))
    check_true(f"打分场景不得命中 {kw}",
               not has_trace(kw, ADVISORY_TEXT, allow_partial=False))

# 单字素词（"销售"/"招生"）没有更短的 bigram 可退，两种模式下行为一致 —— 不命中
for kw in ["销售", "招生"]:
    check_true(f"{kw} 两种模式均不命中（无更短词素可退）",
               has_trace(kw, ADVISORY_TEXT, allow_partial=True) is False
               and has_trace(kw, ADVISORY_TEXT, allow_partial=False) is False)

sales_kw = ["销售", "课程顾问", "课程转化", "教培销售", "招生", "续费",
            "客户转化", "电话销售", "教培", "教育培训", "辅导"]
# 整词匹配：命中的是"课程顾问/招生/续费"这三个整词；
# "销售""教培销售"虽被包含在"课程顾问"字串里，但不是独立出现的词，不算。
check("打分区只认整词",
      detect_exp_tags("做过课程顾问助理，负责续费和招生", sales_kw),
      ["课程顾问", "招生", "续费"])
check("教师简历不再被误判为有销售经历",
      detect_exp_tags("求职意向：学习顾问 联系电话：138 小学教育 语文辅导", sales_kw),
      ["辅导"])

print()
print("=" * 70)
print(f"结论：{'全部通过' if not failures else f'{len(failures)} 项失败 -> {failures}'}")
print("=" * 70)
sys.exit(1 if failures else 0)