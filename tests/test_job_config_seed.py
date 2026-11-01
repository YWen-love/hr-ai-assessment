"""岗位配置种子化与 JD 回填的回归测试。

背景：岗位管理页面原来查不到 JD —— list_job_configs() 根本没 SELECT raw_jd，
HR 打开后台永远只看到一个空列。同时配置表里的 JD 一直是空的：没有任何
一处代码把 JD 原文写进去过。

现在改成：
  - list_job_configs() 把 raw_jd 查出来给页面显示；
  - seed_from_files() 从 job_intro.md 里对应岗位那一节回填 raw_jd
    （只补空，不覆盖 HR 已经填好的）；
  - 配置文件的 raw_jd 字段优先级最高，且不会被塞进 config_json。

这里守住四条线：
  1. JD 可见：list_job_configs() 必须带出 raw_jd；
  2. JD 有源：新配置能从 job_intro.md 回填出非空 JD；
  3. JD 不丢：HR 已经维护好的 JD，回填不能覆盖；
  4. 两张表对得上：配置表里的岗位名要和 job_intro.md 的岗位段落一致
     —— HR 页面的下拉（知识库）和岗位管理的列表（配置表）才不会各说各话。

不碰真实数据库：job_config_store 的 DB_PATH 猴补丁到临时文件。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.job_config_store as jcs

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


CONFIG_DIR = ROOT / "job_configs"
INTRO_FILE = ROOT / "job_intro.md"

original_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    jcs.DB_PATH = Path(tmp) / "job_configs.db"

    try:
        section("1. 人力实习生的配置文件本身是合法的")

        intern_file = CONFIG_DIR / "hr_recruitment_intern.json"
        check("job_configs/hr_recruitment_intern.json 存在", intern_file.is_file(), str(intern_file))
        raw = json.loads(intern_file.read_text(encoding="utf-8"))
        check("job_key 是 hr_recruitment_intern", raw.get("job_key") == "hr_recruitment_intern", str(raw.get("job_key")))
        check("job_name 是 人力实习生", raw.get("job_name") == "人力实习生", str(raw.get("job_name")))

        from app.job_schema import normalize_job_config, split_issues, validate_job_config

        errors, warnings = split_issues(validate_job_config(normalize_job_config(raw)))
        check("校验没有 error", not errors, str(errors))
        check("校验没有 warning", not warnings, str(warnings))

        section("2. 岗位名要和 job_intro.md 的岗位段落对得上")

        # 知识库的 jobs 表是拿 job_intro.md 的 ===岗位名=== 建的，
        # 配置表这边用的是 job_name。两个名字必须一模一样，
        # 否则 HR 页面的下拉和岗位管理的列表永远对不上。
        from app.knowledge_base import _parse_marked_sections

        sections = _parse_marked_sections(INTRO_FILE.read_text(encoding="utf-8"))
        check("job_intro.md 里有 人力实习生 这一段", "人力实习生" in sections, str(list(sections)))
        check("配置里的岗位名在 job_intro.md 里存在", raw["job_name"] in sections, raw["job_name"])

        section("3. seed_from_files 能把配置和 JD 一起灌进库")

        imported = jcs.seed_from_files(CONFIG_DIR)
        check("三个岗位都被种子化", len(imported) == 3, str(imported))
        check("人力实习生在导入列表里", "hr_recruitment_intern" in imported, str(imported))

        records = jcs.list_job_configs()
        check("list_job_configs 返回三条", len(records) == 3, str(len(records)))
        check("列表里带得出 raw_jd", all("raw_jd" in r for r in records), str(list(records[0])))

        by_key = {r["job_key"]: r for r in records}
        intern = by_key.get("hr_recruitment_intern", {})
        check("人力实习生这一行有 JD", bool((intern.get("raw_jd") or "").strip()), repr(intern.get("raw_jd")))
        check("JD 内容是岗位介绍原文", "协助制定和优化人力资源相关流程与政策" in (intern.get("raw_jd") or ""), repr(intern.get("raw_jd"))[:120])

        for key in ("education_learning_consultant", "education_erjiang"):
            row = by_key.get(key, {})
            check(f"{key} 也回填到了 JD", bool((row.get("raw_jd") or "").strip()), repr(row.get("raw_jd"))[:80])

        section("4. JD 原文不会污染 config_json")

        stored = jcs.get_job_config("hr_recruitment_intern")["config"]
        check("config_json 里没有 raw_jd", "raw_jd" not in stored, str(list(stored)))
        check("config_json 里 job_profile 还在", "job_profile" in stored, str(list(stored)))

        section("5. 回填只补空，不覆盖 HR 已经维护好的内容")

        cfg = dict(stored)
        cfg["job_profile"] = dict(cfg["job_profile"])
        cfg["job_profile"]["salary"] = ["HR 手改过的薪资"]
        jcs.save_job_config(
            cfg,
            source_type="manual",
            review_status="reviewed",
            raw_jd="HR 手改过的 JD 原文",
        )

        again = jcs.seed_from_files(CONFIG_DIR)
        check("重复 seed 不再导入", again == [], str(again))

        kept = jcs.get_job_config("hr_recruitment_intern")
        check("HR 改过的 JD 没被覆盖", kept["raw_jd"] == "HR 手改过的 JD 原文", repr(kept["raw_jd"]))
        check("HR 改过的薪资没被覆盖", kept["config"]["job_profile"]["salary"] == ["HR 手改过的薪资"], str(kept["config"]["job_profile"]["salary"]))

        section("6. JD 是空的会被补上")

        jcs.save_job_config(
            cfg,
            source_type="manual",
            review_status="reviewed",
            raw_jd="",
        )
        jcs.seed_from_files(CONFIG_DIR)
        filled = jcs.get_job_config("hr_recruitment_intern")
        check("空 JD 被补上", bool((filled["raw_jd"] or "").strip()), repr(filled["raw_jd"]))
        check("补上的是同一段 JD", "协助制定和优化人力资源相关流程与政策" in (filled["raw_jd"] or ""), repr(filled["raw_jd"])[:120])

        section("7. 文件里的 raw_jd 优先级最高")

        # 配置文件里显式写了 raw_jd 时，它压过 job_intro.md。
        explicit_dir = Path(tmp) / "explicit"
        explicit_dir.mkdir()
        payload = dict(raw)
        payload["raw_jd"] = "配置文件里写的 JD 原文"
        (explicit_dir / "intern.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        jcs.DB_PATH = Path(tmp) / "explicit.db"
        jcs.seed_from_files(explicit_dir)
        explicit = jcs.get_job_config("hr_recruitment_intern")
        check("文件里的 JD 写进去了", explicit["raw_jd"] == "配置文件里写的 JD 原文", repr(explicit["raw_jd"]))
        check("文件里的 raw_jd 没进 config_json", "raw_jd" not in explicit["config"], str(list(explicit["config"])))
    finally:
        jcs.DB_PATH = original_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
