"""候选人测评页回归测试。

对着「候选人: undefined 岗位: undefined」这个线上问题写的。

**这个 bug 的本质**：页面头部原来靠前端再调一次接口取候选人 / 岗位。
接口任何一次非 200（路由不存在、跨站拦截、岗位配置被删），
前端就把 undefined 直接渲染到了页面上 —— 而这两个值本来就在会话行里躺着，
服务端渲染页面时已经拿到了。

所以这里守住三条线：
  1. 候选人 / 岗位由服务端渲染进 HTML，不依赖任何接口；
  2. 任何情况下页面里都不出现 "undefined"；
  3. /candidate/start 在岗位配置缺失时退化成「有姓名有岗位 + error 字段」，
     而不是整包 404 把头部信息一起拖下水。

不发起真实模型调用：所有会走到 LLM 的分支都被挡在 _load_config 之前，
或用已有的 chat_history 走幂等分支。
不碰真实数据库：job_config_store / session_store 的 DB_PATH 猴补丁到临时文件。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.candidate_page as cp
import app.job_config_store as jcs
import app.session_store as ss
from app.assessment_service import AssessmentError, AssessmentService

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 页面渲染
# ---------------------------------------------------------------------------

section("1. 候选人 / 岗位由服务端渲染进 HTML")

session = {"candidate_name": "张三", "job_name": "初中学习顾问"}
page = cp.render_candidate_page("sess-abc-123", session)

check("候选人姓名出现在页面上", "张三" in page)
check("岗位名出现在页面上", "初中学习顾问" in page)
check("页面不出现 undefined", "undefined" not in page, "页面上仍有 undefined")
check("session_id 进了页面", "sess-abc-123" in page)
check("前端不再从接口取头部字段", "candidate_name" not in page and "job_name" not in page)

section("2. 字段缺失时也有兜底，不出现 undefined")

for label, arg in (("session=None", None), ("session={}", {})):
    rendered = cp.render_candidate_page("sess-1", arg)
    check(f"{label} 时不出现 undefined", "undefined" not in rendered)
    check(f"{label} 时显示占位候选人", "候选人" in rendered)
    check(f"{label} 时显示占位岗位", "未填写" in rendered)

partial = cp.render_candidate_page("sess-1", {"candidate_name": "李四"})
check("只有姓名时岗位走占位", "李四" in partial and "未填写" in partial)
check("只有姓名时也不出现 undefined", "undefined" not in partial)

section("3. 姓名 / 岗位里的 HTML 与 session_id 里的引号都被转义")

xss = cp.render_candidate_page(
    "sess-1",
    {"candidate_name": "<script>alert(1)</script>", "job_name": "A & B"},
)
check("姓名里的 <script> 被转义", "&lt;script&gt;alert(1)&lt;/script&gt;" in xss)
check("姓名里的裸 </script> 不出现", "alert(1)</script>" not in xss)
check("岗位里的 & 被转义", "A &amp; B" in xss)

injected = cp.render_candidate_page('a";alert(1);', {"candidate_name": "张三", "job_name": "岗位"})
check("session_id 里的引号被 JSON 转义", '"a\\";alert(1);"' in injected)
check("session_id 裸串不出现", '"a";alert(1);"' not in injected)
check("转义后的页面仍无 undefined", "undefined" not in injected)


# ---------------------------------------------------------------------------
# /candidate/start 的降级行为
# ---------------------------------------------------------------------------

section("4. 岗位配置缺失时，start_candidate_session 返回姓名 + 岗位 + error")

original_session_db = ss.DB_PATH
original_job_db = jcs.DB_PATH

with tempfile.TemporaryDirectory() as tmp:
    ss.DB_PATH = str(Path(tmp) / "sessions.db")
    jcs.DB_PATH = str(Path(tmp) / "job_configs.db")

    try:
        service = AssessmentService()

        # 岗位配置不存在（模拟「配置被 HR 删了 / 链接指错了岗位」）
        sid_missing = ss.create_session(
            candidate_name="张三",
            job_name="初中学习顾问",
            resume_json={},
            candidate_summary="",
            job_key="job_that_does_not_exist",
        )
        result = run(service.start_candidate_session(sid_missing))
        check("岗位配置缺失时不抛异常", isinstance(result, dict))
        check("仍然返回候选人姓名", result.get("candidate_name") == "张三", str(result))
        check("仍然返回岗位名", result.get("job_name") == "初中学习顾问", str(result))
        check("开场白为空", not result.get("first_question"), str(result))
        check("带上了 error 字段", bool(result.get("error")), str(result))
        check("error 里说明是岗位配置问题", "岗位配置不存在" in (result.get("error") or ""), str(result))

        section("5. 会话不存在时才 404")

        try:
            run(service.start_candidate_session("no_such_session"))
            check("会话不存在时抛 AssessmentError", False, "没有抛异常")
        except AssessmentError as exc:
            check("会话不存在时抛 AssessmentError", "会话不存在" in str(exc), str(exc))

        section("6. 已有开场白时不重开（幂等，不调模型）")

        sid_done = ss.create_session(
            candidate_name="李四",
            job_name="课程顾问",
            resume_json={},
            candidate_summary="",
            job_key="job_that_does_not_exist",
        )
        ss.save_session_answers(
            sid_done,
            [],
            "你好，请介绍一下你自己。",
            status="in_progress",
            chat_history=[{"role": "assistant", "content": "你好，请介绍一下你自己。"}],
        )
        result2 = run(service.start_candidate_session(sid_done))
        check("返回已存的开场白", result2.get("first_question") == "你好，请介绍一下你自己。", str(result2))
        check("配置已删但有开场白时不报 error", not result2.get("error"), str(result2))
        check("候选人姓名正确", result2.get("candidate_name") == "李四", str(result2))
        check("岗位名正确", result2.get("job_name") == "课程顾问", str(result2))
    finally:
        ss.DB_PATH = original_session_db
        jcs.DB_PATH = original_job_db


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
