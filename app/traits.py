"""对话意向抽取（conversation_traits）。

**固定组件**：schema、prompt、代码逻辑不随岗位变化，后续不再改动。
新增岗位只改 job-config，本模块一行都不用动。

职责边界（对应「项目诉求对齐」的禁止项）：
- 只做客观结构化提取：候选人说了什么、愿不愿意
- 不打分、不算匹配分、不做淘汰/通过判决、不推荐岗位
- 不含任何岗位专属业务规则；岗位名与要点一律由 job_config 传入

决策、推荐、报告组装全部在 app/report.py 那一层，读本模块的产物去做。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.job_schema import iter_check_item_texts
from app.llm_client import LLMClient

logger = logging.getLogger(__name__)

# 偏好分类。固定枚举而非自由发挥，便于 HR 后台按类目筛选。
# 这六类覆盖「拒绝事项 / 偏好方向 / 硬性客观限制」三类诉求 + 佐证，
# 新增岗位不需要动这个表。
TRAIT_CATEGORIES: Dict[str, str] = {
    "job_preference": "倾向岗位/职业方向",
    "avoidance": "明确排斥的方向",
    "work_style": "工作偏好（节奏、形式、强度）",
    "personality_fit": "性格与自我认知诉求",
    "motivation": "求职动机（看重收入/成长/稳定/兴趣）",
    "constraint": "候选人自述的客观限制（家庭、地域、时间）",
}

TRAIT_EXTRACT_SYSTEM_PROMPT = """
你是招聘测评的对话分析器。你的唯一任务：从一段 HR 与候选人的完整对话记录里，
客观结构化地抽取候选人【自己主动表达】的意向。

【当前岗位】
岗位名称：{{job_name}}
需要确认的要点：{{check_items}}

【绝对约束】
1. 只记录候选人在对话中【实际说过的内容】。没说过的一律不记录，不推测、不脑补。
2. 不要记录 HR 的引导话术，只记录候选人的表达。
3. evidence 字段必须能在对话原文里找到依据，禁止编造。
4. 不要提及除上述岗位之外的任何岗位名称。

【禁止项 —— 越界即为错误输出】
- 不要输出分数、档位、匹配度。打分由代码按 job-config 执行，不归你管。
- 不要输出「淘汰 / 通过 / 不通过 / 建议录用」这类业务判决。
- 不要推荐任何其他岗位，也不要判断候选人适合哪个岗位。
- 不要写任何岗位红线、硬性淘汰条件。
你的产出只是「候选人表达了什么」，仅此而已。

【conversation_traits 抽取范围】
- job_preference：候选人提到的想做的方向
- avoidance：候选人明确排斥、不想做的方向
- work_style：对工作节奏、工作形式、工作强度的偏好
- personality_fit：候选人自述的性格特点，或对工作状态的要求
- motivation：候选人求职时最看重的因素
- constraint：候选人自述的现实限制（家庭、地域、可投入时间等）

每条偏好写成一句中文，20 字以内，能独立看懂。
举例一律使用中性措辞，严禁写入任何具体行业或岗位的示例。

【willing_for_target_job 判定】
只判断候选人对【当前岗位】的推进意愿，不涉及任何其他判断：
- true：候选人接受岗位性质、工作时间、薪资等关键要点，态度是愿意继续
- false：候选人明确表示不接受、或明确表示不考虑了
- null：对话未明确表态、信息不足 —— 这种情况不要猜

【输出格式】只输出 JSON，不要任何解释文字，不要代码围栏：
{
  "conversation_traits": [
    {"category": "job_preference|avoidance|work_style|personality_fit|motivation|constraint",
     "content": "偏好描述",
     "evidence": "对话中的原话片段"}
  ],
  "willing_for_target_job": true,
  "check_results": [
    {"text": "要点原文", "accepted": true}
  ],
  "reason": "一句话说明判定依据"
}
"""


class TraitExtractor:
    def __init__(self):
        self.client = LLMClient(temperature=0.0)

    async def extract(
        self,
        transcript: str,
        job_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """抽取主观层。失败时返回空结构，绝不阻断测评主流程。"""
        job_name = job_config.get("job_name", "当前岗位")
        check_items = "；".join(iter_check_item_texts(job_config)) or "（无）"

        prompt = TRAIT_EXTRACT_SYSTEM_PROMPT.replace("{{job_name}}", job_name).replace(
            "{{check_items}}", check_items
        )

        try:
            data = await self.client.chat_json(
                prompt,
                f"对话记录：\n{transcript or '（无对话记录）'}",
            )
        except Exception:
            logger.exception("主观偏好抽取失败，job=%s", job_name)
            return empty_traits()

        return _normalize(data, check_items)


def empty_traits() -> Dict[str, Any]:
    """失败/无对话时的兜底结构。"""
    return {
        "conversation_traits": [],
        "willing_for_target_job": None,
        "check_results": [],
        "reason": "偏好抽取未完成，需人工复核",
    }


def _normalize(data: Any, check_items: str) -> Dict[str, Any]:
    """清洗模型输出：丢弃非法分类、非布尔取值、超长描述。"""
    if not isinstance(data, dict):
        return empty_traits()

    valid_categories = set(TRAIT_CATEGORIES)
    traits: List[Dict[str, str]] = []

    for item in data.get("conversation_traits") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        content = str(item.get("content", "")).strip()
        if category not in valid_categories or not content:
            continue
        traits.append(
            {
                "category": category,
                "content": content[:60],
                "evidence": str(item.get("evidence", "")).strip()[:80],
            }
        )

    # 同时认新旧两个键：模型偶尔会照着旧 prompt 回 willing_for_main_job。
    # 只做取名兼容，不做语义兼容 —— 两者含义完全一致。
    willing = data.get("willing_for_target_job", data.get("willing_for_main_job"))
    if not isinstance(willing, bool):
        willing = None  # 模型给了字符串/空值一律视为未明确表态

    results: List[Dict[str, Any]] = []
    for item in data.get("check_results") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        accepted = item.get("accepted")
        if text and isinstance(accepted, bool):
            results.append({"text": text[:80], "accepted": accepted})

    return {
        "conversation_traits": traits,
        "willing_for_target_job": willing,
        "check_results": results,
        "reason": str(data.get("reason", "")).strip()[:200],
    }


def trait_summary(traits: List[Dict[str, str]], *, max_items: int = 6) -> str:
    """把偏好数组压成一段文字，喂给备选岗位匹配环节。"""
    if not traits:
        return ""
    parts = [
        f"{TRAIT_CATEGORIES.get(t['category'], t['category'])}：{t['content']}"
        for t in traits[:max_items]
    ]
    return "；".join(parts)


def summarize_traits_json(traits: Dict[str, Any]) -> str:
    """入库用：存成紧凑 JSON。"""
    return json.dumps(traits, ensure_ascii=False)