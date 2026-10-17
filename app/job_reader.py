from app.paths import resource_path
from app.knowledge_base import get_job_knowledge, list_jobs


def read_job_intro_for_job(job_name: str) -> str:
    return get_job_knowledge(job_name, require_rules=False).job_intro


def read_prompt_template(path: str) -> str:
    return resource_path(path).read_text(encoding="utf-8")


def retrieve_job_section(file_path: str, job_name: str) -> str:
    del file_path
    return get_job_knowledge(job_name).screening_rules


def list_available_jobs(file_path: str) -> list[str]:
    del file_path
    return list_jobs()
