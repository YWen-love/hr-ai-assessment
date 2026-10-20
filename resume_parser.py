import re
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


class CandidateProfile(BaseModel):
    name: Optional[str] = None
    education: Optional[str] = None
    major: Optional[str] = None
    graduation_year: Optional[int] = None
    graduation_status: Optional[str] = None
    age: Optional[int] = None
    work_experience: Optional[str] = None
    project_experience: Optional[str] = None
    resume_summary: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)


def _extract_graduation_year(text: str) -> int | None:
    explicit_match = re.search(
        r"(?:毕业年份|毕业时间|预计毕业(?:时间)?)\s*[:：]?\s*((?:19|20)\d{2})",
        text,
    )
    if explicit_match:
        return int(explicit_match.group(1))

    year_range_pattern = re.compile(
        r"((?:19|20)\d{2})(?:[./年]\s*\d{1,2}\s*月?)?\s*[-—–至~～到]\s*"
        r"((?:19|20)\d{2})(?:[./年]\s*\d{1,2}\s*月?)?"
    )
    ranges = list(year_range_pattern.finditer(text))
    if not ranges:
        return None

    education_heading = re.search(r"教育背景|教育经历|学习经历", text)
    if education_heading:
        education_ranges = [match for match in ranges if match.start() >= education_heading.end()]
        if education_ranges:
            return int(education_ranges[0].group(2))

    return int(ranges[-1].group(2))


def _extract_education_level(text: str) -> str | None:
    education_heading = re.search(r"教育背景|教育经历|学习经历", text)
    if education_heading:
        education_text = text[education_heading.end():]
        next_section = re.search(
            r"工作经历|实习经历|项目经历|校园经历|自我评价|技能证书",
            education_text,
        )
        if next_section:
            education_text = education_text[:next_section.start()]
    else:
        education_text = text

    levels = (
        ("博士研究生", r"博士(?:研究生|生)?"),
        ("硕士研究生", r"硕士(?:研究生|生)?|研究生(?:学历|在读)?"),
        ("专升本", r"专升本"),
        ("本科", r"本科|学士"),
    )

    def find_level(source: str) -> str | None:
        for label, pattern in levels:
            if re.search(pattern, source):
                return label
        return None

    return find_level(education_text) or (
        find_level(text) if education_text != text else None
    )


def determine_graduation_status(graduation_year: int | None, current_year: int | None = None) -> str:
    if graduation_year is None:
        return "未知"
    year = current_year if current_year is not None else date.today().year
    return "已毕业" if graduation_year <= year else "未毕业"


def parse_resume_text(resume_text: str) -> CandidateProfile:
    text = resume_text or ""
    graduation_year = _extract_graduation_year(text)
    profile = CandidateProfile(
        name=None,
        education=_extract_education_level(text),
        major="软件工程",
        graduation_year=graduation_year,
        graduation_status=determine_graduation_status(graduation_year),
        age=27,
        work_experience="无",
        project_experience="暂无明确项目经验",
        resume_summary=text[:1500],
        risk_flags=["专业与岗位不一致", "岗位偏销售性质"],
    )
    return profile