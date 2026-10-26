"""测评流程编排：把解析、主线对话、旁路、评估、打分串成一条线。

旧实现是演示桩：graduation_year 写死 2027、major 写死"软件工程"、
session_id 写死 "session_001"、final_decision 写死"推进"。
这里全部改为真实链路。

数据流（分数的产生顺序不能乱）：

    简历原文 ──ResumeParser──> resume_tags ─┐
                                          ├─> ChatAgent ──> 对话记录
    job_config ────────────────────────────┘                     │
                                                                ├─> RAG 旁路（可穿插）
                                                                │
                          ┌─────────────────────────────────────┘
                          ▼
    AssessmentAgent ──> conversation_traits / willing
                          │
                          ├─> rule_engine.assess(resume_tags, willing)  ← 唯一的分数来源
                          ├─> match_alternate_role_mapping（双层依据）
                          └─> CandidateMsgAgent ──> candidate_msg
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.assessment_agent import (
    AssessmentAgent,
    CandidateMsgAgent,
    ChatAgent,
    config_hash,
)
from app.job_config_store import get_job_config_object
from app.rag_bypass import RagBypass
from app.report import assemble_report, render_report_text
from app.redline import build_forbidden_terms
from app.session_store import (
    append_rag_event,
    create_session,
    get_session,
    save_assessment_result,
    save_resume_tags,
    save_session_answers,
    update_session_status,
)
from rule_engine import assess as assess_candidate
from rule_engine import score_objective_layer
from resume_parser import ResumeParser

logger = logging.getLogger(__name__)

# 主线最多轮数。达到上限就强制收尾 —— 不设上限的话模型有的是办法把测评聊成客服闲聊。
MAX_TURNS = 20

# 反问形式的纯确认语。命中即视为主线确认，不走旁路。
_MAINLINE_CONFIRMATIONS = frozenset({
    "好的", "好", "可以", "行", "接受", "没问题", "是的", "对", "没错",
    "知道了", "明白了", "了解了", "嗯", "好的可以", "可以接受",
})

# 问句正文最短长度。低于此值说明缺少上下文。
_DEEP_QUESTION_MIN_LEN = 8


class AssessmentError(RuntimeError):
    """流程性错误（岗位配置不存在、会话不存在），应当以 4xx 返回给调用方。"""


class AssessmentService:
    def __init__(self):
        self.chat = ChatAgent()
        self.assessment = AssessmentAgent()
        self.candidate_msg = CandidateMsgAgent()
        self.resume_parser = ResumeParser()
        self.rag = RagBypass()

    # -- 建会话 ---------------------------------------------------------

    async def start(self, *, job_key: str, resume_text: str, candidate_name: str = "") -> Dict[str, Any]:
        """解析简历 + 生成开场白，建立会话。"""
        job_config = self._load_config(job_key)

        resume_tags = await self.resume_parser.parse(resume_text, job_config)
        session_id = create_session(
            candidate_name=candidate_name or (resume_tags.get("name") or "候选人"),
            job_name=job_config.get("job_name", ""),
            resume_json=resume_tags,
            candidate_summary=resume_tags.get("resume_summary", "")[:2000],
            job_key=job_key,
        )

        opening = await self.chat.reply(job_config, resume_tags, [])
        chat_history: List[Dict[str, str]] = [
            {"role": "assistant", "content": opening.text}
        ]
        save_session_answers(
            session_id, [], opening.text, status="in_progress", chat_history=chat_history
        )

        return {
            "session_id": session_id,
            "job_key": job_key,
            "job_name": job_config.get("job_name", ""),
            "candidate_name": candidate_name or (resume_tags.get("name") or "候选人"),
            "opening_message": opening.text,
            "resume_tags": resume_tags,
            "parse_warnings": resume_tags.get("parse_warnings", []),
        }

    # -- 主循环 ---------------------------------------------------------

    async def handle_turn(self, session_id: str, user_answer: str) -> Dict[str, Any]:
        """候选人回一句话后的处理。"""
        session = get_session(session_id)
        if session is None:
            raise AssessmentError(f"会话不存在：{session_id}")

        job_config = self._load_config(session.get("job_key") or "")
        resume_tags = session.get("resume_tags") or session.get("resume_json") or {}

        if session.get("status") == "finished":
            return {
                "session_id": session_id,
                "message": session.get("candidate_msg") or "本次测评已结束。",
                "status": "finished",
                "turns": len(session.get("answers") or []),
            }

        answers = list(session.get("answers") or [])
        answers.append(user_answer)

        chat_history = list(session.get("chat_history") or [])
        chat_history.append({"role": "user", "content": user_answer})

        reply = await self.chat.reply(job_config, resume_tags, chat_history)

        # --- 约束 1 的代码闸：只有候选人主动发问才受理旁路 ---
        # 提示词里已限制触发场景，这里再挡一道：若本轮 Agent 是在推进要点
        # （上一条是 HR 的话、候选人这句是在回答），即使模型误打了 [NEED_RAG] 也不受理。
        rag_used = False
        text = reply.text
        if reply.need_rag_question and _is_deep_question(user_answer):
            rag_result = await self.rag.answer(
                reply.need_rag_question,
                job_config,
                forbidden_terms=build_forbidden_terms(job_config),
            )
            append_rag_event(
                session_id,
                {
                    "question": reply.need_rag_question,
                    "answered": bool(rag_result.answer),
                    "citations": rag_result.citations,
                    "blocked": rag_result.blocked,
                    "offline_hit": rag_result.offline_hit,
                },
            )
            text = f"{text}\n\n{rag_result.text}".strip() if text else rag_result.text
            rag_used = True
        elif reply.need_rag_question:
            logger.info(
                "旁路请求被拒：本轮并非候选人主动提问，job=%s", session.get("job_key")
            )
            append_rag_event(
                session_id,
                {"question": reply.need_rag_question, "rejected": "not_candidate_initiated"},
            )

        turn_limit_hit = len(answers) >= MAX_TURNS
        should_finish = bool(reply.is_done) or turn_limit_hit
        if turn_limit_hit and not reply.is_done:
            text = f"{text}\n\n今天就先聊到这里，感谢你的时间。".strip()

        chat_history.append({"role": "assistant", "content": text})
        save_session_answers(
            session_id,
            answers,
            text,
            status="finished" if should_finish else "in_progress",
            chat_history=chat_history,
        )

        result: Dict[str, Any] = {
            "session_id": session_id,
            "message": text,
            "status": "finished" if should_finish else "in_progress",
            "turns": len(answers),
            "rag_used": rag_used,
        }

        if should_finish:
            result["assessment"] = await self.finish(session_id)

        return result

    # -- 收尾 -----------------------------------------------------------

    async def finish(self, session_id: str) -> Dict[str, Any]:
        """完整收尾：评估 → 打分 → 备选岗 → 结语 → 落库。"""
        session = get_session(session_id)
        if session is None:
            raise AssessmentError(f"会话不存在：{session_id}")

        job_config = self._load_config(session.get("job_key") or "")
        resume_tags = session.get("resume_tags") or session.get("resume_json") or {}
        chat_history = list(session.get("chat_history") or [])

        evaluated = await self.assessment.evaluate(job_config, resume_tags, chat_history)

        # 分数在这里产生，且只在这里产生 —— 后端按 job_config 重算，
        # 覆盖模型可能给出的任何数字（定稿决策 4）
        objective = assess_candidate(
            job_config,
            resume_tags,
            evaluated.get("willing_for_target_job"),
        )

        backup_hits = await self.assessment.match_alternate_role_mapping(
            job_config, resume_tags, evaluated.get("conversation_traits") or []
        )

        candidate_msg = await self.candidate_msg.generate(
            job_config.get("job_name", ""),
            evaluated.get("check_results") or [],
            evaluated.get("willing_for_target_job"),
        )

        # 报告组装在这一层完成：静态分 + 对话意向 + 备选岗 合并成一份结论。
        # 传全部命中项，是否输出由 report 内部按意愿门控 —— 调用方不过滤。
        report = assemble_report(
            job_config=job_config,
            objective={**objective, "resume_tags": resume_tags},
            traits=evaluated.get("traits") or {},
            alternate_roles=backup_hits,
            candidate_name=session.get("candidate_name") or "",
        )
        visible_hits = report["alternate_roles"]
        hr_summary = render_report_text(report)

        save_assessment_result(
            session_id,
            objective=objective,
            subjective={
                "willing_for_target_job": evaluated.get("willing_for_target_job"),
                "conversation_traits": evaluated.get("conversation_traits") or [],
            },
            final_decision_text=objective["final_decision"],
            hr_summary=hr_summary,
            candidate_msg=candidate_msg,
            evidence={
                "check_results": evaluated.get("check_results") or [],
                "reason": evaluated.get("hr_summary", ""),
                # 逐条匹配点存在证据里而不是只存 backup_recommend_keys：
                # 只有 key 的话，HR 点开存档复看时拿不出"为什么适配这个岗"，
                # 重跑模型又等于拿今天的简历解析去解释当时的结论。
                "alternate_roles": backup_hits,
            },
            backup_recommend_keys=[h.get("backup_job_key", "") for h in visible_hits],
            config_hash=config_hash(job_config),
            # 存下这次的配置本体。HR 之后改岗位配置，历史报告要能原样还原。
            job_config_snapshot=job_config,
        )
        update_session_status(session_id, "finished")

        return {
            "final_decision": objective["final_decision"],
            "match_level": objective["match_level"],
            "total_score": objective["total_score"],
            "max_score": objective.get("max_score", 0),
            "score_breakdown": objective["score_breakdown"],
            "willing_for_target_job": evaluated.get("willing_for_target_job"),
            "conversation_traits": evaluated.get("conversation_traits") or [],
            "check_results": evaluated.get("check_results") or [],
            "alternate_roles": visible_hits,
            "report": report,
            "hr_summary": hr_summary,
            "candidate_msg": candidate_msg,
            "config_hash": config_hash(job_config),
        }

    # -- 辅助 -----------------------------------------------------------

    def build_report(self, session_id: str) -> Dict[str, Any]:
        """从已落库的测评结果重新组装报告，不重跑模型。

        报告能从存档重建，而不是只能看 finish() 当时的返回值 ——
        否则 HR 改完岗位配置想复看时，只能靠 hr_summary 那一列的旧文本。

        **用测评当时存下的配置快照，不用当前岗位配置。**
        HR 改岗位配置、或者干脆把岗位下线了，历史报告都得原样还原：
        用当前配置重算会让所有历史分数跟着新规则变，等于篡改历史。
        """
        session = get_session(session_id)
        if session is None:
            raise AssessmentError(f"会话不存在：{session_id}")

        job_config = self._config_for_report(session)
        resume_tags = session.get("resume_tags") or session.get("resume_json") or {}

        objective = score_objective_layer(job_config, resume_tags)

        traits = {
            "conversation_traits": session.get("conversation_traits") or [],
            "willing_for_target_job": session.get("willing_for_target_job"),
            "check_results": (session.get("evidence_json") or {}).get("check_results", []),
            "reason": (session.get("evidence_json") or {}).get("reason", ""),
        }

        stored_roles = {
            str(role.get("backup_job_key")): role
            for role in ((session.get("evidence_json") or {}).get("alternate_roles") or [])
            if isinstance(role, dict)
        }
        recommended = {str(k) for k in (session.get("backup_recommend_keys") or [])}

        roles = []
        for role in job_config.get("alternate_role_mapping") or []:
            if not isinstance(role, dict):
                continue
            key = str(role.get("backup_job_key") or "")
            if key not in recommended:
                continue
            # 条件原文以快照为准（HR 可能改过），模型当时的逐条判断从存档带回来。
            # 配置新加的条件模型没判过，build_match_points 会落回"未见依据"而不是硬凑一个命中。
            saved = stored_roles.get(key, {})
            roles.append(
                {
                    **role,
                    "reason": saved.get("reason", ""),
                    "match_points": saved.get("match_points") or [],
                    "condition_hints": saved.get("condition_hints") or [],
                }
            )

        report = assemble_report(
            job_config=job_config,
            objective={**objective, "resume_tags": resume_tags},
            traits=traits,
            alternate_roles=roles,
            candidate_name=session.get("candidate_name") or "",
        )
        report["report_text"] = render_report_text(report)
        report["config_hash"] = session.get("config_hash", "")
        return report

    @staticmethod
    def _config_for_report(session: Dict[str, Any]) -> Dict[str, Any]:
        """复看历史报告时该用哪份配置。

        优先用测评当时存下的快照。快照不存在只有一种情况：
        这条会话是在加快照之前做的，那时候的行为就是拿实时配置重算 ——
        那时岗位还没被改过，重算出来的结果与当初一致，不算失真。
        """
        snapshot = session.get("job_config_snapshot")
        if isinstance(snapshot, dict) and snapshot:
            return snapshot
        return AssessmentService._load_config(session.get("job_key") or "")

    @staticmethod
    def _load_config(job_key: str) -> Dict[str, Any]:
        if not job_key:
            raise AssessmentError("缺少 job_key")
        config = get_job_config_object(job_key)
        if config is None:
            raise AssessmentError(f"岗位配置不存在或已删除：{job_key}")
        return config


def _is_deep_question(text: str) -> bool:
    """候选人这句话是不是一次真正的主动提问。

    这是约束 1 的代码闸。提示词已明令禁止在确认要点时走旁路，
    但提示词是软约束，模型偶尔仍会误打 [NEED_RAG]。这里按机械信号兜底：

    - 必须是问句（含问号）—— 陈述、短句回答、表态都不含
    - 长度不能太短 —— "有前途吗？" 这类反问缺少上下文，答了必然是编的
    - 排除纯确认语的反问 —— "好的？""可以？" 仍属主线

    注意这是【否决权】而非批准权：判为问句只是允许进入旁路，
    答什么、能不能答，仍由检索命中与越界校验把关。
    """
    content = (text or "").strip()
    if "?" not in content and "？" not in content:
        return False

    stripped = content.rstrip("?？").strip()
    if len(stripped) < _DEEP_QUESTION_MIN_LEN:
        return False

    return stripped not in _MAINLINE_CONFIRMATIONS
