"""测评结论计算：客观层打分 + 双维度终局矩阵。

本模块【不含任何业务常量】。旧实现对任何候选人都返回 job_acceptance="接受"、
final_decision="观察"，那不是评估，那是常量。

现在的分工：
- 打分规则、分值、档位阈值 → 全部来自该岗位自己的 job_config（app/job_schema.py）
- 终局是否推进 → 客观档位 × 主观意向 的固定矩阵

新增一个岗位只需要新增一条 job_config 记录，本文件一行都不用改。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.job_schema import (
    level_for_score,
    resolve_field,
    score_rules_for,
)
from app.report import decide


def score_objective_layer(
    job_config: Dict[str, Any],
    resume_tags: Dict[str, Any],
) -> Dict[str, Any]:
    """客观层：只读简历静态标签，按配置算分并划档。

    不接受任何对话内容 —— 主观意向由 conversation_traits 单独承载，
    两层混算就退化成"聊天说得好听就给分"。
    """
    total, hits = score_rules_for(job_config, resume_tags)
    return {
        "total_score": total,
        "match_level": level_for_score(job_config, total),
        "score_breakdown": _explain(hits, resume_tags, job_config),
        "max_score": _max_score(job_config),
    }


def assess(
    job_config: Dict[str, Any],
    resume_tags: Dict[str, Any],
    willing_for_target_job: Optional[bool],
) -> Dict[str, Any]:
    """完整结论：客观分 + 主观意愿 → 终局决策。

    willing_for_target_job 为 None 表示候选人对话中未明确表态。
    此时结论是「待人工复核」而不是「不推进」—— 候选人是没说清楚，
    不是说了不。判不出结论就如实说判不出，由 HR 带着适配岗位去判断。

    终局判定本身实现在 app/report.py（报告组装层），这里只做转发：
    决策逻辑必须只有一个实现点，否则两处各判一次迟早判出两个结果。
    """
    objective = score_objective_layer(job_config, resume_tags)
    decision = decide(objective["match_level"], willing_for_target_job)

    return {
        **objective,
        "willing_for_target_job": willing_for_target_job,
        "final_decision": decision["final_decision"],
        "decision_basis": decision["decision_basis"],
        "willing_text": decision["willing_text"],
    }


def _max_score(job_config: Dict[str, Any]) -> int:
    """满分参考值。只用于 HR 界面展示"当前 x / 满分 y"，不参与任何判定。"""
    rules = job_config.get("score_rules") or {}
    total = 0
    for entry in rules.get("add_score") or []:
        if isinstance(entry, dict):
            total += abs(int(entry.get("score") or 0))
    return total


def _explain(
    hits: List[Dict[str, Any]],
    resume_tags: Dict[str, Any],
    job_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """把命中的规则展开成 HR 能直接看懂的明细。

    每条都带上"命中了哪个字段的实际值"—— 只说"+3 分"没有用，
    HR 要知道是因为简历里写了什么才加的分。
    """
    minus_ids = {
        rule.get("id")
        for rule in ((job_config.get("score_rules") or {}).get("minus_score") or [])
        if isinstance(rule, dict)
    }

    rows: List[Dict[str, Any]] = []
    for rule in hits:
        field = rule.get("field", "")
        # AND 容器没有单一 field，展开成「A 且 B」并把各子条件的实际值也拼出来，
        # 否则报告里会渲染成「（ = None）」。纯展示层，不参与任何判定。
        conds = [c for c in (rule.get("all_of") or []) if isinstance(c, dict)]
        if conds:
            field = " 且 ".join(str(c.get("field", "")) for c in conds)
            actual_value: Any = " 且 ".join(
                str(resolve_field(resume_tags, str(c.get("field", "")))) for c in conds
            )
        else:
            actual_value = resolve_field(resume_tags, field)
        rows.append(
            {
                "rule_id": rule.get("id", ""),
                "desc": rule.get("desc", ""),
                "field": field,
                "actual_value": actual_value,
                "expected_value": rule.get("value"),
                "score": int(rule.get("score") or 0),
                "direction": "扣分" if rule.get("id") in minus_ids else "加分",
            }
        )
    return rows