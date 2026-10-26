"""简历解析：把简历原文抽成打分层可用的结构化标签。

两层标签体系（定稿要求）：
- **基础层**：学历、专业、院校类型、求职意向 —— 与岗位无关，任何岗位共用同一套字段
- **岗位层**：`exp_tags` —— 完全由 job_config 的 exp_keywords 决定，换岗位就换词表

这里最关键的一条：`exp_tags` 是**封闭词表**，代码强制过滤。
模型自由发挥出来的标签一律丢弃 —— 简历解析一旦允许自造标签，
后面的 DSL 打分就是在对着幻觉算分，测评结果当场失去可复现性。

exp_tags 走双通道取交集之外的并集：
1. 代码确定性匹配（bigram 命中 exp_keywords）—— 召回有下限，模型挂了也有标签
2. 模型语义选择（必须落在同一封闭词表内）—— 补上字面不同但语义一致的情况

旧实现的问题：parse_resume_text 对任何简历都返回 major="软件工程"、age=27、
work_experience="无"。那不是解析，那是伪造。现已删除全部硬编码字段。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

from app.llm_client import LLMClient
from app.text_match import find_hits, normalize

logger = logging.getLogger(__name__)

# 打分层读取的院校字段，顺序即 prompt 里的输出顺序
SCHOOL_TYPE_KEYS = (
    "is_normal_university",  # 师范类院校
    "is_985_211",            # 985 / 211
    "is_first_batch",        # 原一本 / 双一流
    "is_public_school",      # 公办
)

_DEGREE_LEVELS = (
    ("博士研究生", r"博士(?:研究生|生)?"),
    ("硕士研究生", r"硕士(?:研究生|生)?"),
    ("专升本", r"专升本"),
    ("本科", r"本科|学士"),
    ("大专", r"大专|专科|高职"),
)

_SECTION_STOP = r"工作经历|实习经历|项目经历|校园经历|自我评价|技能证书|获奖经历"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

RESUME_PARSE_SYSTEM_PROMPT = """
你是简历结构化解析器。把候选人提交的简历原文，抽取成打分层可用的结构化标签。
只输出 JSON，不要解释文字，不要代码围栏，不要输出 JSON 以外的任何内容。

【字段表】（字段名必须一字不差，不要新增顶层字段）
{
  "name": "候选人姓名；简历里没写就填 null",
  "degree": "最高学历，取值用 专科/本科/硕士研究生/博士研究生；没写填 null",
  "major": "专业名称，照抄简历原文（如 汉语言文学）；没写填 null",
  "school_name": "毕业院校全称，照抄简历原文；没写填 null",
  "graduation_year": "毕业年份，整数；在读就填预计毕业年份；没写填 null",
  "resume_intention": "简历上写的求职意向原文，照抄；没写填 null",
  "work_experience": "工作与实习经历的一句话概括；没有就填 null",
  "school_type": {
    "is_normal_university": false,
    "is_985_211": false,
    "is_first_batch": false,
    "is_public_school": false
  },
  "school_type_basis": "判定院校类型的依据，逐条说明哪个字段让你这么判；判不出来填 null",
  "exp_tag_evidence": [
    {"tag": "从下方封闭词表里选的标签", "evidence": "简历里支撑这个标签的原话片段"}
  ]
}

【经验标签封闭词表】（exp_tag_evidence 的 tag 只能从这里选，一字不差）
{{keyword_table}}

【判定规则】
1. 凡是简历里没有的信息，一律填 null 或 false。【严禁编造】，包括不要根据学校名
   之外的因素推测，不要因为"看起来像"就填。
2. exp_tag_evidence 只输出简历里有明确依据的标签。同一段经历命中多个标签就都列。
   evidence 必须能在简历原文里逐字找到。不确定就不输出这一条。
3. school_type 的四个布尔值：
   - is_normal_university：师范类院校（含独立学院的师范专业院校判断依据见 basis）
   - is_985_211：仅当 school_name 是公认的 985 或 211 院校时填 true，拿不准填 false
   - is_first_batch：原一本批次院校
   - is_public_school：公办院校
   只要有一条你不能说出具体依据，就在 school_type_basis 里写明"存疑"。
   school_type 直接参与打分，错判会直接影响档位，宁可全部 false 让人工补。
4. 不要评价候选人，不要给建议，不要输出打分相关内容。

【输出示例】（仅示意形状，示例内容与你解析的简历无关）
{{shape_example}}
"""


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------

def empty_tags() -> Dict[str, Any]:
    """空标签。字段名与 job_schema.FIELD_TYPES 严格对应，缺失即 None。"""
    return {
        "name": None,
        "degree": None,
        "major": None,
        "school_name": None,
        "graduation_year": None,
        "graduation_status": None,
        "resume_intention": None,
        "work_experience": None,
        "school_type": {key: False for key in SCHOOL_TYPE_KEYS},
        "exp_tags": [],
        "exp_tag_evidence": {},
        "resume_summary": "",
        "risk_flags": [],
        "parse_source": "none",
        "parse_warnings": [],
    }


# ---------------------------------------------------------------------------
# 纯本地解析（不依赖 LLM）—— 同时是 LLM 结果的补漏与兜底
# ---------------------------------------------------------------------------

def _section(text: str, *headings: str) -> str:
    """截取某个小节正文。找不到标题时返回空串。"""
    match = re.search("|".join(headings), text)
    if not match:
        return ""
    body = text[match.end():]
    stop = re.search(_SECTION_STOP, body)
    return body[: stop.start()] if stop else body


def _extract_graduation_year(text: str) -> Optional[int]:
    explicit = re.search(r"(?:毕业年份|毕业时间|预计毕业(?:时间)?)\s*[:：]?\s*((?:19|20)\d{2})", text)
    if explicit:
        return int(explicit.group(1))

    ranges = list(re.finditer(
        r"((?:19|20)\d{2})(?:[./年]\s*\d{1,2}\s*月?)?\s*[-—–至~～到]\s*"
        r"((?:19|20)\d{2})(?:[./年]\s*\d{1,2}\s*月?)?",
        text,
    ))
    if not ranges:
        return None

    heading = re.search(r"教育背景|教育经历|学习经历", text)
    if heading:
        scoped = [m for m in ranges if m.start() >= heading.end()]
        if scoped:
            return int(scoped[0].group(2))
    return int(ranges[-1].group(2))


def _extract_degree(text: str) -> Optional[str]:
    for source in (_section(text, r"教育背景|教育经历|学习经历"), text):
        for label, pattern in _DEGREE_LEVELS:
            if re.search(pattern, source):
                return label
    return None


def _extract_intention(text: str) -> Optional[str]:
    """抽取求职意向。

    这是打分层字段（resume_intention），规则里经常拿它判意向是否对口。
    本地路径抽不出来的话，LLM 一挂意向类加分项就静默失效 ——
    表现为"分数莫名其妙偏低"，很难排查，所以这里必须补上。
    """
    match = re.search(
        r"(?:求职意向|应聘岗位|意向岗位|应聘职位|求职岗位|期望岗位)\s*[:：]\s*([^\n\r，,。;；]{1,30})",
        text,
    )
    return match.group(1).strip() or None if match else None


def determine_graduation_status(graduation_year: int | None, current_year: int | None = None) -> str:
    if graduation_year is None:
        return "未知"
    year = current_year if current_year is not None else date.today().year
    return "已毕业" if graduation_year <= year else "未毕业"


def detect_exp_tags(resume_text: str, exp_keywords: List[str]) -> List[str]:
    """代码侧确定性标签识别。

    拿 exp_keywords 逐个在简历原文里找字面依据。这条通道不经过模型，
    因此模型不可用、超时、返回垃圾时，标签体系依然能工作。

    匹配只认整词（allow_partial=False）。原因见 app/text_match.has_trace 的
    文档：放宽到 bigram 会让 "课程顾问/电话销售/教育培训" 在一份小学教师
    简历上全部命中，白送 3 分。宁可少命中，让 LLM 通道去补。
    """
    if not exp_keywords:
        return []
    haystack = normalize(resume_text or "")
    return find_hits(
        [k for k in exp_keywords if isinstance(k, str)],
        haystack,
        allow_partial=False,
    )


def parse_resume_text(resume_text: str, job_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """纯本地解析，不调用模型。

    保留给同步调用方（旧 UI 路径）与 LLM 失败兜底。只做确定性能做的事：
    学历、毕业年份、专业关键词这类正则可靠抽取的字段，其余一律留空。
    """
    text = resume_text or ""
    tags = empty_tags()

    year = _extract_graduation_year(text)
    tags["graduation_year"] = year
    tags["graduation_status"] = determine_graduation_status(year)
    tags["degree"] = _extract_degree(text)
    tags["resume_intention"] = _extract_intention(text)
    tags["resume_summary"] = text[:2000]
    tags["exp_tags"] = detect_exp_tags(text, _config_keywords(job_config))
    tags["parse_source"] = "local"

    if not tags["exp_tags"]:
        tags["parse_warnings"].append("本地解析未命中任何经验标签，经验类加分项将不生效")
    return tags


# ---------------------------------------------------------------------------
# LLM 解析（主路径）
# ---------------------------------------------------------------------------

class ResumeParser:
    def __init__(self):
        self.client = LLMClient(temperature=0.0)

    async def parse(
        self,
        resume_text: str,
        job_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """解析简历。失败时降级到本地解析，绝不抛出阻断测评。"""
        text = resume_text or ""
        if not text.strip():
            tags = empty_tags()
            tags["parse_warnings"].append("简历原文为空，无法解析")
            return tags

        keywords = _config_keywords(job_config)
        # 本地先跑一遍：既是 LLM 失败时的兜底，也用来给模型做交叉校验
        baseline = parse_resume_text(text, job_config)

        system_prompt = (
            RESUME_PARSE_SYSTEM_PROMPT
            .replace("{{keyword_table}}", _keyword_table(keywords))
            .replace("{{shape_example}}", _shape_example())
        )

        try:
            raw = await self.client.chat_json(system_prompt, f"简历原文：\n{text}")
        except Exception:
            logger.exception("简历解析失败，降级为本地解析")
            baseline["parse_warnings"].append("模型解析失败，已降级为本地解析，结果不完整")
            baseline["parse_source"] = "local_fallback"
            return baseline

        return _merge(raw, text, keywords, baseline)


def _config_keywords(job_config: Optional[Dict[str, Any]]) -> List[str]:
    if not job_config:
        return []
    return [k for k in (job_config.get("exp_keywords") or []) if isinstance(k, str) and k.strip()]


def _keyword_table(keywords: List[str]) -> str:
    if not keywords:
        return "（该岗位未配置经验关键词，exp_tag_evidence 输出空数组）"
    return "\n".join(f"- {k}" for k in keywords)


def _shape_example() -> str:
    return (
        '{"name": null, "degree": "本科", "major": null, "school_name": null, '
        '"graduation_year": 2025, "resume_intention": null, "work_experience": null, '
        '"school_type": {"is_normal_university": false, "is_985_211": false, '
        '"is_first_batch": false, "is_public_school": false}, '
        '"school_type_basis": null, "exp_tag_evidence": []}'
    )


def _merge(
    raw: Any,
    resume_text: str,
    keywords: List[str],
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    """清洗模型输出并与本地结果合并。

    每一处赋值都做了类型/闭集校验。模型给什么都不能突破下面几道闸：
    - exp_tags 必须落在岗位配置的封闭词表内
    - school_type 只认四个已知 key，其余丢弃
    - graduation_year 只认整数
    """
    if not isinstance(raw, dict):
        return _degraded(baseline, "模型返回的不是 JSON 对象，已降级为本地解析")

    tags = empty_tags()
    warnings: List[str] = baseline["parse_warnings"]
    tags["resume_summary"] = (raw.get("resume_summary") or baseline["resume_summary"] or resume_text)[:2000]

    for field_name in ("name", "degree", "major", "school_name", "resume_intention", "work_experience"):
        value = _clean_str(raw.get(field_name))
        # 模型缺失时用本地抽取的补上，两个来源都没有就留 None
        tags[field_name] = value or baseline.get(field_name)

    year = _clean_year(raw.get("graduation_year")) or baseline.get("graduation_year")
    tags["graduation_year"] = year
    tags["graduation_status"] = determine_graduation_status(year)

    tags["school_type"], type_basis = _clean_school_type(raw.get("school_type"))
    basis = _clean_str(raw.get("school_type_basis"))
    if basis:
        tags["school_type_basis"] = basis
    elif type_basis:
        tags["school_type_basis"] = "模型仅返回了布尔值，未给出判定依据，建议人工复核"
        warnings.append("院校类型缺少判定依据，已标记建议人工复核")

    tags["exp_tags"], tags["exp_tag_evidence"] = _merge_exp_tags(
        raw.get("exp_tag_evidence"), keywords, resume_text
    )

    tags["risk_flags"] = _risk_flags(tags)
    tags["parse_source"] = "llm"
    tags["parse_warnings"] = warnings
    return tags


def _degraded(baseline: Dict[str, Any], message: str) -> Dict[str, Any]:
    tags = dict(baseline)
    tags["parse_source"] = "local_fallback"
    tags["parse_warnings"] = list(baseline.get("parse_warnings") or []) + [message]
    return tags


def _clean_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    text = str(value).strip()
    # 模型常用这些词表示"没有"，比 null 更容易被下游误当成真实取值
    if not text or text in {"无", "未知", "不详", "N/A", "null", "无相关经验", "暂无", "没有"}:
        return None
    return text


def _clean_year(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1950 <= value <= date.today().year + 10 else None
    if isinstance(value, str):
        match = re.search(r"(19|20)\d{2}", value)
        if match:
            year = int(match.group(0))
            return year if 1950 <= year <= date.today().year + 10 else None
    return None


def _clean_school_type(value: Any) -> tuple[Dict[str, bool], bool]:
    """只保留四个已知布尔键；模型多吐的字段一律丢弃。"""
    result = {key: False for key in SCHOOL_TYPE_KEYS}
    if not isinstance(value, dict):
        return result, False
    for key in SCHOOL_TYPE_KEYS:
        result[key] = value.get(key) is True
    # 布尔值存在不等于有依据，交由调用方决定是否提示复核
    return result, any(result.values())


def _merge_exp_tags(
    raw_entries: Any,
    keywords: List[str],
    resume_text: str,
) -> tuple[List[str], Dict[str, str]]:
    """代码通道 + 模型通道求并集，标签必须落在岗位封闭词表内。"""
    allowed = {k for k in keywords if isinstance(k, str) and k.strip()}
    evidence: Dict[str, str] = {}

    # 通道 1：模型选择，但必须逐字命中封闭词表
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            tag = (entry.get("tag") or "").strip()
            if tag not in allowed:
                continue  # 幻觉标签直接丢弃
            evidence.setdefault(tag, _clean_str(entry.get("evidence")) or "")

    # 通道 2：代码确定性匹配，兜底 + 补召回
    for tag in detect_exp_tags(resume_text, list(allowed)):
        evidence.setdefault(tag, "")

    return sorted(evidence), {tag: text for tag, text in evidence.items() if text}


def _risk_flags(tags: Dict[str, Any]) -> List[str]:
    """需要 HR 重点看一眼的情况。只做客观事实提示，不做评价。"""
    flags: List[str] = []
    if not tags.get("degree"):
        flags.append("简历未识别到学历，需人工确认")
    if not tags.get("school_name"):
        flags.append("简历未识别到毕业院校，院校类规则无法生效")
    if not tags.get("exp_tags"):
        flags.append("未识别到任何经验标签，经验类加分项不生效")
    if tags.get("graduation_status") == "未毕业":
        flags.append("候选人尚未毕业，需确认实习/校招意向")
    return flags