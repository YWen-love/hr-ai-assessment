"""会话 cookie 策略 / 跨站写请求防护 / 登出宽限期的离线测试。

覆盖 app/session_policy.py：
  * SameSite / Secure 改写（跨站 iframe 场景：https → None+Secure，http → Lax）
  * 跨站写请求拦截（CSRF）：Origin 不同 host → 403；同 host / 无 Origin → 放行
  * 登出宽限期：宽限期内回来 → 取消登出；超时 → 真的清 session

不连网络、不碰真实数据库、不启动 Gradio。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.session_policy import (
    SameOriginGuardMiddleware,
    SessionCookiePolicyMiddleware,
    apply_logout_grace,
    rewrite_session_cookie,
    scope_hosts,
    scope_is_https,
)

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


# ── 纯函数层 ───────────────────────────────────────────────────────────────

def test_rewrite_session_cookie() -> None:
    section("rewrite_session_cookie")
    raw = "hr_session=abc.def; path=/; httponly; samesite=lax"

    https_out = rewrite_session_cookie(raw, secure=True)
    check("https → SameSite=None", "SameSite=None" in https_out, https_out)
    check("https → Secure", "Secure" in https_out, https_out)
    check("https → 丢掉 samesite=lax", "samesite=lax" not in https_out.lower().replace("samesite=none", ""), https_out)
    check("https → 保留 path/httponly", "path=/" in https_out and "httponly" in https_out, https_out)
    check("https → 保留 cookie 值", https_out.startswith("hr_session=abc.def"), https_out)

    http_out = rewrite_session_cookie(raw, secure=False)
    check("http → SameSite=Lax", "SameSite=Lax" in http_out, http_out)
    check("http → 无 Secure", "secure" not in http_out.lower(), http_out)
    check("http → 保留 path/httponly", "path=/" in http_out and "httponly" in http_out, http_out)

    # 已经带 Secure 的输入不应残留重复属性
    once = rewrite_session_cookie("hr_session=x; path=/; httponly; samesite=none; secure", secure=True)
    check("不产生重复 SameSite/Secure", once.lower().count("samesite=") == 1 and once.lower().count("secure") == 1, once)


def test_scope_helpers() -> None:
    section("scope 辅助函数")
    https_scope = {
        "type": "http",
        "scheme": "http",
        "headers": [(b"x-forwarded-proto", b"https"), (b"host", b"demo.ms.show")],
    }
    check("X-Forwarded-Proto 优先于 scheme", scope_is_https(https_scope) is True)
    check("host 集合含 host 头", "demo.ms.show" in scope_hosts(https_scope), scope_hosts(https_scope))

    plain_scope = {"type": "http", "scheme": "http", "headers": [(b"host", b"127.0.0.1:7860")]}
    check("无转发头时按 scheme 判断", scope_is_https(plain_scope) is False)
    check("host 集合含端口", "127.0.0.1:7860" in scope_hosts(plain_scope), scope_hosts(plain_scope))

    fwd_host_scope = {
        "type": "http",
        "scheme": "http",
        "headers": [(b"host", b"internal:7860"), (b"x-forwarded-host", b"demo.ms.show")],
    }
    check("X-Forwarded-Host 也算本机 host", "demo.ms.show" in scope_hosts(fwd_host_scope), scope_hosts(fwd_host_scope))


# ── 中间件层（用最小 Starlette app，不启动 Gradio）─────────────────────────

def _build_app() -> Starlette:
    async def login(request):
        request.session["hr_user_id"] = "HR01"
        return PlainTextResponse("ok")

    async def write(request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/login", login, methods=["GET"]),
            Route("/write", write, methods=["GET", "POST"]),
        ]
    )
    app.add_middleware(SessionMiddleware, secret_key="test-secret", session_cookie="hr_session")
    app.add_middleware(SessionCookiePolicyMiddleware, cookie_name="hr_session")
    app.add_middleware(SameOriginGuardMiddleware)
    return app


def test_cookie_policy_middleware() -> None:
    section("SessionCookiePolicyMiddleware")
    client = TestClient(_build_app(), base_url="https://demo.ms.show")

    resp = client.get("/login", headers={"X-Forwarded-Proto": "https"})
    set_cookie = resp.headers.get("set-cookie", "")
    check("https 响应 → SameSite=None", "SameSite=None" in set_cookie, set_cookie)
    check("https 响应 → Secure", "Secure" in set_cookie, set_cookie)

    http_client = TestClient(_build_app(), base_url="http://127.0.0.1:7860")
    resp2 = http_client.get("/login")
    set_cookie2 = resp2.headers.get("set-cookie", "")
    check("http 响应 → SameSite=Lax", "SameSite=Lax" in set_cookie2, set_cookie2)
    check("http 响应 → 无 Secure", "secure" not in set_cookie2.lower(), set_cookie2)


def test_same_origin_guard() -> None:
    section("SameOriginGuardMiddleware")
    client = TestClient(_build_app(), base_url="https://demo.ms.show")

    same = client.post("/write", headers={"Origin": "https://demo.ms.show"})
    check("同源写请求放行", same.status_code == 200, f"status={same.status_code}")

    other = client.post("/write", headers={"Origin": "https://evil.example.com"})
    check("跨站写请求被拒", other.status_code == 403, f"status={other.status_code}")

    null_origin = client.post("/write", headers={"Origin": "null"})
    check("Origin=null 被拒", null_origin.status_code == 403, f"status={null_origin.status_code}")

    no_origin = client.post("/write")
    check("无 Origin 头放行（curl/服务端调用）", no_origin.status_code == 200, f"status={no_origin.status_code}")

    get_cross = client.get("/write", headers={"Origin": "https://evil.example.com"})
    check("GET 不拦（只挡写请求）", get_cross.status_code == 200, f"status={get_cross.status_code}")

    fwd = client.post("/write", headers={"Origin": "https://demo.ms.show", "X-Forwarded-Host": "demo.ms.show"})
    check("带 X-Forwarded-Host 时同源判定仍成立", fwd.status_code == 200, f"status={fwd.status_code}")


def test_logout_grace() -> None:
    section("登出宽限期")
    session = {"hr_user_id": "HR01"}

    apply_logout_grace(session, now=1000.0)
    check("没有待登出标记时不动 session", session == {"hr_user_id": "HR01"}, session)

    session["logout_at"] = 1030.0
    apply_logout_grace(session, now=1000.0)
    check("宽限期内 → 取消登出", session.get("hr_user_id") == "HR01", session)
    check("宽限期内 → 清掉待登出标记", "logout_at" not in session, session)

    session["logout_at"] = 1030.0
    apply_logout_grace(session, now=1100.0)
    check("超时 → 真的登出", session.get("hr_user_id") is None, session)

    session["logout_at"] = "not-a-number"
    apply_logout_grace(session, now=1100.0)
    check("脏标记 → 视为登出", session == {}, session)


if __name__ == "__main__":
    test_rewrite_session_cookie()
    test_scope_helpers()
    test_cookie_policy_middleware()
    test_same_origin_guard()
    test_logout_grace()

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        sys.exit(1)
    print("test_session_policy: 全部通过")
