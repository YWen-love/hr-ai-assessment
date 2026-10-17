from __future__ import annotations

from typing import Dict, List, Optional

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


class JobIntro(BaseModel):
    job_name: str
    responsibilities: List[str] = Field(default_factory=list)
    nature: str = ""
    work_time: str = ""
    rest_schedule: str = ""
    salary: Dict[str, str] = Field(default_factory=dict)
    remarks: List[str] = Field(default_factory=list)


class AssessmentSession(BaseModel):
    session_id: str
    candidate_id: str
    status: str = "created"
    current_step: str = "start"
    questions: List[str] = Field(default_factory=list)
    answers: List[str] = Field(default_factory=list)
    final_decision: Optional[str] = None
    summary: Optional[str] = None


class AssessmentResult(BaseModel):
    candidate_id: str
    graduation_status: str
    intent_type: str
    job_acceptance: str
    work_time_acceptance: str
    salary_acceptance: str
    final_decision: str
    reason_summary: str
    not_accept_points: List[str] = Field(default_factory=list)
    conversation_summary: str
