"""看模型对 score_rules 到底返回了什么（生成流程里的 raw JSON）。"""
import asyncio, json, sys
sys.stdout.reconfigure(encoding="utf-8")
from prompts import JD_TO_CONFIG_SYSTEM_PROMPT
from app.job_schema import (backup_table_for_prompt, field_table_for_prompt,
                            operator_table_for_prompt, score_rule_table_for_prompt)
from app.llm_client import LLMClient

system = (JD_TO_CONFIG_SYSTEM_PROMPT
    .replace("{{field_table}}", field_table_for_prompt())
    .replace("{{operator_table}}", operator_table_for_prompt())
    .replace("{{backup_table}}", backup_table_for_prompt())
    .replace("{{score_rule_table}}", score_rule_table_for_prompt()))

jd = open(".playwright-cli/测试JD_初中数学老师.txt", encoding="utf-8").read()

async def main():
    client = LLMClient(temperature=0.2, timeout=240.0)
    raw = await client.chat_json(system, f"岗位 JD：\n{jd}")
    with open("probe_raw_out.json", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    sr = raw.get("score_rules")
    print("score_rules 类型:", type(sr).__name__)
    if isinstance(sr, dict):
        print("分组:", list(sr.keys()))
        for g in ("add_score", "minus_score"):
            for r in sr.get(g) or []:
                print(f"  {g}: id={r.get('id')!r} field={r.get('field')!r} op={r.get('op')!r} "
                      f"value={r.get('value')!r} score={r.get('score')!r}")
        total = sum(int(r.get("score", 0)) for r in (sr.get("add_score") or []))
    else:
        total = sum(int(r.get("score", 0)) for r in (sr or []) if isinstance(r, dict))
    print("加分项满分之和:", total)
    print("level_threshold:", raw.get("level_threshold"))

asyncio.run(main())
