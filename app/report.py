"""测评报告组装层。

按「项目诉求对齐」的分层约定，这一层是**唯一**做决策和推荐的地方：

    静态打分（rule_engine）  ─┐
    conversation_traits(traits) ─┼─> assemble_report() ─> 报告
    job-config（映射规则）      ─┘

- 静态分只由简历标签 + job-config 的 DSL 规则算出，**不读对话**
- conversation_traits 只提供候选人的意愿与偏好，**不参与算分**
- 终局结论优先跟随 willing_for_target_job
- 备选岗位推荐读 job-config 的 alternate_role_mapping，业务映射全在配置里

本模块不含任何岗位名词。新增岗位只改 job-config。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.job_schema import FINAL_DECISION_MATRIX, final_decision
from app.text_match import find_hits, tokenize
from app.traits import TRAIT_CATEGORIES, trait_summary

# willing 为 None（对话没表态）时的结论。
#
# 这里刻意不用「不推进」：候选人是没说清楚，不是说了不。两者混为一谈，
# HR 会把一批「其实可以聊」的人当成拒绝，漏掉的可能性比误推高得多。
# 判不出结论时如实说判不出，人才是唯一诚实的输出。
_WILLING_UNKNOWN_DECISION = "待人工复核"


def should_recommend_alternates(
    willing_for_target_job: Optional[bool],
    switch: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """备选岗位要不要输出给 HR。返回 (是否推荐, 判定理由)。

    业务定稿：

      willing=False  → 推荐。已经拒绝主岗，这时候不给去处等于把人晾着
      willing=True   → 不推荐。主岗谈成了还推别的，是在制造内部竞争
      willing=None   → 推荐。结论是「待人工复核」，HR 正是靠这份适配岗位
                       来判断的；不推的话，复核就没有依据，只能重聊一遍

    也就是说：只有候选人明确愿意主岗时才不推。其余两种"人还没进主岗"的
    情况都要给出适配方向。

    switch（job-config 的 backup_show_switch）只当总闸用：
    `when_reject_main_job=false` 可让某岗位完全不推备选。默认 true。
    """
    if switch is not None and not switch.get("when_reject_main_job", True):
        return False, "岗位配置 backup_show_switch.when_reject_main_job=false，该岗位不推备选岗位"

    if willing_for_target_job is True:
        return False, "候选人愿意主岗位，不推备选岗位"
    if willing_for_target_job is False:
        return True, "候选人明确拒绝主岗位，推送备选岗位"
    return True, "候选人态度不明确、结论为待人工复核，附适配岗位供 HR 判断"


def decide(
    match_level: str,
    willing_for_target_job: Optional[bool],
    *,
    score_detail: str = "",
) -> Dict[str, Any]:
    """终局推进判定。

    规则（定稿）：
      候选人明确不愿意 → 不推进本岗位（与静态档位无关）
      候选人态度不明确 → 待人工复核（本档位与静态分无关）
      候选人愿意       → 结合静态档位给 HR 参考

    静态档位只在「愿意」这一支起作用。它不参与意愿的判定 ——
    简历再好看也改不了候选人自己说的「我不想做销售」；
    同样，候选人只是没说清楚时，简历再好看也不该替他做决定。
    """
    if willing_for_target_job is None:
        return {
            "final_decision": _WILLING_UNKNOWN_DECISION,
            "decision_basis": (
                "对话中候选人未明确表态，不自动判定推进或拒绝，交人工复核"
                f"（静态档位 {match_level} 仅作参考，不参与本结论）"
            ),
            "willing_text": _willing_text(willing_for_target_job),
            "score_detail": score_detail,
        }

    if willing_for_target_job is False:
        return {
            "final_decision": final_decision(match_level, False),
            "decision_basis": "对话中候选人明确表达不接受，终局判定优先跟随主观意愿",
            "willing_text": _willing_text(willing_for_target_job),
            "score_detail": score_detail,
        }

    # 愿意这一支才让静态档位参与。矩阵定义在 job_schema，这里不另抄一份 ——
    # 抄第二份就等于埋了一个「矩阵说 A、这里说 B」的静默漂移点。
    if (match_level, True) not in FINAL_DECISION_MATRIX:
        # 档位不在矩阵里 = 配置有问题（level_for_score 只可能产出三档）。
        # 不能跟着矩阵的保守默认返回「不推进」—— 候选人明明说了愿意，
        # 却因为一个配置 bug 被判掉，那比误推进更糟。改为明确提示人工确认。
        return {
            "final_decision": "谨慎推进",
            "decision_basis": (
                f"候选人愿意推进，但静态档位为「{match_level}」，不在终局矩阵内，"
                f"请人工确认岗位配置的 level_threshold"
            ),
            "willing_text": _willing_text(willing_for_target_job),
            "score_detail": score_detail,
        }

    decision = final_decision(match_level, True)
    return {
        "final_decision": decision,
        "decision_basis": (
            f"候选人愿意推进（{match_level}，静态分仅作辅助参考）"
            if match_level in ("高匹配", "中匹配")
            else f"候选人愿意推进，但静态匹配仅 {match_level}，建议人工确认"
        ),
        "willing_text": _willing_text(willing_for_target_job),
        "score_detail": score_detail,
    }


def _willing_text(willing: Optional[bool]) -> str:
    if willing is True:
        return "愿意推进"
    if willing is False:
        return "明确不接受"
    return "态度不明确"


def build_trait_report(traits: Dict[str, Any]) -> Dict[str, Any]:
    """把 conversation_traits 摊成报告里可读的四块：偏好、排斥、客观限制、佐证。"""
    items = traits.get("conversation_traits") or []
    return {
        "summary": trait_summary(items),
        "preferences": _pick(items, ("job_preference", "motivation", "personality_fit", "work_style")),
        "avoidances": _pick(items, ("avoidance",)),
        "constraints": _pick(items, ("constraint",)),
        "check_results": traits.get("check_results") or [],
        "reason": traits.get("reason", ""),
    }


def _pick(items: List[Dict[str, Any]], categories) -> List[Dict[str, str]]:
    """按分类挑条目，并带上原文佐证。"""
    wanted = set(categories)
    return [
        {
            "category": item.get("category", ""),
            "category_label": TRAIT_CATEGORIES.get(item.get("category", ""), ""),
            "content": item.get("content", ""),
            "evidence": item.get("evidence", ""),
        }
        for item in items
        if isinstance(item, dict) and item.get("category") in wanted
    ]


def keyword_hints(condition: str, context: Dict[str, Any]) -> List[str]:
    """从映射条件里挑出在候选人画像中出现过的词，作为佐证附给模型。

    **这不是命中判定。** 备选推荐是语义判断，最终由模型基于简历标签 +
    对话偏好下结论。这里只把「有字面证据的词」挑出来，帮模型在信息稀疏时定位线索。

    两个已踩过的坑，都已在此规避：
    1. 曾在下游做过硬性粗筛（无字面命中即判不匹配），会因措辞差异大量漏判 —— 已废弃。
    2. 中文复合词整词匹配无效："师范院校" 对上"汉语言文学（师范）" 会漏。
       故退化为 bigram（2 字滑窗）匹配，无需分词器即可命中核心词素。

    放在本层而不是 traits.py：它消费的是 job-config 的映射条件，
    属于推荐路径，不属于 traits 抽取路径。traits 是固定组件，不碰。
    """
    tokens = tokenize(condition, split_conjunctions=True)
    if not tokens:
        return []

    haystack = " ".join(
        str(value)
        for value in (
            context.get("resume_summary", ""),
            context.get("major", ""),
            context.get("resume_intention", ""),
            context.get("degree", ""),
            " ".join(context.get("exp_tags", []) or []),
            context.get("trait_summary", ""),
        )
    )
    return find_hits(tokens, haystack)


# 匹配点的四种状态。
#
# 关键是把「模型判为满足」和「只捞到字面线索」分开标。HR 复核时看的是证据
# 强度，两者混成一句「符合」就等于给了一个说不清来源的结论 —— 而备选岗恰好是
# 用来决定人往哪走的，糊不得。宁可显示"未见依据"，也不能显示"看起来匹配"。
_MATCH_MARKS = {
    "hit": "√ 满足",
    "hint": "~ 有线索",
    "miss": "× 不满足",
    "unknown": "· 未见依据",
}


def _normalize_condition(text: Any) -> str:
    """条件文本归一化，用来把模型回填的条件对上配置里的原文。

    模型照抄条件时经常差一个标点或空格。直接字符串比较会把同一条条件
    判成两条，报告里就冒出重复条目；反过来漏配，则该显示的匹配点整条消失。
    """
    drop = "；;，,。.、（）()【】[]「」\"'!！?？:： "
    return "".join(ch for ch in str(text) if ch not in drop)


def _lookup(points: Dict[str, Any], condition: str) -> Optional[Dict[str, Any]]:
    key = _normalize_condition(condition)
    if key in points:
        return points[key]
    for norm, point in points.items():
        if key and norm and (key in norm or norm in key):
            return point
    return None


def build_match_points(
    hit: Dict[str, Any],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """把一条备选岗位命中，逐条展开成 HR 能直接核对的匹配点。

    每个 `match_condition` 都出一条，不合并、不省略 —— HR 要看的是
    "四条里到底中了几条"，不是模型总结的一句话。命中四条和命中一条
    在结论上是两回事，糊成一句「相关经验较强」就分不出来了。

    状态取自两处，优先级模型高于代码：
      - 模型的逐条判断（match_points，语义判断，是准的）
      - 代码的字面佐证（condition_hints，仅在模型没给逐条结论时兜底）

    模型没逐条判断、代码又捞不到词，才落到「未见依据」。这不等于不匹配，
    只等于没有依据 —— 报告里就这么写，不替模型补，也不替 HR 下结论。
    """
    model_points: Dict[str, Any] = {}
    for point in hit.get("match_points") or []:
        if isinstance(point, dict) and point.get("condition"):
            model_points.setdefault(_normalize_condition(point["condition"]), point)

    hint_index: Dict[str, List[str]] = {}
    for entry in hit.get("condition_hints") or []:
        if isinstance(entry, dict) and entry.get("condition"):
            hint_index.setdefault(
                _normalize_condition(entry["condition"]),
                [str(k) for k in (entry.get("keywords") or [])],
            )

    conditions = [str(c) for c in (hit.get("match_condition") or []) if str(c).strip()]
    if not conditions:
        # 配置里没写条件（老配置）：模型给了什么就列什么，不凭空造条目
        conditions = [str(p["condition"]) for p in model_points.values() if p.get("condition")]
    if not conditions:
        return []

    points = []
    for condition in conditions:
        point = _lookup(model_points, condition)
        key = _normalize_condition(condition)
        keywords = hint_index.get(key)
        if keywords is None:
            # 上游没预计算时兜底重算一遍，保证调用方不依赖字段齐全
            keywords = keyword_hints(condition, context)

        if point is not None and point.get("hit") is True:
            status = "hit"
        elif point is not None:
            status = "miss"
        elif keywords:
            status = "hint"
        else:
            status = "unknown"

        evidence = str(point.get("evidence") or "") if point is not None else ""
        points.append(
            {
                "condition": condition,
                "status": status,
                "mark": _MATCH_MARKS[status],
                "evidence": evidence[:200],
                "keywords": keywords,
            }
        )
    return points


def annotate_alternate_roles(
    hits: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """给模型判出的备选岗位补上字面佐证词和逐条匹配点，供 HR 复核。

    模型已给出 hit/reason，这里只做补充与展开，不改变匹配结论。
    """
    annotated = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        points = build_match_points(hit, context)
        annotated.append(
            {
                **hit,
                "match_points": points,
                "evidence_keywords": [
                    token for point in points for token in (point.get("keywords") or [])
                ],
            }
        )
    return annotated


def assemble_report(
    *,
    job_config: Dict[str, Any],
    objective: Dict[str, Any],
    traits: Dict[str, Any],
    alternate_roles: Optional[List[Dict[str, Any]]] = None,
    candidate_name: str = "",
) -> Dict[str, Any]:
    """组装最终测评报告：静态结论 + 意愿 + 对话摘要 + 备选岗位推荐。

    `alternate_roles` 传**全部命中项**，是否输出由本函数按 should_recommend_alternates 判定。
    调用方不要预先过滤 —— 判定逻辑只有一个实现点，调用方各判一次迟早判出两个结果。
    """
    job_name = job_config.get("job_name", "")
    match_level = objective.get("match_level", "")
    willing = traits.get("willing_for_target_job")

    decision = decide(
        match_level,
        willing,
        score_detail=f"{match_level}（{objective.get('total_score', 0)} 分）",
    )

    all_roles = annotate_alternate_roles(alternate_roles or [], {
        "resume_summary": objective.get("resume_summary", ""),
        "major": (objective.get("resume_tags") or {}).get("major", ""),
        "resume_intention": (objective.get("resume_tags") or {}).get("resume_intention", ""),
        "degree": (objective.get("resume_tags") or {}).get("degree", ""),
        "exp_tags": (objective.get("resume_tags") or {}).get("exp_tags", []) or [],
        "trait_summary": build_trait_report(traits)["summary"],
    })
    show_roles, roles_reason = should_recommend_alternates(
        willing, job_config.get("backup_show_switch")
    )

    score_detail = "；".join(
        f"{row.get('direction', '')}{row.get('score', 0)}分·{row.get('desc', '')}"
        for row in (objective.get("score_breakdown") or [])
        if isinstance(row, dict)
    )

    trait_report = build_trait_report(traits)

    return {
        "candidate_name": candidate_name,
        "job_key": job_config.get("job_key", ""),
        "job_name": job_name,
        # ---- 静态层 ----
        "objective_layer": {
            "total_score": objective.get("total_score", 0),
            "max_score": objective.get("max_score", 0),
            "match_level": match_level,
            "score_breakdown": objective.get("score_breakdown") or [],
            "note": "静态分只由简历标签 + job-config 规则算出，不含任何对话信息",
        },
        # ---- 主观层 ----
        "conversation_traits": traits,
        "trait_report": trait_report,
        # ---- 终局 ----
        "final_decision": decision["final_decision"],
        "decision_basis": decision["decision_basis"],
        "willing_for_target_job": willing,
        "willing_text": decision["willing_text"],
        # ---- 备选岗位推荐 ----
        # alternate_roles 是**实际输出**的那一份（已按意愿门控）；
        # matched_roles 保留全部命中项，供 HR 复核"为什么当时推/没推"。
        "alternate_roles": all_roles if show_roles else [],
        "matched_roles": all_roles,
        "alternate_roles_visible": show_roles,
        "alternate_roles_reason": roles_reason,
        "score_summary": score_detail,
    }


def render_report_text(report: Dict[str, Any]) -> str:
    """给 HR 看的纯文本报告。邮件、导出、打印都用这一份。"""
    objective = report.get("objective_layer", {})
    trait = report.get("trait_report", {})

    lines = [
        f"【测评报告】{report.get('candidate_name') or '候选人'} × {report.get('job_name', '')}",
        "",
        f"静态匹配：{objective.get('match_level', '')}"
        f"（{objective.get('total_score', 0)}/{objective.get('max_score', 0)} 分）",
    ]
    for row in objective.get("score_breakdown") or []:
        lines.append(
            f"  {row.get('direction', '')}{row.get('score', 0)}分 · {row.get('desc', '')}"
            f"（{row.get('field', '')} = {row.get('actual_value', '')}）"
        )

    lines += [
        "",
        f"对话意向：{report.get('willing_text', '')}",
    ]
    if trait.get("summary"):
        lines.append(f"对话摘要：{trait['summary']}")

    for label, key in (("偏好方向", "preferences"), ("排斥事项", "avoidances"), ("客观限制", "constraints")):
        entries = trait.get(key) or []
        if not entries:
            continue
        lines.append(f"{label}：")
        for entry in entries:
            evidence = f"（原话：{entry['evidence']}）" if entry.get("evidence") else ""
            lines.append(f"  - {entry.get('content', '')}{evidence}")

    lines += [
        "",
        f"终局判定：{report.get('final_decision', '')}",
        f"判定依据：{report.get('decision_basis', '')}",
    ]

    roles = report.get("alternate_roles") or []
    if roles:
        # 待人工复核时标题要说清这些岗位是给人判断用的，不是结论
        title = (
            "适配岗位（供人工复核参考）："
            if report.get("final_decision") == "待人工复核"
            else "备选岗位推荐："
        )
        lines.append(title)
        for role in roles:
            lines.append(f"  - {role.get('backup_job_name', '')}：{role.get('reason', '')}")
            points = role.get("match_points") or []
            if not points:
                # 逐条匹配点是需求里点名的内容，出不来就要让人看见"出不来"，
                # 不能静默只剩一句理由 —— 那等于让人以为没有匹配点。
                lines.append("      匹配点：（本岗位未配置命中条件，无逐条匹配点）")
                continue
            lines.append("      匹配点：")
            for point in points:
                lines.append(f"        {point.get('mark', _MATCH_MARKS['unknown'])} {point.get('condition', '')}")
                detail = point.get("evidence") or ""
                keywords = point.get("keywords") or []
                if keywords:
                    hint = "字面佐证词：" + "、".join(keywords)
                    detail = f"{detail}（{hint}）" if detail else hint
                if detail:
                    lines.append(f"            {detail}")
    elif report.get("matched_roles"):
        # 有命中但没输出：必须写清楚原因，否则 HR 分不清"没有备选岗"
        # 和"有备选岗但按规则压住了"。后者是要人工复核的情况。
        lines.append(f"备选岗位推荐：（{report.get('alternate_roles_reason', '')}）")
    else:
        lines.append("备选岗位推荐：该岗位未配置 alternate_role_mapping")

    return "\n".join(lines)