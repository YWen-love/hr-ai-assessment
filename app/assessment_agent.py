import logging
import re

from prompts import QUESTION_PROMPT, SUMMARY_PROMPT, SYSTEM_PROMPT
from app.deepseek_client import DeepSeekClient

logger = logging.getLogger(__name__)

_INTERNSHIP_DAILY_RATES = {
    "初中学习顾问": {"本科": 100, "硕士研究生": 130},
    "人力实习生": {"本科": 110, "硕士研究生": 150},
}
_DEGREE_DAILY_RATE_PATTERN = re.compile(
    r"(?:本科|硕士研究生)(?:实习生)?\s*[:：]?\s*"
    r"(?:每日底薪|日底薪|日薪)\s*\d+\s*元"
)


def _personalize_internship_salary(text: str, job_name: str, education: str | None) -> str:
    rates = _INTERNSHIP_DAILY_RATES.get(job_name)
    if not rates:
        return text

    recognized_education = education if education in rates else None
    tailored_text = _DEGREE_DAILY_RATE_PATTERN.sub(
        lambda match: (
            match.group(0)
            if recognized_education and match.group(0).startswith(recognized_education)
            else ""
        ),
        text,
    )
    if recognized_education:
        salary_instruction = (
            f"本候选人简历识别的最高学历为{recognized_education}，"
            f"本次只向候选人介绍{recognized_education}实习生日底薪"
            f"{rates[recognized_education]}元。不要提及其他学历对应的日薪，也不要同时列出多个金额。"
        )
    else:
        salary_instruction = (
            "本候选人的最高学历无法确认为本科或硕士研究生，不得向候选人报出这两档日薪，"
            "应说明具体日薪需由HR核实。"
        )
    return f"{tailored_text}\n\n候选人薪资说明要求：{salary_instruction}"


class AssessmentAgent:
    def __init__(self):
        self.client = DeepSeekClient()

    @staticmethod
    def fallback_question(candidate_summary: dict, chat_history: str = "") -> str:
        name = candidate_summary.get("name") or "候选人"
        job_name = candidate_summary.get("job_name") or "岗位"
        if job_name == "人力实习生" and AssessmentAgent._hr_screening_completed(chat_history):
            return (
                "好的，已记录你的实习时间安排，也感谢你确认接受工作内容、工作时间和薪资待遇。"
                "人力实习生测评到这里就结束了，后续会有高途的人事与你进行电话联系，请留意来电。"
            )
        return (
            f"抱歉，刚才系统暂时没有生成{job_name}岗位的下一步问题，你刚才的回答已记录。"
            "为了继续测评，请再确认一下是否接受岗位介绍中的工作内容、工作时间和薪资安排？"
        )

    @staticmethod
    def _hr_screening_completed(chat_history: str) -> bool:
        turns = []
        lines = chat_history.splitlines()
        for index, line in enumerate(lines[:-1]):
            if line.startswith("小助手:") and lines[index + 1].startswith("候选人:"):
                turns.append((line.removeprefix("小助手:").strip(), lines[index + 1].removeprefix("候选人:").strip()))

        def accepted_topic(*markers: str) -> bool:
            for question, answer in turns:
                if any(marker in question for marker in markers):
                    if any(word in answer for word in ("不接受", "不能", "不可以", "不愿意")):
                        continue
                    if any(word in answer for word in ("可以", "接受", "没问题", "愿意")):
                        return True
            return False

        availability_confirmed = False
        day_values = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
                      "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
        for question, answer in turns:
            if "每周" not in question or "实习" not in question:
                continue
            day_match = re.search(r"([1-7一二三四五六七])\s*天", answer)
            duration_match = re.search(r"([3-9三四五六七八九])\s*个?月|半年|一年", answer)
            if day_match and day_values[day_match.group(1)] >= 4 and duration_match:
                availability_confirmed = True
                break

        return (
            availability_confirmed
            and accepted_topic("工作内容")
            and accepted_topic("时间安排", "工作时间", "时间和休息")
            and accepted_topic("待遇", "薪资")
        )

    @staticmethod
    def initial_question(candidate_summary: dict) -> str:
        name = candidate_summary.get("name") or "候选人"
        job_name = candidate_summary.get("job_name") or "初中学习顾问"
        graduation_status = candidate_summary.get("graduation_status", "未知")
        greeting = f"你好，{name}，我是智能测评小助手途途，接下来就{job_name}岗位跟你做几个简单沟通，你按实际情况回答就好。"

        if graduation_status == "已毕业":
            return f"{greeting}正式介绍前先问一下，你之前了解咱们{job_name}主要是做什么工作的吗？"
        if graduation_status == "未毕业":
            graduation_year = candidate_summary.get("graduation_year")
            cohort = f"{graduation_year}届" if graduation_year else ""
            if job_name == "人力实习生":
                return (
                    f"{greeting}这边看到你是{cohort}，想先确认一下，"
                    "你是计划以实习为主，还是希望后续争取实习转正机会呢？"
                )
            return f"{greeting}这边看到你是{cohort}，想先确认一下，你是考虑先来公司实习，还是想走校招、通过实习争取转正机会呢？"
        return f"{greeting}为了确定后续沟通方向，想先确认一下，你目前已经毕业了吗？"

    async def generate_question(self, candidate_summary: dict, job_intro: str, screening_rules: str, chat_history: str = "") -> str:
        job_name = candidate_summary.get("job_name", "")
        education = candidate_summary.get("education")
        if candidate_summary.get("graduation_status") == "未毕业":
            job_intro = _personalize_internship_salary(job_intro, job_name, education)
            screening_rules = _personalize_internship_salary(
                screening_rules, job_name, education
            )
        prompt = QUESTION_PROMPT.format(
            candidate_summary=str(candidate_summary),
            job_intro=job_intro,
            screening_rules=screening_rules,
            chat_history=chat_history if chat_history else "（刚开始对话，还没有历史记录）",
        )
        try:
            return await self.client.chat(SYSTEM_PROMPT, prompt)
        except Exception:
            logger.exception("Question generation failed for job %s", candidate_summary.get("job_name"))
            return self.fallback_question(candidate_summary, chat_history)

    async def generate_screening_rules(self, job_name: str, job_intro: str) -> str:
        system_prompt = (
            "你是招聘测评规则设计助手。只根据提供的岗位介绍生成可执行的候选人筛选规则。"
            "不得杜撰学历、年龄、经验、薪资、工作时间或硬性门槛；未说明的信息标为“需HR确认”，"
            "不要把它写成淘汰条件。输出中文 Markdown，包含简历静默筛查、测评问题顺序、"
            "岗位介绍/工作时间/薪资确认、通过与不通过条件及结束语。"
        )
        user_prompt = f"岗位名称：{job_name}\n\n岗位介绍：\n{job_intro}"
        return await self.client.chat(system_prompt, user_prompt)

    async def summarize(self, candidate_summary: dict, job_intro: str, answers: str) -> dict:
        prompt = SUMMARY_PROMPT.format(
            candidate_summary=str(candidate_summary),
            job_intro=job_intro,
            answers=answers,
        )
        try:
            content = await self.client.chat(SYSTEM_PROMPT, prompt)
        except Exception:
            logger.exception("Summarize failed for job %s", candidate_summary.get("job_name"))
            content = "模型调用失败，已启用本地演示摘要模式。"
        return {"raw_output": content}
