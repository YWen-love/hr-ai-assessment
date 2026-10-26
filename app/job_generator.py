"""JD → 岗位配置生成。

对应定稿决策 2：**AI 只负责高置信度的字段**。

- 高置信度（模型直接从 JD 抄得出来）：job_profile、exp_keywords、dialog_check_items
- 低置信度（需要业务判断）：score_rules、level_threshold

因此生成结果一律 review_status=pending，用来标记这份规则来自模型而非人工维护。
注意：当前测评链路**不会**读取这个字段，pending 的配置一样能直接跑测评 ——
它只是溯源信息，不是一道闸门。
模型给出的分数规则会经过引擎校验（字段是否在封闭词表内、op 与字段类型
是否匹配、分值是否合法），但阈值和分值是业务口径，不是从 JD 里能推导出来的东西，
是否合用仍要人来判断。

生成流程：
    JD 原文 ──LLM──> 原始 JSON ──normalize_job_config──> 补缺省字段
                ──validate_job_config(strict=False)──> errors / warnings
                ──预览返回 HR（不落库）
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from prompts import JD_TO_CONFIG_SYSTEM_PROMPT
from app.job_schema import (
    backup_table_for_prompt,
    count_backups_without_conditions,
    field_table_for_prompt,
    normalize_job_config,
    operator_table_for_prompt,
    score_rule_table_for_prompt,
    split_issues,
    validate_job_config,
)
from app.llm_client import LLMClient

logger = logging.getLogger(__name__)

# JD 生成一次要吐整份岗位配置，实测 MiniMax-M3.1-Flash 单次要 150s 上下。
# 默认 60s 超时会让每次生成都先烧掉一次超时、再降级到备用提供方，
# 白白翻倍耗时。这里按实测值给足。
GENERATION_TIMEOUT = 240.0


class JobGenerationError(RuntimeError):
    """JD 无法解析成配置。errors 直接展示给 HR。"""


# 打分规则的默认模板。AI 可以改，但这里给一个结构完整、语义中性的骨架，
# 避免模型漏字段导致整条配置校验不过。
DEFAULT_SCORE_RULES: Dict[str, Any] = {
    "add_score": [
        {
            "id": "intent_match",
            "field": "resume_intention",
            "op": "contains_any",
            "value": [],
            "score": 2,
            "desc": "求职意向与本岗位方向一致",
        },
        {
            "id": "related_experience",
            "field": "exp_tags",
            "op": "has_any",
            "value": [],
            "score": 3,
            "desc": "有与本岗位直接相关的实习或工作经历",
        },
        {
            "id": "degree_meet",
            "field": "degree",
            "op": "in",
            "value": [],
            "score": 1,
            "desc": "学历达到岗位要求",
        },
    ],
    "minus_score": [],
}

DEFAULT_THRESHOLD = {"high_min": 5, "mid_min": 3}


class JobConfigGenerator:
    def __init__(self):
        self.client = LLMClient(temperature=0.2, timeout=GENERATION_TIMEOUT)

    async def generate(
        self,
        jd_text: str,
        *,
        job_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把 JD 转成岗位配置。**只返回预览，不落库。**

        返回 {config, errors, warnings} —— errors 非空时配置不可保存，
        warnings 表示可保存但建议人工确认。
        """
        text = (jd_text or "").strip()
        if not text:
            raise JobGenerationError("JD 内容为空")

        system_prompt = (
            JD_TO_CONFIG_SYSTEM_PROMPT
            .replace("{{field_table}}", field_table_for_prompt())
            .replace("{{operator_table}}", operator_table_for_prompt())
            .replace("{{backup_table}}", backup_table_for_prompt())
            .replace("{{score_rule_table}}", score_rule_table_for_prompt())
        )

        try:
            raw = await self.client.chat_json(
                system_prompt, f"岗位 JD：\n{text}"
            )
        except Exception as exc:
            logger.exception("JD 生成失败")
            raise JobGenerationError(f"模型未能生成配置：{exc}") from exc

        if not isinstance(raw, dict):
            raise JobGenerationError("模型返回的不是 JSON 对象")

        config = _build_config(raw, jd_hint=text, job_key=job_key)
        errors, warnings = _validate(config)

        # 模型没给命中条件的备选岗位，规范化阶段会按岗位名兜一条粗条件。
        # 这属于我们替模型补的，得让 HR 知道去细化。
        derived = count_backups_without_conditions(raw.get("alternate_role_mapping"))
        if derived:
            warnings.append(
                f"[警告] 模型返回的 {derived} 个备选岗位缺少命中条件，"
                "已按岗位名自动补了一条粗条件，建议在预览面板里细化后再保存"
            )

        return {"config": config, "errors": errors, "warnings": warnings}


def _build_config(raw: Dict[str, Any], *, jd_hint: str, job_key: Optional[str]) -> Dict[str, Any]:
    """把模型输出收敛成合法配置。

    逐个字段兜底：模型漏了必填项就直接失败是不够的，
    能从 JD 原文里正则取到的（岗位名称）先补上，再给 HR 看。
    """
    config: Dict[str, Any] = {
        "job_key": job_key or raw.get("job_key") or _guess_job_key(
            raw.get("job_name") or _guess_job_name(jd_hint)
        ),
        "job_name": raw.get("job_name") or _guess_job_name(jd_hint),
        "job_profile": raw.get("job_profile") or {},
        "dialog_check_items": raw.get("dialog_check_items") or [],
        "exp_keywords": [k for k in (raw.get("exp_keywords") or []) if isinstance(k, str)],
        "score_rules": _merge_score_rules(raw.get("score_rules")),
        "level_threshold": raw.get("level_threshold") or dict(DEFAULT_THRESHOLD),
        "alternate_role_mapping": [b for b in (raw.get("alternate_role_mapping") or []) if isinstance(b, dict)],
        "backup_show_switch": {"when_reject_main_job": True},
        # 定稿决策 2：AI 生成一律待复核
        "review_status": "pending",
        "source": "auto_llm",
    }

    # rag_doc_id 默认与 job_key 同名，一岗一库，满足知识库隔离约定
    config.setdefault("rag_doc_id", config["job_key"])

    return normalize_job_config(config)


def _merge_score_rules(raw: Any) -> Dict[str, Any]:
    """模型的规则与默认模板合并。

    模型给了就用模型的；某条规则缺字段时用模板里的同 id 规则补结构，
    避免模型漏写 score 或 desc 导致整条配置校验不过。

    模型有时把 score_rules 直接吐成一个扁平数组（不分 add/minus 分组）。
    这种按「全是加分项」处理 —— 直接当 dict 看待会走「类型不对 →
    返回占位模板」的分支，模型写的规则被整份丢掉，只剩一组 value 为空、
    永远不会命中的占位规则，岗位存进库也是死的。
    """
    if isinstance(raw, list):
        raw = {"add_score": raw}
    if not isinstance(raw, dict):
        return json.loads(json.dumps(DEFAULT_SCORE_RULES))

    merged: Dict[str, Any] = {}
    for group in ("add_score", "minus_score"):
        entries = raw.get(group)
        if not isinstance(entries, list):
            entries = []
        defaults = {
            item["id"]: item for item in DEFAULT_SCORE_RULES[group]
        }
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fallback = defaults.get(entry.get("id"), {})
            combined = {**fallback, **entry}
            # value 为空时保留模板占位，交由校验器拦下并提示人工填
            if not combined.get("value") and fallback.get("value") is not None:
                combined["value"] = []
            cleaned.append(combined)
        merged[group] = cleaned

    if not merged["add_score"]:
        merged["add_score"] = json.loads(json.dumps(DEFAULT_SCORE_RULES["add_score"]))
    return merged


def _validate(config: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """校验。errors 必须修，warnings 建议人工确认。

    走的是和保存入库同一个校验器，同一套分段口径 ——
    预览说「能存」的结果如果被保存拦下，HR 会以为页面坏了。
    """
    errors, warnings = split_issues(validate_job_config(config))

    warnings.extend(soft_gap_warnings(config))
    warnings.append(
        "AI 生成的配置需人工复核：score_rules 与 level_threshold 属业务口径，"
        "确认分值与阈值后再标记为已复核"
    )
    return errors, warnings


# is_true / is_false / is_empty 不读 value，缺 value 是合法的；
# 其余操作符的 value 为空 = 这条规则永远不会命中，必须让人看见。
_VALUE_FREE_OPS = {"is_true", "is_false", "is_empty"}


def soft_gap_warnings(config: Dict[str, Any]) -> List[str]:
    """宽松校验放行、但会让配置悄悄失效的坑。

    公开入口：HR 在后台改完配置点「校验」时走同一套提示，
    免得生成时看到过的告警、人工改完就再也看不到了。
    """
    warnings: List[str] = []

    rules = config.get("score_rules") or {}

    keywords = config.get("exp_keywords") or []
    uses_exp_tags = any(
        isinstance(r, dict) and r.get("field") == "exp_tags"
        for group in ("add_score", "minus_score")
        for r in (rules.get(group) or [])
    )
    # 词表为空时 detect_exp_tags 直接返回 []，所有 exp_tags 规则静默失效
    if uses_exp_tags and not keywords:
        warnings.append(
            "[警告] exp_keywords 为空，但打分规则里用到了 exp_tags："
            "简历解析出不了任何经验标签，这些规则永远不会加分"
        )
    if not keywords:
        warnings.append("[警告] exp_keywords 为空，该岗位无法从简历提取经验标签")

    for group in ("add_score", "minus_score"):
        for index, rule in enumerate(rules.get(group) or []):
            if not isinstance(rule, dict):
                continue
            if rule.get("op") in _VALUE_FREE_OPS:
                continue
            value = rule.get("value")
            if isinstance(value, (list, dict, str)) and not value:
                warnings.append(
                    f"[警告] score_rules.{group}[{index}]（{rule.get('id') or '未命名'}）"
                    f"的 value 为空，这条规则永远不会命中"
                )

    thresholds = config.get("level_threshold") or {}
    # 满分只看加分项：扣分项只会把分数往下拉，算进去会把上限抬高，
    # 明明够不到的档位就查不出来了。口径与 score_rules_for 一致。
    max_score = sum(
        int(r.get("score", 0))
        for r in (rules.get("add_score") or [])
        if isinstance(r, dict)
        and isinstance(r.get("score"), int)
        and not isinstance(r.get("score"), bool)
    )
    high_min = thresholds.get("high_min")
    # 满分都够不到高匹配档 = 这套规则实际只有两档用
    if isinstance(high_min, int) and not isinstance(high_min, bool) and max_score < high_min:
        warnings.append(
            f"[警告] 所有加分项满分之和 {max_score} 小于 high_min({high_min})，"
            f"「高匹配」档将无法达到"
        )
    mid_min = thresholds.get("mid_min")
    if isinstance(mid_min, int) and not isinstance(mid_min, bool) and max_score < mid_min:
        warnings.append(
            f"[警告] 所有加分项满分之和 {max_score} 小于 mid_min({mid_min})，"
            f"「中匹配」档将无法达到"
        )

    for index, backup in enumerate(config.get("alternate_role_mapping") or []):
        if isinstance(backup, dict) and not backup.get("match_condition"):
            warnings.append(
                f"[警告] alternate_role_mapping[{index}] 没有 match_condition，备选岗位匹配无从判断"
            )

    return warnings


def _guess_job_name(jd_text: str) -> str:
    """从 JD 原文猜岗位名。取不到就让 HR 自己填，不瞎编。"""
    match = re.search(r"(?:岗位名称|职位名称|职位|岗位)\s*[:：]\s*([^\n\r，,。;；]{2,20})", jd_text)
    return match.group(1).strip() if match else ""


def _guess_job_key(job_name: str) -> str:
    """岗位名转 job_key。

    中文无法直接转写为合法 key（只允许小写字母数字下划线），
    这时回退到 jd_<时间戳>，保证 key 合法 —— 具体语义留给 HR 在预览面板里改。
    """
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (job_name or "").lower()).strip("_")
    if cleaned and not cleaned[0].isdigit():
        return cleaned
    return f"jd_{int(time.time())}"