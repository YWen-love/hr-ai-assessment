"""报告组装层测试。

对应「项目诉求对齐」的四条验证项：
  1. 静态打分不受对话影响
  2. conversation_traits 稳定抽取意向，且不参与算分
  3. 终局结论优先跟随候选人意愿
  4. 报告能带出对话摘要 + 备选岗位推荐

纯本地，不发起任何模型调用。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.job_schema import FINAL_DECISION_MATRIX, normalize_job_config
from app.report import (
    assemble_report,
    build_match_points,
    build_trait_report,
    decide,
    keyword_hints,
    render_report_text,
    should_recommend_alternates,
)
from app.traits import TRAIT_EXTRACT_SYSTEM_PROMPT, empty_traits
from rule_engine import assess, score_objective_layer

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if not ok and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 68}\n{title}\n{'-' * 68}")


JOB = {
    "job_key": "demo_role",
    "job_name": "示例岗位",
    "score_rules": {
        "add_score": [
            {
                "id": "intent_match",
                "field": "resume_intention",
                "op": "contains_any",
                "value": ["销售"],
                "score": 2,
                "desc": "意向一致",
            },
            {
                "id": "exp_match",
                "field": "exp_tags",
                "op": "has_any",
                "value": ["招生"],
                "score": 3,
                "desc": "有相关经历",
            },
        ],
        "minus_score": [],
    },
    "level_threshold": {"high_min": 5, "mid_min": 3},
}

HIGH_TAGS = {
    "resume_intention": "销售",
    "exp_tags": ["招生"],
    "major": "市场营销",
    "degree": "本科",
    "school_type": {"is_985_211": True},
}

TRAITS_FALSE = {
    "conversation_traits": [
        {"category": "avoidance", "content": "排斥销售转化", "evidence": "我不太想做销售转化"},
        {"category": "constraint", "content": "无法接受晚班", "evidence": "晚上有课要带"},
        {"category": "job_preference", "content": "更想做授课类岗位", "evidence": "我喜欢讲台"},
    ],
    "willing_for_target_job": False,
    "check_results": [{"text": "接受晚班", "accepted": False}],
    "reason": "候选人多次明确表示不接受",
}


# ---------------------------------------------------------------------------

def test_static_independent_of_dialogue() -> None:
    section("1. 静态打分不受对话影响")
    base = score_objective_layer(JOB, HIGH_TAGS)
    check("高分画像得满分", base["total_score"] == 5, str(base["total_score"]))
    check("判为高匹配", base["match_level"] == "高匹配", base["match_level"])

    # score_objective_layer 根本不接受对话参数 —— 结构上就无从读取
    import inspect
    params = list(inspect.signature(score_objective_layer).parameters)
    check("函数签名里没有任何对话参数", params == ["job_config", "resume_tags"], str(params))

    # traits 里的内容即使命中打分规则的字面词，也不会渗进静态分
    leaky_tags = {**HIGH_TAGS, "resume_intention": "销售", "exp_tags": ["招生", "晚班"]}
    after = score_objective_layer(JOB, leaky_tags)
    check("简历标签里的任意值不会引入对话层", after["total_score"] == base["total_score"])

    # 意愿为 False 时，静态分与档位仍原样输出，不被改写
    decided = assess(JOB, HIGH_TAGS, False)
    check("意愿 False 时静态分仍为满分", decided["total_score"] == 5, str(decided["total_score"]))
    check("意愿 False 时档位仍是高匹配", decided["match_level"] == "高匹配", decided["match_level"])


def test_traits_not_scored() -> None:
    section("2. conversation_traits 不参与算分")
    # 极端情形：traits 塞进任何东西，静态结果必须一模一样
    huge_traits = {
        "conversation_traits": [
            {"category": "job_preference", "content": "完全对口完美无缺的天才候选人", "evidence": ""}
            for _ in range(50)
        ],
        "willing_for_target_job": True,
    }
    a = assess(JOB, HIGH_TAGS, True)
    b = assess(JOB, HIGH_TAGS, True)
    check("同样输入结果稳定（无隐藏随机项）", a["total_score"] == b["total_score"])

    # 空 traits 与满 traits 的静态分一致
    check(
        "静态分只由 tags 决定",
        score_objective_layer(JOB, HIGH_TAGS)["total_score"]
        == score_objective_layer(JOB, HIGH_TAGS)["total_score"],
    )

    # traits 的 prompt 里不得出现岗位业务词；判决类词只允许出现在「禁止」语境内
    prompt = TRAIT_EXTRACT_SYSTEM_PROMPT
    business_words = [
        "学习顾问", "二讲", "课程顾问", "教培", "教师", "销售", "普通话", "晚班",
        "底薪", "提成", "五险一金",
    ]
    leaked_business = [w for w in business_words if w in prompt]
    check("traits prompt 无岗位业务词", not leaked_business, str(leaked_business))

    decision_words = ["淘汰", "通过", "录用", "推荐", "打分", "档位", "红线"]
    banned_block = prompt.split("【禁止项")[1].split("【conversation_traits")[0]
    outside_ban = [
        w for w in decision_words
        if w in prompt.replace(banned_block, "")
    ]
    check("判决类词只出现在禁止项内", not outside_ban, str(outside_ban))
    check("禁止项确实点名了判决/打分/推荐",
          all(w in banned_block for w in ("判决", "打分", "推荐", "红线")), banned_block[:200])

    check("traits 输出键为 willing_for_target_job", "willing_for_target_job" in prompt)
    check("traits prompt 无岗位名词", "示例岗位" not in prompt)


def test_decision_follows_willing() -> None:
    section("3. 终局结论优先跟随意愿")
    # 核心诉求：静态高匹配 + 明确不愿意 → 不推进
    high_false = assess(JOB, HIGH_TAGS, False)
    check("高匹配 + 不愿意 → 不推进", high_false["final_decision"] == "不推进", high_false["final_decision"])

    low_tags = {"resume_intention": "教师", "exp_tags": [], "major": "汉语言文学", "degree": "本科"}
    low_false = assess(JOB, low_tags, False)
    check("低匹配 + 不愿意 → 不推进", low_false["final_decision"] == "不推进")

    low_true = assess(JOB, low_tags, True)
    check("低匹配 + 愿意 → 谨慎推进", low_true["final_decision"] == "谨慎推进", low_true["final_decision"])

    high_true = assess(JOB, HIGH_TAGS, True)
    check("高匹配 + 愿意 → 推进", high_true["final_decision"] == "推进")

    # 未表态 → 待人工复核，且与静态档位无关
    for level, tags in (("高匹配", HIGH_TAGS), ("低匹配", low_tags)):
        unknown = assess(JOB, tags, None)
        check(
            f"{level} + 未表态 → 待人工复核",
            unknown["final_decision"] == "待人工复核",
            unknown["final_decision"],
        )
    check("未表态的依据写明不自动判定",
          "交人工复核" in decide("高匹配", None)["decision_basis"],
          decide("高匹配", None)["decision_basis"])
    check("未表态时意愿文本为态度不明确",
          decide("高匹配", None)["willing_text"] == "态度不明确")
    check("未表态与明确拒绝是两回事",
          decide("高匹配", None)["final_decision"] != decide("高匹配", False)["final_decision"])

    # decide() 必须与 job_schema 的终局矩阵一致 —— 矩阵是唯一定义源。
# 这条断言是防漂移的：有人只改矩阵不改 decide（或反之）时立刻炸。
# 只覆盖 level_for_score 真实能产出的三档；未知档位是刻意的例外，另测。
    for level in ("高匹配", "中匹配", "低匹配"):
        for willing in (True, False):
            expected = FINAL_DECISION_MATRIX[(level, willing)]
            check(
                f"decide 与矩阵一致：{level} × 意愿={willing}",
                decide(level, willing)["final_decision"] == expected,
                f'{decide(level, willing)["final_decision"]} != {expected}',
            )

    # 未知档位不得静默判掉：候选人说了愿意，就不能因配置 bug 推给「不推进」
    unknown_level = decide("未知档位", True)
    check("未知档位 + 愿意 → 谨慎推进（不静默判掉）",
          unknown_level["final_decision"] == "谨慎推进", unknown_level["final_decision"])
    check("未知档位的依据点名了 level_threshold",
          "level_threshold" in unknown_level["decision_basis"], unknown_level["decision_basis"])
    check("未知档位 + 不愿意 → 仍是不推进",
          decide("未知档位", False)["final_decision"] == "不推进")

    # 意愿 False 时，静态档位不影响结论
    for level, tags in (("高匹配", HIGH_TAGS), ("低匹配", low_tags)):
        check(f"{level} + 不愿意 → 一律不推进", assess(JOB, tags, False)["final_decision"] == "不推进")

    basis = decide("高匹配", False)["decision_basis"]
    check("不推进的依据说明跟随主观意愿", "主观意愿" in basis, basis)


def test_report_content() -> None:
    section("4. 报告带出对话摘要 + 备选岗位推荐")
    objective = assess(JOB, HIGH_TAGS, False)
    roles = [
        {
            "backup_job_key": "teacher_alt",
            "backup_job_name": "授课类备选岗",
            "match_condition": ["喜欢讲课", "有授课经历"],
            "reason": "候选人明确偏好授课方向",
            "matched": True,
        }
    ]
    report = assemble_report(
        job_config=JOB,
        objective=objective,
        traits=TRAITS_FALSE,
        alternate_roles=roles,
        candidate_name="测试候选人",
    )

    check("报告带静态层", report["objective_layer"]["match_level"] == "高匹配")
    check("报告带主观层原始结构", report["conversation_traits"] is TRAITS_FALSE)
    check("终局跟随意愿", report["final_decision"] == "不推进", report["final_decision"])
    check("意愿文本可读", report["willing_text"] == "明确不接受", report["willing_text"])

    trait_report = report["trait_report"]
    check("摘要非空", bool(trait_report["summary"]), trait_report["summary"])
    check("偏好方向被单独归类", len(trait_report["preferences"]) == 1, str(trait_report["preferences"]))
    check("排斥事项被单独归类", len(trait_report["avoidances"]) == 1)
    check("客观限制被单独归类", len(trait_report["constraints"]) == 1)
    check("佐证原文保留", trait_report["avoidances"][0]["evidence"] == "我不太想做销售转化")

    check("带出备选岗位", len(report["alternate_roles"]) == 1)
    check("备选岗位有名称", report["alternate_roles"][0]["backup_job_name"] == "授课类备选岗")

    text = render_report_text(report)
    print()
    for line in text.splitlines():
        print(f"    {line}")
    print()
    for needed in ("静态匹配：高匹配", "对话意向：明确不接受", "终局判定：不推进", "备选岗位推荐"):
        check(f"文本报告含「{needed}」", needed in text)
    check("文本报告含原文佐证", "我不太想做销售转化" in text)

    # 空 traits（抽取失败，willing=None）也不能崩，且应落到待人工复核
    empty = assemble_report(job_config=JOB, objective=objective, traits=empty_traits())
    check("空 traits 可组装", empty["final_decision"] == "待人工复核", empty["final_decision"])
    check("空 traits 无匹配岗位", empty["alternate_roles"] == [])
    check("空 traits 文本可渲染", isinstance(render_report_text(empty), str))
    check("空 traits 文本说明未配置映射",
          "未配置" in render_report_text(empty), render_report_text(empty)[-120:])


def test_recommend_gate() -> None:
    section("5. 备选推荐门控：只有明确愿意主岗时不推")

    # 业务定稿的三条
    show_false, reason_false = should_recommend_alternates(False)
    check("willing=False → 推荐", show_false is True, reason_false)
    show_true, reason_true = should_recommend_alternates(True)
    check("willing=True → 不推荐", show_true is False, reason_true)
    show_none, reason_none = should_recommend_alternates(None)
    check("willing=None → 仍推荐适配岗位（供人工复核）", show_none is True, reason_none)
    check("未表态的理由写明是人工复核", "人工复核" in reason_none, reason_none)

    # 取反的旧 bug：旧实现是「愿意主岗才展示」
    check(
        "旧逻辑的反面不再成立：愿意主岗不推备选",
        should_recommend_alternates(True, {"when_reject_main_job": True})[0] is False,
    )
    check(
        "明确拒绝主岗照推备选",
        should_recommend_alternates(False, {"when_reject_main_job": True})[0] is True,
    )
    check(
        "总闸开着时未表态也推适配岗位",
        should_recommend_alternates(None, {"when_reject_main_job": True})[0] is True,
    )
    # 三种意愿里，只有 True 不推
    not_recommended = [
        willing for willing in (True, False, None)
        if not should_recommend_alternates(willing, {"when_reject_main_job": True})[0]
    ]
    check("只有 willing=True 不推备选", not_recommended == [True], str(not_recommended))

    # 总闸：配置关掉就完全不推，且理由要指向配置
    off, off_reason = should_recommend_alternates(False, {"when_reject_main_job": False})
    check("总闸关闭时即使拒绝主岗也不推", off is False, off_reason)
    check("总闸关闭的理由指向配置", "backup_show_switch" in off_reason, off_reason)
    check("未传 switch 时按默认推", should_recommend_alternates(False, None)[0] is True)

    # ---- 端到端：报告里 alternate_roles 是否真的随意愿变 ----
    roles = [{
        "backup_job_key": "alt_role",
        "backup_job_name": "授课类备选岗",
        "match_condition": ["喜欢讲课"],
        "reason": "偏好授课",
    }]
    job_with_switch = {**JOB, "backup_show_switch": {"when_reject_main_job": True}}
    objective = assess(JOB, HIGH_TAGS, False)

    r_refuse = assemble_report(job_config=job_with_switch, objective=objective,
                               traits=TRAITS_FALSE, alternate_roles=roles)
    r_accept = assemble_report(job_config=job_with_switch, objective=objective,
                               traits={**TRAITS_FALSE, "willing_for_target_job": True},
                               alternate_roles=roles)
    r_unknown = assemble_report(job_config=job_with_switch, objective=objective,
                                traits={**TRAITS_FALSE, "willing_for_target_job": None},
                                alternate_roles=roles)

    check("拒绝主岗 → 报告带出备选", len(r_refuse["alternate_roles"]) == 1, str(r_refuse["alternate_roles"]))
    check("愿意主岗 → 报告不带备选", r_accept["alternate_roles"] == [])
    check("未表态 → 报告仍带出适配岗位（供人工复核）",
          len(r_unknown["alternate_roles"]) == 1, str(r_unknown["alternate_roles"]))
    check("未表态时结论为待人工复核",
          r_unknown["final_decision"] == "待人工复核", r_unknown["final_decision"])

    # 命中项始终保留，HR 才能复核"为什么当时没推"
    for name, report in (("拒绝", r_refuse), ("愿意", r_accept), ("未表态", r_unknown)):
        check(f"{name}情形保留 matched_roles 供复核", len(report["matched_roles"]) == 1)

    check("推荐理由随意愿变化", r_refuse["alternate_roles_reason"] != r_accept["alternate_roles_reason"])

    # 文本报告要写清"有备选但被压住"和"本来就没配"的区别
    text_accept = render_report_text(r_accept)
    check("愿意主岗时文本说明不推的原因", "不推备选岗位" in text_accept, text_accept[-200:])
    check("文本不含备选岗位名", "授课类备选岗" not in text_accept)

    text_refuse = render_report_text(r_refuse)
    check("拒绝主岗时文本列出备选", "授课类备选岗" in text_refuse)

    # 待人工复核的报告，适配岗位标题要说明是给人判断用的，不是结论
    text_unknown = render_report_text(r_unknown)
    check("待复核时文本标题为适配岗位（供人工复核参考）",
          "适配岗位（供人工复核参考）" in text_unknown, text_unknown[-200:])
    check("待复核时文本含终局判定：待人工复核",
          "终局判定：待人工复核" in text_unknown, text_unknown[-200:])
    check("待复核时文本含适配岗位名", "授课类备选岗" in text_unknown)
    check("明确拒绝时标题仍为备选岗位推荐",
          "备选岗位推荐：" in text_refuse, text_refuse[-200:])

    no_mapping = assemble_report(job_config=JOB, objective=objective, traits=TRAITS_FALSE,
                                 alternate_roles=[])
    check("未配置映射时文本说明原因",
          "未配置" in render_report_text(no_mapping), render_report_text(no_mapping)[-120:])


def test_switch_normalization() -> None:
    section("6. backup_show_switch 规范化与旧字段迁移")
    # 旧配置带 when_accept_main_job=false：语义相反，必须丢弃而不是取反
    legacy = normalize_job_config({
        "job_key": "x", "job_name": "X",
        "backup_show_switch": {"when_accept_main_job": False},
    })
    check("旧字段被丢弃", "when_accept_main_job" not in legacy["backup_show_switch"],
          str(legacy["backup_show_switch"]))
    check("默认开启拒绝主岗时的推荐",
          legacy["backup_show_switch"]["when_reject_main_job"] is True,
          str(legacy["backup_show_switch"]))
    # 丢弃而非取反：旧 false → 新 true，而不是旧 false → 新 false
    check("旧 false 不被取反成 false",
          should_recommend_alternates(False, legacy["backup_show_switch"])[0] is True)

    # 显式关闭必须被尊重
    explicit = normalize_job_config({
        "job_key": "x", "job_name": "X",
        "backup_show_switch": {"when_reject_main_job": False},
    })
    check("显式 false 被保留",
          should_recommend_alternates(False, explicit["backup_show_switch"])[0] is False)

    # 完全没写 switch 时给默认值
    missing = normalize_job_config({"job_key": "x", "job_name": "X"})
    check("缺省时默认 true",
          missing["backup_show_switch"]["when_reject_main_job"] is True,
          str(missing["backup_show_switch"]))

    # 规范化不应改动调用方传入的 dict
    source = {"job_key": "x", "job_name": "X",
              "backup_show_switch": {"when_accept_main_job": False}}
    normalize_job_config(source)
    check("未污染入参", "when_accept_main_job" in source["backup_show_switch"], str(source))


def test_keyword_hints() -> None:
    section("8. 备选映射的字面佐证")
    ctx = {
        "major": "汉语言文学（师范）",
        "resume_intention": "教师/授课",
        "exp_tags": ["代课", "教培授课"],
        "trait_summary": "倾向岗位/职业方向：更想做授课类岗位",
    }
    check("师范背景被识别", keyword_hints("师范院校或师范类专业", ctx) == ["师范院校", "师范类专业"])
    check("代课经历被识别", "代课" in keyword_hints("实习经历为授课、代课、主讲", ctx))
    check("无关条件无佐证", keyword_hints("实习经历为客户开发、商务谈判", ctx) == [])
    check("空条件返回空", keyword_hints("", ctx) == [])


def test_match_points() -> None:
    section("9. 适配岗位逐条列举匹配点")
    ctx = {"major": "汉语言文学（师范）", "exp_tags": ["代课"], "trait_summary": ""}
    conditions = ["师范院校或师范类专业", "有授课、代课经历", "接受排班"]

    hit = {
        "backup_job_name": "授课类备选岗",
        "match_condition": conditions,
        "condition_hints": [
            {"condition": "师范院校或师范类专业", "keywords": ["师范院校"]},
            {"condition": "有授课、代课经历", "keywords": ["代课"]},
            {"condition": "接受排班", "keywords": []},
        ],
        # 模型第三条只说"信息不足"，第一条标点与配置不完全一致
        "match_points": [
            {"condition": "师范院校或师范类专业背景。", "hit": True, "evidence": "major=汉语言文学（师范）"},
            {"condition": "有授课、代课经历", "hit": False, "evidence": "仅有代课，无授课"},
        ],
    }
    points = build_match_points(hit, ctx)

    check("每条命中条件都出一条匹配点，不合并",
          [p["condition"] for p in points] == conditions, str([p["condition"] for p in points]))
    check("模型判满足 -> hit", points[0]["status"] == "hit", points[0]["mark"])
    check("满足项带模型依据", points[0]["evidence"] == "major=汉语言文学（师范）")
    check("模型判不满足 -> miss", points[1]["status"] == "miss", points[1]["mark"])
    check("模型漏判但有字面线索 -> hint",
          points[2]["status"] == "unknown", points[2]["mark"])

    # 模型漏判的那条：有 condition_hints 就标"有线索"，而不是"未见依据"
    hit_no_judgement = {**hit, "match_points": []}
    loose = build_match_points(hit_no_judgement, ctx)
    check("无逐条判断时按字面线索分级",
          [p["status"] for p in loose] == ["hint", "hint", "unknown"],
          str([(p["condition"], p["status"]) for p in loose]))
    check("字面线索写入 keywords", loose[0]["keywords"] == ["师范院校"], str(loose[0]))

    # 标点不一致不能造成重复条目
    check("模型改标点不产生重复条目", len(loose) == len(conditions))

    # 模型凭空多报的条件不进报告：条件清单以 job-config 为准
    hit_extra = {**hit, "match_points": hit["match_points"] + [
        {"condition": "有教师资格证", "hit": True, "evidence": "编的"}]}
    check("模型多报的条件不进入匹配点",
          "有教师资格证" not in [p["condition"] for p in build_match_points(hit_extra, ctx)])

    # 没有条件也没有模型判断 -> 诚实返回空，由渲染层显式说明
    check("既无配置条件也无模型判断时返回空",
          build_match_points({"backup_job_name": "x"}, ctx) == [])

    # 上游没预计算 condition_hints 时，代码要能自己兜底
    no_hints = {**hit, "condition_hints": [], "match_points": []}
    check("缺 condition_hints 时代码自行兜底",
          build_match_points(no_hints, ctx)[0]["status"] == "hint")

    report = assemble_report(
        job_config=JOB,
        objective=assess(JOB, HIGH_TAGS, False),
        traits=TRAITS_FALSE,
        alternate_roles=[hit],
    )
    text = render_report_text(report)
    print()
    for line in text.splitlines():
        print(f"    {line}")
    print()
    for needed in ("匹配点：", "√ 满足 师范院校或师范类专业",
                   "× 不满足 有授课、代课经历", "· 未见依据 接受排班",
                   "字面佐证词：代课"):
        check(f"文本报告含「{needed}」", needed in text)
    check("匹配点区块里不出现佐证词总数堆在末尾",
          text.count("匹配点") >= 1 and "（佐证词：" not in text, text[-300:])

    # 无匹配点时渲染层必须说明，不能静默只剩一句理由
    bare = assemble_report(job_config=JOB, objective=assess(JOB, HIGH_TAGS, False),
                           traits=TRAITS_FALSE,
                           alternate_roles=[{"backup_job_name": "裸岗", "reason": "试试看"}])
    check("无匹配点时文本明说没有逐条匹配点",
          "无逐条匹配点" in render_report_text(bare), render_report_text(bare)[-200:])


def test_build_trait_report_categories() -> None:
    section("7. trait_report 分类完整性")
    built = build_trait_report(
        {
            "conversation_traits": [
                {"category": "job_preference", "content": "a", "evidence": "e1"},
                {"category": "avoidance", "content": "b", "evidence": "e2"},
                {"category": "constraint", "content": "c", "evidence": "e3"},
                {"category": "work_style", "content": "d", "evidence": "e4"},
                {"category": "motivation", "content": "e", "evidence": "e5"},
                {"category": "personality_fit", "content": "f", "evidence": "e6"},
                {"category": "unknown_category", "content": "丢弃", "evidence": ""},
            ],
            "check_results": [],
            "reason": "r",
        }
    )
    total = sum(len(built[k]) for k in ("preferences", "avoidances", "constraints"))
    check("六类偏好全部被覆盖（避免ance 单列，其余归偏好/限制）", total == 6, str(total))
    check("非法分类被丢弃", all(e["content"] != "丢弃" for e in built["preferences"]))
    check("每条都带中文标签", all(e["category_label"] for e in built["preferences"]))


def main() -> int:
    print("=" * 68)
    print("报告组装层：静态分 / 意愿 / 推荐")
    print("=" * 68)
    for test in (
        test_static_independent_of_dialogue,
        test_traits_not_scored,
        test_decision_follows_willing,
        test_report_content,
        test_recommend_gate,
        test_switch_normalization,
        test_keyword_hints,
        test_match_points,
        test_build_trait_report_categories,
    ):
        test()
    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  - {name}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())