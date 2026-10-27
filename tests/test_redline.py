"""红线校验与主线协议测试。

这是安全关键模块：一旦失效，候选人就会看到不该看到的内部信息。
测试覆盖协议剥离、禁词拦截、兜底话术三条路径。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.redline import (
    build_forbidden_terms,
    find_forbidden_terms,
    parse_chat_message,
    rag_answer_is_offline,
    sanitize_candidate_text,
)

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL}  {label}: 实际={actual!r} 期望={expected!r}")
    if not ok:
        failures.append(label)


JOB = {
    "job_key": "education_learning_consultant",
    "job_name": "初中学习顾问",
    "alternate_role_mapping": [
        {"backup_job_key": "education_erjiang", "backup_job_name": "初中二讲老师"},
    ],
}
TERMS = build_forbidden_terms(JOB)

print("=" * 68)
print("1. 禁词表只收备选岗，主岗名绝不能进禁词表")
print("=" * 68)
check("禁词表", TERMS, ["初中二讲老师", "education_erjiang"])
check("主岗名不在禁词表", "初中学习顾问" in TERMS, False)
check("空配置返回空表", build_forbidden_terms(None), [])
check("无备选岗返回空表", build_forbidden_terms({"alternate_role_mapping": []}), [])

print()
print("=" * 68)
print("2. [NEED_RAG] 标记捕获，且内容绝不下发给候选人")
print("=" * 68)
msg = parse_chat_message(
    "可以的，那我们先聊到这。\n[NEED_RAG]这个岗位的晋升空间大不大？", forbidden_terms=TERMS
)
check("下发给候选人的正文", msg.text, "可以的，那我们先聊到这。")
check("捕获到 RAG 问题", msg.need_rag_question, "这个岗位的晋升空间大不大？")
check("正文不含标记", "NEED_RAG" in msg.text, False)
check("未触发拦截", msg.was_blocked, False)

print()
print("=" * 68)
print("3. [DONE] 结束标记")
print("=" * 68)
msg = parse_chat_message(
    "好的，很高兴你可以接受，后续会有人跟你电话联系。\n[DONE]", forbidden_terms=TERMS
)
check("DONE 被识别", msg.is_done, True)
check("正文已剥离标记", msg.text, "好的，很高兴你可以接受，后续会有人跟你电话联系。")

print()
print("=" * 68)
print("4. 备选岗名泄漏必须被拦截")
print("=" * 68)
for bad in [
    "不过我觉得你更适合去做初中二讲老师，要不我帮你问问那边？",
    "我们还有一个岗位 education_erjiang 你可以考虑。",
    "要不试试换个方向吧，我们这儿别的岗位也缺人。",
    "你可以转岗到我们内容部去。",
]:
    msg = parse_chat_message(bad, forbidden_terms=TERMS)
    print(f"{PASS if msg.was_blocked else FAIL}  拦截: {bad[:24]}... -> 命中 {msg.blocked_terms}")
    if not msg.was_blocked:
        failures.append(f"未拦截: {bad}")

print()
print("=" * 68)
print("5. 正常内容不得被误伤")
print("=" * 68)
for good in [
    "好的，那这个岗位的底薪大概是一个什么水平呢？",
    "周三到周日上班我大概能接受，不过周末想有一天完全休息。",
    "我之前做过半年教育机构的课程顾问，把体验课转化成了正价课。",
    "我们这个岗位不负责招生引流，只做存量学员的转化。",
]:
    msg = parse_chat_message(good, forbidden_terms=TERMS)
    ok = not msg.was_blocked and msg.text == good
    print(f"{PASS if ok else FAIL}  未误伤: {good[:24]}...")
    if not ok:
        failures.append(f"误伤: {good}")

print()
print("=" * 68)
print("6. 候选人侧最终清洗：宁可兜底，不给残句")
print("=" * 68)
check("干净文本原样返回",
      sanitize_candidate_text("你的测评已经完成啦。", forbidden_terms=TERMS), "你的测评已经完成啦。")
check("命中红线时返回兜底话术",
      sanitize_candidate_text("我觉得你更适合初中二讲老师。", forbidden_terms=TERMS).startswith("好的，我这边先记录下来"), True)
check("空文本返回兜底话术", sanitize_candidate_text("   ", forbidden_terms=TERMS).startswith("好的，我这边先记录下来"), True)
check("漏进来的标记被剥掉",
      sanitize_candidate_text("好的。\n[NEED_RAG]转岗吗", forbidden_terms=TERMS), "好的。")

print()
print("=" * 68)
print("7. RAG 旁路回答不得反向污染主线")
print("=" * 68)
check("正常答疑放行",
      rag_answer_is_offline("这个岗位的晋升路径是：课程顾问→主管→区域经理。", forbidden_terms=TERMS), False)
check("含备选岗的答疑拦截",
      rag_answer_is_offline("其实你更适配初中二讲老师。", forbidden_terms=TERMS), True)
check("含评分的答疑拦截",
      rag_answer_is_offline("你这次测评是高匹配档位。", forbidden_terms=TERMS), True)
check("含转岗建议的答疑拦截",
      rag_answer_is_offline("我建议你转岗来做二讲。", forbidden_terms=TERMS), True)
check("空回答拦截", rag_answer_is_offline("", forbidden_terms=TERMS), True)

# 提示词里原有的「不要提及分数/测评结果/岗位匹配程度」已改由代码兜底，
# 这里锁住代码闸真的能接住这几类 —— 提示词删了，闸不能跟着变松。
check("提到匹配度拦截",
      rag_answer_is_offline("你匹配度有 85%，很适合这个岗位。", forbidden_terms=TERMS), True)
check("提到测评结果拦截",
      rag_answer_is_offline("你的测评结果已经出来了，档位是高匹配。", forbidden_terms=TERMS), True)
check("告知候选人自己的分数拦截",
      rag_answer_is_offline("你的分数是 78 分。", forbidden_terms=TERMS), True)
check("「您的得分」这种客套说法也拦",
      rag_answer_is_offline("您的得分达到了 92 分，可以进入下一轮。", forbidden_terms=TERMS), True)
check("岗位介绍里提到薪资档位不拦（不是评估结论）",
      rag_answer_is_offline("这个岗位的薪资档位按经验分三档，详情可以面聊。", forbidden_terms=TERMS), False)
check("教育类知识库里的「中考分数」不拦（不是候选人自己的评估）",
      rag_answer_is_offline("我们主要服务中考分数 500 分左右的学生。", forbidden_terms=TERMS), False)

print()
print("=" * 68)
print(f"结论：{'全部通过' if not failures else f'{len(failures)} 项失败 -> {failures}'}")
print("=" * 68)
sys.exit(1 if failures else 0)