import json
from pathlib import Path

from app.assessment_agent import AssessmentAgent
from resume_parser import determine_graduation_status, parse_resume_text
from rule_engine import evaluate_candidate
from app.paths import data_root
from app.job_reader import read_prompt_template
from app.knowledge_base import get_job_knowledge
from app.resume_reader import read_resume_text
from app.session_store import get_session, save_session_answers, save_session_result, update_session_status


class AssessmentService:
    def __init__(self):
        self.agent = AssessmentAgent()

    async def prepare_job_knowledge(self) -> None:
        from app.knowledge_base import generate_missing_screening_rules

        await generate_missing_screening_rules(self.agent)

    async def parse_resume(self, file) -> dict:
        file_path = data_root() / "uploads" / Path(file.filename).name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        contents = await file.read()
        file_path.write_bytes(contents)

        text = await read_resume_text(str(file_path))
        profile = parse_resume_text(text)
        return {
            "status": "ok",
            "candidate": profile.model_dump(),
        }

    async def start_assessment(self, candidate_id: str, job_name: str) -> dict:
        knowledge = get_job_knowledge(job_name)
        job_intro = knowledge.job_intro
        screening_rules = knowledge.screening_rules
        prompt_template = read_prompt_template("docs/prompt_template.md")

        candidate_summary = {
            "candidate_id": candidate_id,
            "name": "候选人",
            "graduation_year": 2027,
            "graduation_status": determine_graduation_status(2027),
            "major": "软件工程",
            "education": "本科",
            "job_name": job_name,
            "prompt_template": prompt_template,
        }

        question = await self.agent.generate_question(candidate_summary, job_intro, screening_rules)

        return {
            "session_id": "session_001",
            "candidate_id": candidate_id,
            "job_name": job_name,
            "first_question": question,
            "job_intro_loaded": True,
            "screening_rules_loaded": True,
            "prompt_template_loaded": True,
        }

    async def launch_candidate_session(self, session_id: str, session_data: dict) -> dict:
        get_job_knowledge(session_data.get("job_name", ""))
        resume_json = session_data.get("resume_json", {})

        candidate_summary = {
            "candidate_id": session_id,
            "name": session_data.get("candidate_name", "候选人"),
            "graduation_year": resume_json.get("graduation_year"),
            "graduation_status": resume_json.get("graduation_status") or determine_graduation_status(resume_json.get("graduation_year")),
            "major": resume_json.get("major"),
            "education": resume_json.get("education"),
            "job_name": session_data.get("job_name"),
            "resume_summary": session_data.get("candidate_summary"),
        }

        question = self.agent.initial_question(candidate_summary)
        
        # 初始化对话历史，把第一句话加进去
        chat_history = [
            {"role": "assistant", "content": question}
        ]
        save_session_answers(session_id, [], question, status="in_progress", chat_history=chat_history)
        
        return {
            "mode": "candidate",
            "session_id": session_id,
            "candidate_name": session_data.get("candidate_name", "候选人"),
            "job_name": session_data.get("job_name"),
            "first_question": question,
            "resume_loaded": True,
        }

    async def next_question(self, session_id: str, user_answer: str) -> dict:
        session = get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")
        
        # 已完成的会话不允许继续答题
        if session.get("status") == "finished":
            return {
                "session_id": session_id,
                "next_question": "本次测评已结束，请联系HR了解结果。",
                "answer_received": user_answer,
                "answers_count": len(session.get("answers", [])),
                "status": "finished",
            }

        answers = list(session.get("answers", []) or [])
        answers.append(user_answer)
        
        # 获取完整对话历史
        chat_history = list(session.get("chat_history", []) or [])
        chat_history.append({"role": "user", "content": user_answer})
        
        # 把对话历史拼成文本传给AI
        chat_history_text = "\n".join(
            f"{'小助手' if msg['role'] == 'assistant' else '候选人'}: {msg['content']}"
            for msg in chat_history
        )

        # 调用AI生成下一个问题
        resume_json = session.get("resume_json", {})
        candidate_summary = {
            "candidate_id": session_id,
            "name": session.get("candidate_name", "候选人"),
            "graduation_year": resume_json.get("graduation_year"),
            "graduation_status": resume_json.get("graduation_status") or determine_graduation_status(resume_json.get("graduation_year")),
            "major": resume_json.get("major"),
            "education": resume_json.get("education"),
            "job_name": session.get("job_name"),
            "resume_summary": session.get("candidate_summary"),
        }

        current_job = session.get("job_name", "")
        knowledge = get_job_knowledge(current_job)
        job_intro = knowledge.job_intro
        screening_rules = knowledge.screening_rules
        next_question = await self.agent.generate_question(candidate_summary, job_intro, screening_rules, chat_history_text)
        
        # 把AI回复加进对话历史
        chat_history.append({"role": "assistant", "content": next_question})
        
        # 判断是否是结束语，如果是自动标记为已完成
        end_keywords = ['很高兴你可以接受', '很遗憾', '祝你找到其他合适的工作机会', '后续会有高途的人事与你进行电话联系']
        is_end = any(kw in next_question for kw in end_keywords)
        new_status = "finished" if is_end else "in_progress"
        
        save_session_answers(session_id, answers, next_question, status=new_status, chat_history=chat_history)
        
        # 如果是结束语，自动生成总结结果
        if is_end:
            await self.finish_assessment(session_id)

        return {
            "session_id": session_id,
            "next_question": next_question,
            "answer_received": user_answer,
            "answers_count": len(answers),
            "status": new_status,
        }

    async def finish_assessment(self, session_id: str) -> dict:
        session = get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        resume_json = session.get("resume_json", {})
        candidate_summary = {
            "candidate_id": session_id,
            "name": session.get("candidate_name", "候选人"),
            "graduation_year": resume_json.get("graduation_year"),
            "graduation_status": resume_json.get("graduation_status") or determine_graduation_status(resume_json.get("graduation_year")),
            "major": resume_json.get("major"),
            "education": resume_json.get("education"),
            "job_name": session.get("job_name"),
            "answers": session.get("answers", []),
        }

        current_job = session.get("job_name", "")
        job_intro = get_job_knowledge(current_job, require_rules=False).job_intro
        result = evaluate_candidate(candidate_summary, {"job_name": session.get("job_name")})
        # 用候选人的回答生成总结（不再用硬编码问题列表）
        summary_text = "\n".join(f"候选人回答{i+1}: {answer}" for i, answer in enumerate(session.get("answers", [])))
        user_summary = await self.agent.summarize(candidate_summary, json.dumps(job_intro, ensure_ascii=False), summary_text)

        result["final_decision"] = "推进"
        result["reason_summary"] = user_summary.get("raw_output", "候选人已完成测评并具备基本岗位匹配。")
        save_session_result(session_id, result["final_decision"], result["reason_summary"])

        return {
            "session_id": session_id,
            "final_result": result,
            "conversation_summary": result["reason_summary"],
        }
