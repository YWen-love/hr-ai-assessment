from resume_parser import determine_graduation_status


def evaluate_candidate(candidate_summary: dict, job_intro: dict) -> dict:
    result = {
        "graduation_status": "未知",
        "intent_type": "未知",
        "job_acceptance": "未知",
        "work_time_acceptance": "未知",
        "salary_acceptance": "未知",
        "final_decision": "观察",
        "reason_summary": "",
        "not_accept_points": [],
    }

    graduation_status = candidate_summary.get("graduation_status") or determine_graduation_status(
        candidate_summary.get("graduation_year")
    )
    result["graduation_status"] = graduation_status
    if graduation_status == "未毕业":
        result["intent_type"] = "实习"
    elif graduation_status == "已毕业":
        result["intent_type"] = "正式岗位"

    result["job_acceptance"] = "接受"
    result["work_time_acceptance"] = "接受"
    result["salary_acceptance"] = "接受"
    result["reason_summary"] = "候选人基本符合岗位筛选规则，方案已进入推进判断。"

    # 这里是你最重要的扩展点：
    # 1. 如果非师范专业 + 岗位偏销售，则应确认岗位认知和接受度
    # 2. 如果是27届/未毕业，则必须确认实习/转正意向
    # 3. 如果候选人不接受岗位内容、时间或薪资，需按规则输出不推进
    
    return result
