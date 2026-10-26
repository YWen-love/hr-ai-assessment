"""一键跑全部回归测试。

    python tests/run_all.py

纯本地，不发起任何网络请求。
"""

import subprocess
import sys
from pathlib import Path

TESTS = [
    "test_scoring_engine_generic.py",  # 打分引擎通用性
    "test_resume_parser.py",           # 简历解析 + 标签封闭词表
    "test_e2e_learning_consultant.py", # 真实岗位配置端到端
    "test_traits.py",                  # 对话偏好抽取
    "test_redline.py",                 # 协议标记与红线拦截
    "test_llm_parsing.py",             # 响应解析容错
    "test_job_generator.py",           # JD → 配置生成 + 岗位增删改查
    "test_job_schema_backups.py",      # 备选岗位形状归一化
    "test_job_schema_score_rules.py",  # 打分规则形状归一化
    "test_job_manager_ui.py",          # HR 后台岗位管理 Tab 的 handler
    "test_report.py",                  # 报告组装：静态分 / 意愿 / 推荐
]

HERE = Path(__file__).resolve().parent

failed = []
for name in TESTS:
    result = subprocess.run(
        [sys.executable, str(HERE / name)],
        cwd=str(HERE.parent),
        capture_output=True,
    )
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"  {status}  {name}")
    if result.returncode != 0:
        failed.append((name, result.stdout.decode("utf-8", "replace")))

print()
if failed:
    for name, output in failed:
        print(f"===== {name} =====")
        print(output[-3000:])
    print(f"失败 {len(failed)}/{len(TESTS)}")
    sys.exit(1)

print(f"全部通过（{len(TESTS)} 个测试文件）")