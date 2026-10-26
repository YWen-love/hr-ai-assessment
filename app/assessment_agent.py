"""测评主流程编排。

三类 Agent，一个文件，按隔离要求分工：

- **ChatAgent** —— 主线对话。上下文里【没有 alternate_role_mapping】，模型无从说漏备选岗。
- **AssessmentAgent** —— 收尾评估。只在这里才加载备选岗位，产出事实不给分数。
- **CandidateMsgAgent** —— 候选人侧回访。上下文里连 job_config 都不给，
  只给它"确认结果 + 是否愿意"，从物理上杜绝候选人看到任何内部判断。

分数一律不在本文件产生，由 rule_engine 按 job_config 重算（定稿决策 4）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from prompts import (
    ASSESSMENT_SYSTEM_PROMPT,
    BACKUP_MATCH_SYSTEM_PROMPT,
    CANDIDATE_MSG_SYSTEM_PROMPT,
    CHAT_SYSTEM_PROMPT,
)
from app.job_schema import iter_check_item_texts
from app.llm_client import LLMClient
from app.redline import ChatMessage, build_forbidden_terms, parse_chat_message
from app.report import keyword_hints, should_recommend_alternates
from app.traits import TraitExtractor, trait_summary

logger = logging.getLogger(__name__)


def config_hash(job_config: Dict[str, Any]) -> str:
    """岗位配置指纹。测评结论要能追溯到"用的是哪一版配置"。"""
    payload = json.dumps(job_config, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _profile_lines(job_config: Dict[str, Any]) -> List[str]:
    profile = job_config.get("job_profile") or {}

    def joined(field_name: str) -> str:
        values = profile.get(field_name) or []
        if isinstance(values, list):
            return "；".join(str(v) for v in values if v)
        return str(values) if values else "（未配置）"

    return [joined("responsibilities"), joined("work_time"), joined("salary")]


def render_resume_tags(resume_tags: Dict[str, Any], *, max_exp_tags: int = 20) -> str:
    """简历标签的可读化。控制长度 —— 标签全量塞进 prompt 会挤占对话注意力。"""
    tags = resume_tags or {}
    school_type = tags.get("school_type") or {}

    lines = [
        f"姓名：{tags.get('name') or '未提供'}",
        f"学历：{tags.get('degree') or '未识别'}",
        f"专业：{tags.get('major') or '未识别'}",
        f"毕业院校：{tags.get('school_name') or '未识别'}",
        f"毕业年份：{tags.get('graduation_year') or '未识别'}"
        f"（{tags.get('graduation_status') or '未知'}）",
        f"求职意向：{tags.get('resume_intention') or '未填写'}",
    ]

    if isinstance(school_type, dict) and any(school_type.values()):
        marks = [key for key, hit in school_type.items() if hit is True]
        lines.append(f"院校类型：{', '.join(marks)}")

    exp_tags = [t for t in (tags.get("exp_tags") or []) if t][:max_exp_tags]
    lines.append(f"经验标签：{'、'.join(exp_tags) if exp_tags else '未识别'}")

    work = tags.get("work_experience")
    if work:
        lines.append(f"经历概括：{work}")

    return "\n".join(lines)


def format_transcript(chat_history: List[Dict[str, str]]) -> str:
    """对话记录转文本。角色标签用中性的"HR/候选人"，不含岗位名。"""
    if not chat_history:
        return "（无对话记录）"
    lines = []
    for message in chat_history:
        role = "HR" if message.get("role") == "assistant" else "候选人"
        content = (message.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines) or "（无对话记录）"


# ---------------------------------------------------------------------------
# 主线对话
# ---------------------------------------------------------------------------

class ChatAgent:
    """主线对话 Agent。

    硬性隔离：__init__ 之后本对象的任何方法都不会把 alternate_role_mapping 传给模型。
    不是靠 prompt 叮嘱，是数据流上根本没有这个字段。
    """

    def __init__(self):
        self.client = LLMClient(temperature=0.6)

    def render_system_prompt(self, job_config: Dict[str, Any], resume_tags: Dict[str, Any]) -> str:
        """只注入主岗信息。alternate_role_mapping 不在这里，也没有别的地方。"""
        responsibilities, work_time, salary = _profile_lines(job_config)
        return (
            CHAT_SYSTEM_PROMPT
            .replace("{{job_name}}", job_config.get("job_name", "该岗位"))
            .replace("{{job_responsibilities}}", responsibilities)
            .replace("{{job_nature}}", "、".join((job_config.get("job_profile") or {}).get("nature") or []) or "详见岗位职责")
            .replace("{{job_work_time}}", work_time)
            .replace("{{job_rest_schedule}}", "、".join((job_config.get("job_profile") or {}).get("rest_schedule") or []) or "未配置")
            .replace("{{job_salary}}", salary)
            .replace("{{check_items}}", "；".join(iter_check_item_texts(job_config)) or "（无）")
            .replace("{{resume_tags}}", render_resume_tags(resume_tags))
        )

    async def reply(
        self,
        job_config: Dict[str, Any],
        resume_tags: Dict[str, Any],
        chat_history: List[Dict[str, str]],
    ) -> ChatMessage:
        """生成一轮回复。协议标记与红线词在此刻被代码捕获。"""
        # 红线词每次按当前岗位配置重建：运营可能刚给这个岗位加了备选岗
        forbidden = build_forbidden_terms(job_config)
        system_prompt = self.render_system_prompt(job_config, resume_tags)

        try:
            raw = await self.client.chat(
                system_prompt,
                f"对话记录：\n{format_transcript(chat_history)}",
            )
        except Exception:
            logger.exception("主线对话生成失败，job=%s", job_config.get("job_key"))
            raw = FALLBACK_REPLY

        message = parse_chat_message(raw, forbidden_terms=forbidden)
        if message.was_blocked:
            logger.warning(
                "拦截到红线词 %s，job=%s", message.blocked_terms, job_config.get("job_key")
            )
            message.text = FALLBACK_BLOCKED
        elif not message.text.strip():
            message.text = FALLBACK_REPLY

        return message


FALLBACK_REPLY = "抱歉，我这边网络有点问题，刚才那句没发出去，能再说一次吗？"
FALLBACK_BLOCKED = "好的，我这边先记录下来，稍后同步给 HR 跟你确认。"


# ---------------------------------------------------------------------------
# 收尾评估
# ---------------------------------------------------------------------------

class AssessmentAgent:
    """收尾评估 Agent。只产出事实，不产出分数。"""

    def __init__(self):
        self.client = LLMClient(temperature=0.2)
        self.traits = TraitExtractor()

    async def evaluate(
        self,
        job_config: Dict[str, Any],
        resume_tags: Dict[str, Any],
        chat_history: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """产出：逐条确认结果 / 推进意愿 / 备选岗命中 / HR 总结。

        分数不在这里出现。模型给了也会被忽略 —— rule_engine 才是唯一的分数来源。
        """
        check_items = iter_check_item_texts(job_config)
        prompt = (
            ASSESSMENT_SYSTEM_PROMPT
            .replace("{{job_name}}", job_config.get("job_name", "该岗位"))
            .replace("{{check_items}}", "；".join(check_items) or "（无）")
            .replace("{{resume_tags}}", render_resume_tags(resume_tags))
            .replace("{{transcript}}", format_transcript(chat_history))
            .replace("{{alternate_role_mapping}}", _render_backups(job_config))
        )

        raw: Dict[str, Any] = {}
        try:
            raw = await self.client.chat_json(
                prompt, "请基于以上信息输出评估 JSON。"
            )
        except Exception:
            logger.exception("收尾评估失败，job=%s", job_config.get("job_key"))

        subject = await self.traits.extract(
            format_transcript(chat_history), job_config
        )

        return _merge_assessment(raw, subject, check_items, job_config)

    async def match_alternate_role_mapping(
        self,
        job_config: Dict[str, Any],
        resume_tags: Dict[str, Any],
        traits: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """逐个备选岗独立判定。

        与 prompt 里的批量判断分开：批量判断容易互相串味，
        单独一次调用每次只看一个备选岗，隔离性更好，也便于留痕。
        """
        hits: List[Dict[str, Any]] = []
        summary = trait_summary(traits)
        context = {**resume_tags, "trait_summary": summary}

        for backup in job_config.get("alternate_role_mapping") or []:
            if not isinstance(backup, dict):
                continue
            conditions = [str(c) for c in (backup.get("match_condition") or []) if str(c).strip()]
            condition_text = "；".join(conditions)
            # 字面佐证逐条算，不合并。合并后只剩一个词袋，
            # 报告里就分不清是"这条条件有线索"还是"那条条件有线索"。
            condition_hints = [
                {"condition": condition, "keywords": keyword_hints(condition, context)}
                for condition in conditions
            ]
            hints = [word for row in condition_hints for word in row["keywords"]]

            prompt = (
                BACKUP_MATCH_SYSTEM_PROMPT
                .replace("{{backup_job_name}}", backup.get("backup_job_name", "备选岗位"))
                .replace("{{match_conditions}}", condition_text)
                .replace("{{resume_tags}}", render_resume_tags(resume_tags))
                .replace("{{trait_summary}}", summary or "（对话中未表达明确偏好）")
            )
            if hints:
                # 明确标注这只是字面线索，不能当命中依据（约束在 prompt 里也写了）
                prompt += f"\n\n系统提供的字面佐证线索（仅供参考，不构成命中依据）：{'、'.join(hints)}"

            try:
                result = await self.client.chat_json(prompt, "请判断是否命中，只输出 JSON。")
            except Exception:
                logger.exception("备选岗匹配失败：%s", backup.get("backup_job_key"))
                continue

            if result.get("hit") is True:
                hits.append(
                    {
                        "backup_job_key": backup.get("backup_job_key", ""),
                        "backup_job_name": backup.get("backup_job_name", ""),
                        "reason": str(result.get("reason", ""))[:300],
                        # 条件原文带下去：报告层要逐条展开匹配点，
                        # 而它拿不到 job-config，就只能靠这份拷贝。
                        "match_condition": conditions,
                        "condition_hints": condition_hints,
                        "match_points": result.get("match_points") or [],
                        "evidence": result.get("evidence") or [],
                        "keyword_hints": hints,
                    }
                )
        return hits


def _render_backups(job_config: Dict[str, Any]) -> str:
    backups = job_config.get("alternate_role_mapping") or []
    if not backups:
        return "（本岗位未配置备选储备岗位）"
    lines = []
    for backup in backups:
        if isinstance(backup, dict):
            conditions = "；".join(str(c) for c in (backup.get("match_condition") or []))
            lines.append(f"- {backup.get('backup_job_key', '')}：{backup.get('backup_job_name', '')}；命中条件：{conditions}")
    return "\n".join(lines)


def _merge_assessment(
    raw: Any,
    subject: Dict[str, Any],
    check_items: List[str],
    job_config: Dict[str, Any],
) -> Dict[str, Any]:
    """合并模型评估与偏好抽取结果。

    willing_for_target_job 优先取偏好抽取模块的判定 —— 那个 prompt 是专门为
    「意愿」设计的，通用评估 prompt 在同一件事上更容易被简历匹配度带偏。
    """
    raw = raw if isinstance(raw, dict) else {}

    willing = subject.get("willing_for_target_job")
    if not isinstance(willing, bool):
        willing = raw.get("willing_for_target_job")
    if not isinstance(willing, bool):
        willing = None

    switch = job_config.get("backup_show_switch")
    hits = raw.get("hit_alternate_role_mapping") or []
    if not isinstance(hits, list):
        hits = []

    # 只有明确拒绝主岗才推备选。未表态不自动推，交给人工复核。
    # 判定实现在 app/report.py，这里只透出结果 —— 决策逻辑单一实现点。
    roles_visible, roles_reason = should_recommend_alternates(willing, switch)

    return {
        # traits 原样透出：报告组装层要读完整的 conversation_traits 结构
        # （含 check_results、reason），不能只拿到拍平的数组。
        "traits": {**subject, "willing_for_target_job": willing},
        "check_results": subject.get("check_results") or [],
        "conversation_traits": subject.get("conversation_traits") or [],
        "willing_for_target_job": willing,
        "hit_alternate_role_mapping": [h for h in hits if isinstance(h, dict)],
        "hr_summary": str(raw.get("hr_summary", "")).strip(),
        "backup_visible": roles_visible,
        "backup_reason": roles_reason,
    }


# ---------------------------------------------------------------------------
# 候选人侧回访（物理隔离）
# ---------------------------------------------------------------------------

class CandidateMsgAgent:
    """生成给候选人看的结语。

    只接收：岗位名、逐条确认结果、是否愿意。job_config、备选岗、分数一律不传。
    """

    def __init__(self):
        self.client = LLMClient(temperature=0.4)

    async def generate(
        self,
        job_name: str,
        check_results: List[Dict[str, Any]],
        willing: Optional[bool],
        *,
        fallback: str = "好的，感谢你今天的沟通，后续结果会由 HR 同步给你。",
    ) -> str:
        accepted = sum(
            1 for item in check_results
            if isinstance(item, dict) and item.get("accepted") is True
        )
        total = len(check_results)

        prompt = (
            CANDIDATE_MSG_SYSTEM_PROMPT
            .replace("{{job_name}}", job_name or "该岗位")
            .replace("{{check_results_text}}", f"共确认 {total} 项，其中确认接受 {accepted} 项")
            .replace("{{willing_text}}", {
                True: "候选人明确表示愿意继续",
                False: "候选人明确表示不接受",
            }.get(willing, "候选人未明确表态"))
        )

        try:
            return (await self.client.chat(prompt, "请生成结语。")).strip() or fallback
        except Exception:
            logger.exception("候选人结语生成失败，job=%s", job_name)
            return fallback