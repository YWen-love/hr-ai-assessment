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
    "test_job_schema_profile.py",      # job_profile 键名归一化
    "test_job_generator_score_rules.py",  # 打分规则分组名漂移 + 兜底提示
    "test_job_manager_ui.py",          # HR 后台岗位管理 Tab 的 handler
    "test_session_policy.py",          # session cookie 策略 / CSRF 拦截 / 登出宽限期
    "test_report.py",                  # 报告组装：静态分 / 意愿 / 推荐
    "test_candidate_page.py",          # 候选人页：服务端渲染头部 / 不出 undefined / 岗位配置缺失降级
    "test_job_config_migration.py",    # 岗位配置拆库迁移：启动自动搬 / 幂等 / 非破坏 / 容错
    "test_job_config_seed.py",         # 岗位配置种子化 + JD 回填：可见 / 有源 / 不丢 / 两表对齐
    "test_job_config_soft_delete.py",  # 软删的岗位不被种子化复活：删了就真删了
    "test_dead_code_removed.py",       # 死代码清理：job_reader / render_hr_page 不许复活
    "test_job_picker_refresh.py",      # 工作台岗位下拉自动刷新：指纹 / 跳过 / 兜底提示
    "test_parse_link_job_resolver.py",  # 测评链接只认岗位配置表：不再叠知识库那道闸
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