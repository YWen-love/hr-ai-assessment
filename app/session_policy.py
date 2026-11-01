"""会话 cookie 策略 + 跨站写请求防护。

背景（实测于魔搭创空间预览）：
  魔搭把应用放在 modelscope.cn 的**跨站 iframe** 里，真正跑应用的页面是 *.ms.show。
  浏览器在跨站 iframe 请求里不会带 SameSite=Lax 的 cookie →
  每次打开 / 刷新应用都被判成「未登录」→ /hr/jobs 被 302 到 /login，
  在预览里看起来就是「页面是空的」。

所以：
  * https 请求 → session cookie 改成 SameSite=None; Secure
    （浏览器规定 SameSite=None 必须配 Secure，缺 Secure 会被直接丢弃）
  * 纯 http（本地直连调试）→ 保持 Lax，否则 Secure cookie 在 http 下会被浏览器丢弃

代价：SameSite=None 之后，浏览器在跨站请求里也会带上 cookie，
      原来靠 SameSite=Lax 顺带挡掉的那层 CSRF 保护没了 → SameOriginGuardMiddleware 补上。

另外还有「页面卸载自动登出」：刷新 / 站内跳转也会触发 pagehide，
以前直接清 session，刷新一下就被踢回登录页 —— 改成宽限期，见 apply_logout_grace。
"""
from __future__ import annotations

import time
from typing import Any, MutableMapping, Optional, Set
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGOUT_GRACE_SECONDS = 30
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


def header_value(scope: Scope, name: str) -> str:
    """从 ASGI scope 里取一个请求头（大小写不敏感）。"""
    raw = name.encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == raw:
            return value.decode("latin-1")
    return ""


def scope_is_https(scope: Scope) -> bool:
    """请求对外是不是 https。

    反代（魔搭）后面 app 自身可能只看到 http，优先信 X-Forwarded-Proto。
    """
    forwarded = header_value(scope, "x-forwarded-proto").split(",")[0].strip().lower()
    if forwarded:
        return forwarded == "https"
    return scope.get("scheme", "http") == "https"


def scope_hosts(scope: Scope) -> Set[str]:
    """请求可能被认作「本机」的 host 集合：Host 与 X-Forwarded-Host 都算。"""
    hosts = set()
    for key in ("host", "x-forwarded-host"):
        for value in header_value(scope, key).split(","):
            value = value.strip().lower()
            if value:
                hosts.add(value)
    return hosts


def rewrite_session_cookie(header_value: str, secure: bool) -> str:
    """改写 session cookie 的 SameSite / Secure。

    其余属性（path / httponly / max-age）原样保留。
    """
    parts = [part.strip() for part in header_value.split(";")]
    cookie, attrs = parts[0], []
    for attr in parts[1:]:
        low = attr.lower()
        if low.startswith("samesite=") or low == "secure":
            continue
        attrs.append(attr)
    attrs.append("SameSite=None; Secure" if secure else "SameSite=Lax")
    return "; ".join([cookie, *attrs])


def apply_logout_grace(
    session: MutableMapping[str, Any],
    now: Optional[float] = None,
    grace_seconds: float = LOGOUT_GRACE_SECONDS,
) -> None:
    """处理「待登出」标记。

    页面卸载（刷新 / 站内跳转 / 关标签页）都会触发 pagehide → /api/logout。
    以前是立刻清 session，结果刷新一下就被踢回登录页。
    现在改成宽限期：置一个时间戳，宽限期内又有请求就取消登出，超时才真的清。
    """
    logout_at = session.get("logout_at")
    if logout_at is None:
        return
    current = time.time() if now is None else now
    try:
        deadline = float(logout_at)
    except (TypeError, ValueError):
        session.clear()
        return
    if current < deadline:
        del session["logout_at"]
    else:
        session.clear()


class SessionCookiePolicyMiddleware:
    """按请求协议改写 session cookie 的 SameSite / Secure（跨站 iframe 场景）。"""

    def __init__(self, app: ASGIApp, cookie_name: str = "hr_session") -> None:
        self.app = app
        self.cookie_name = cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        secure = scope_is_https(scope)
        prefix = (self.cookie_name + "=").encode("latin-1")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = []
                for key, value in message.get("headers", []):
                    if key.lower() == b"set-cookie" and value.startswith(prefix):
                        value = rewrite_session_cookie(
                            value.decode("latin-1"), secure
                        ).encode("latin-1")
                    headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SameOriginGuardMiddleware:
    """跨站写请求拦截（CSRF 防护）。

    session cookie 改成 SameSite=None 之后，浏览器在跨站请求里也会带上它，
    原来靠 SameSite=Lax 顺带挡掉的那层保护没了，这里补回来：
    写请求如果带 Origin，其 host 必须与请求本身的 host 相同。
    没有 Origin 头的（curl / 服务端调用）放行。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope.get("method", "GET").upper()
        if scope["type"] != "http" or method in SAFE_HTTP_METHODS:
            await self.app(scope, receive, send)
            return

        origin = header_value(scope, "origin").strip()
        if not origin:
            await self.app(scope, receive, send)
            return

        origin_host = urlsplit(origin).netloc.lower()
        if not origin_host or origin_host not in scope_hosts(scope):
            await self._reject(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        from starlette.responses import JSONResponse

        response = JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        await response(scope, receive, send)
