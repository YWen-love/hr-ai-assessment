"""真实链路冒烟测试（会发起真实模型调用，消耗 API 额度）。

    python scripts/live_smoke.py

用 tests/fixtures 下的脱敏样本跑通八条链路，逐条对应「项目诉求对齐」的验证项：
1. 提供方配置     —— 确认 MiniMax 主线 / 火山方舟备用都能读到
2. 连通性        —— 真实调用一次 JSON
3. 简历解析      —— 两个样本跑 LLM 解析，核对院校类型与经验标签
4. 对话意向抽取  —— 10 轮真实对话样本抽 conversation_traits（不含分数/判决）
5. 静态打分      —— 只读简历标签，验证改意愿不改变分数
6. 终局判定      —— 三种意愿各出什么结论、推不推适配岗位
7. 备选岗位      —— 读 job-config 的 alternate_role_mapping
8. 完整报告      —— 组装层输出静态分 + 对话摘要 + 备选岗推荐

三态约定：明确拒绝 → 不推进 + 推适配岗位；愿意 → 推进 + 不推；
          态度模糊 → 待人工复核 + 推适配岗位（HR 靠它判断）。

需要你本人执行：本仓库不会自动发起带密钥的外呼请求。
"""

import asyncio
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import resume_parser as rp
from app.llm_client import LLMClient
from app.report import assemble_report, render_report_text, should_recommend_alternates
from app.traits import TraitExtractor, trait_summary
from config import settings
from rule_engine import assess, score_objective_layer

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FIXTURES = ROOT / "tests" / "fixtures"
JOB = json.loads(
    (ROOT / "job_configs" / "education_learning_consultant.json").read_text(encoding="utf-8")
)


def line(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


async def main() -> None:
    line("1. 提供方配置")
    client = LLMClient()
    for key, entry in settings.llm_providers().items():
        mark = ">>> 主线" if key == settings.LLM_PROVIDER else "    备用"
        print(f"{mark}  {entry['label']:<10} model={entry['model'] or '(空)':<26} "
              f"key={'已配置' if entry['api_key'] else '未配置'}")
    for issue in client.config_issues():
        print(f"  ! {issue}")

    line("2. 真实调用：连通性 + JSON 解析")
    try:
        probe = await client.chat_json(
            '只输出 JSON：{"ok": true}',
            "确认服务可用。",
        )
        print(f"  JSON 调用成功：{probe}")
    except Exception as exc:
        print(f"  调用失败：{type(exc).__name__}: {str(exc)[:400]}")
        print("  → 后续步骤跳过。请先确认 MINIMAX_MODEL / API_KEY 正确。")
        return

    line("3. 真实解析：两个简历样本")
    parser = rp.ResumeParser()
    parsed = {}
    for filename in ("resume_normal_teacher.txt", "resume_non_teacher.txt"):
        text = (FIXTURES / filename).read_text(encoding="utf-8")
        tags = await parser.parse(text, JOB)
        parsed[filename] = tags
        print(f"\n  --- {filename} ---")
        print(f"  解析来源   : {tags['parse_source']}")
        print(f"  姓名/学历  : {tags['name']} / {tags['degree']}")
        print(f"  专业/院校  : {tags['major']} / {tags['school_name']}")
        print(f"  求职意向   : {tags['resume_intention']}")
        print(f"  院校类型   : {tags['school_type']}")
        print(f"  依据       : {tags.get('school_type_basis')}")
        print(f"  经验标签   : {tags['exp_tags']}")
        print(f"  标签佐证   : {tags.get('exp_tag_evidence')}")
        print(f"  风险提示   : {tags['risk_flags']}")
        if tags["parse_warnings"]:
            print(f"  解析警告   : {tags['parse_warnings']}")

    print("\n  预期对照：")
    print("    样本1 洛阳师范学院 → 公办=True 师范院校=True 一本=False")
    print("    样本2 河南工业大学 → 公办=True 师范院校=False 一本=True")

    line("4. 真实抽取：10 轮对话样本的 conversation_traits")
    dialogue = (FIXTURES / "dialogue_willing_false.txt").read_text(encoding="utf-8")
    traits = await TraitExtractor().extract(dialogue, JOB)
    print(f"  willing_for_target_job : {traits['willing_for_target_job']}   （预期 False）")
    print(f"  判定依据             : {traits['reason']}")
    print("  偏好明细：")
    for item in traits["conversation_traits"]:
        print(f"    - {item['category']}: {item['content']}   佐证：{item['evidence']}")
    print(f"  摘要: {trait_summary(traits['conversation_traits']) or '（空）'}")
    print("  要点确认：")
    for item in traits["check_results"]:
        print(f"    - {item['text']}: {item['accepted']}")
    print("  注：traits 只输出意愿与偏好，不含分数/档位/判决 —— 见第 6 段校验")

    line("5. 静态打分（只读简历标签，不含任何对话信息）")
    for filename, tags in parsed.items():
        score = score_objective_layer(JOB, tags)
        print(f"  {filename}: {score['total_score']}/{score['max_score']} -> {score['match_level']}")
        for row in score["score_breakdown"]:
            print(f"      {row['direction']}{row['score']}分 | {row['desc']}"
                  f"（{row['field']} = {row['actual_value']}）")

    print("\n  同一份简历，意愿取 True / False / None：静态分必须一模一样")
    baseline = None
    for willing in (True, False, None):
        score = score_objective_layer(JOB, parsed["resume_non_teacher.txt"])
        if baseline is None:
            baseline = score
        same = score["total_score"] == baseline["total_score"] and score["match_level"] == baseline["match_level"]
        print(f"    意愿={str(willing):<5} -> {score['total_score']} 分 {score['match_level']}"
              f"   {'✓ 静态分不受对话影响' if same else '✗ 静态分被对话污染了'}")
    print("  （score_objective_layer 的签名里就没有对话参数，结构上无从读取）")

    line("6. 终局结论优先跟随意愿")
    willing = traits["willing_for_target_job"]
    print(f"  抽取到的意愿：{willing}")
    for filename, tags in parsed.items():
        result = assess(JOB, tags, willing)
        print(f"    {filename}: {result['match_level']} + 意愿={willing} -> {result['final_decision']}")

    # 静态高匹配对照组：同一份画像，只改意愿，看结论怎么变
    high = {
        "resume_intention": "课程顾问",
        "major": "市场营销",
        "exp_tags": ["课程顾问", "招生"],
        "school_type": {"is_normal_university": False, "is_985_211": True,
                        "is_first_batch": True, "is_public_school": True},
    }
    print("\n  三种意愿各自的结论（静态分固定为同一份高匹配画像）：")
    for w in (False, True, None):
        case = assess(JOB, high, w)
        gate, why = should_recommend_alternates(w, JOB.get("backup_show_switch"))
        roles = "、".join(x["backup_job_name"] for x in JOB.get("alternate_role_mapping") or [])
        shown = roles if gate else "（不推）"
        print(f"    意愿={str(w):<5} -> 静态{case['total_score']}分/{case['match_level']}"
              f"  终局={case['final_decision']:<8} 适配岗位：{shown}")
        print(f"                        {why}")

    line("7. 备选岗位推荐（读 job-config 的 alternate_role_mapping）")
    print(f"  岗位配置里的映射条目：{len(JOB.get('alternate_role_mapping') or [])}")
    for entry in JOB.get("alternate_role_mapping") or []:
        print(f"    - {entry.get('backup_job_name')}: {entry.get('match_condition')}")

    # 真实跑一遍备选岗判定，拿到逐条 match_points。
    # 不跑的话第 8 段的匹配点区块永远看不到真实模型给的东西。
    from app.assessment_agent import AssessmentAgent  # 延迟导入：前面几段没用到
    hits = await AssessmentAgent().match_alternate_role_mapping(
        JOB, parsed["resume_normal_teacher.txt"], traits["conversation_traits"]
    )
    print(f"\n  真实判定命中的备选岗：{[h['backup_job_name'] for h in hits] or '（无）'}")
    for hit in hits:
        print(f"    - {hit['backup_job_name']}：{hit['reason']}")
        for point in hit.get("match_points") or []:
            print(f"        {point.get('condition')} -> hit={point.get('hit')} "
                  f"依据={point.get('evidence', '')[:60]}")

    line("8. 完整报告（组装层输出：静态分 + 意愿 + 对话摘要 + 备选岗匹配点）")
    for label, willing in (("明确拒绝", False), ("态度模糊", None)):
        report = assemble_report(
            job_config=JOB,
            objective={**assess(JOB, high, willing), "resume_tags": high},
            traits={**traits, "willing_for_target_job": willing},
            alternate_roles=hits,
            candidate_name=f"对照组候选人（{label}）",
        )
        print(render_report_text(report))
        print("\n" + "-" * 70)


if __name__ == "__main__":
    asyncio.run(main())