"""HR 工作台登录页（HTML 渲染）。

参考现有 render_candidate_page 风格：内联 HTML + <style> + <script>，避免外部依赖。
页面背景：data_root()/static/bg.jpg（由 main.py 的 /static 路由服务），不存在时降级为深色渐变。
"""
from __future__ import annotations

import html
from typing import Optional

# 背景图基础 URL（由 main.py 把 /static 目录 mount 到这里）
BG_URL = "/static/登录.jpg"


def _safe(value: Optional[str], default: str = "") -> str:
    return html.escape(value) if value else default


def render_login_page(next_url: str = "/hr", error: str = "") -> str:
    """渲染登录页 HTML。

    Args:
        next_url: 登录成功后回跳地址（白名单由 main.py 中间件约束）
        error: 错误提示文案（密码错时回填 401 + 渲染此文案）
    """
    safe_next = _safe(next_url, "/hr")
    error_block = ""
    if error:
        error_block = f'<p class="error">{_safe(error)}</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>登录 · HR 简历评测</title>
    <style>
        * {{ box-sizing: border-box; }}
        html, body {{
            height: 100%;
            margin: 0;
            font-family: -apple-system, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
        }}
        body {{
            background:
                linear-gradient(rgba(15, 23, 42, 0.55), rgba(15, 23, 42, 0.55)),
                url('{BG_URL}') center/cover no-repeat fixed;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
            color: #1f2937;
        }}
        .card {{
            width: 100%;
            max-width: 420px;
            background: rgba(255, 255, 255, 0.96);
            backdrop-filter: blur(8px);
            border-radius: 18px;
            padding: 36px 32px 28px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
        }}
        .brand {{
            text-align: center;
            margin-bottom: 24px;
        }}
        .brand .logo {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 56px;
            height: 56px;
            border-radius: 14px;
            background: linear-gradient(135deg, #2858ff, #1d4ed8);
            color: white;
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 12px;
        }}
        .brand h1 {{
            margin: 0 0 4px;
            font-size: 22px;
            font-weight: 700;
            color: #1f2937;
        }}
        .brand p {{
            margin: 0;
            font-size: 14px;
            color: #6b7280;
        }}
        form {{ display: grid; gap: 14px; }}
        label {{
            display: grid;
            gap: 6px;
            font-size: 13px;
            font-weight: 600;
            color: #374151;
        }}
        input[type="text"],
        input[type="password"] {{
            font-size: 15px;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid #d9deeb;
            outline: none;
            transition: border-color .15s, box-shadow .15s;
            background: #fff;
        }}
        input:focus {{
            border-color: #2858ff;
            box-shadow: 0 0 0 3px rgba(40, 88, 255, 0.18);
        }}
        button {{
            margin-top: 6px;
            padding: 12px 16px;
            font-size: 15px;
            font-weight: 600;
            color: white;
            background: linear-gradient(135deg, #2858ff, #1d4ed8);
            border: none;
            border-radius: 10px;
            cursor: pointer;
            transition: transform .1s, box-shadow .15s;
        }}
        button:hover {{
            box-shadow: 0 6px 18px rgba(40, 88, 255, 0.35);
        }}
        button:active {{ transform: translateY(1px); }}
        .error {{
            margin: 0 0 8px;
            padding: 10px 12px;
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #b91c1c;
            border-radius: 10px;
            font-size: 13px;
        }}
        .alt-link {{
            text-align: center;
            margin: 18px 0 0;
            font-size: 13px;
            color: #6b7280;
        }}
        .alt-link a {{
            color: #2858ff;
            text-decoration: none;
            font-weight: 600;
            margin-left: 4px;
        }}
        .alt-link a:hover {{ text-decoration: underline; }}
        .footer {{
            text-align: center;
            margin-top: 22px;
            color: #9ca3af;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="brand">
            <div class="logo">HR</div>
            <h1>HR 简历评测工作台</h1>
            <p>请使用 HR 账号登录</p>
        </div>
        <form method="POST" action="/api/login" autocomplete="on">
            <input type="hidden" name="next" value="{safe_next}" />
            {error_block}
            <label>
                账号
                <input type="text" name="username" required autofocus placeholder="请输入账号" autocomplete="username" />
            </label>
            <label>
                密码
                <input type="password" name="password" required placeholder="请输入密码" autocomplete="current-password" />
            </label>
            <button type="submit">登 录</button>
        </form>
        <p class="alt-link">还没有账号？<a href="/register{("?next=" + html.escape(next_url)) if next_url and next_url != "/hr" else ""}">立即注册</a></p>
        <div class="footer">途途 · 高途 HR 助理</div>
    </div>
</body>
</html>"""


def render_register_page(next_url: str = "/hr", error: str = "", username: str = "") -> str:
    """渲染注册页 HTML（与登录页同款背景图 + 卡片样式）。

    Args:
        next_url: 注册成功后的回跳地址
        error: 错误提示（用户名校验失败/密码过短/两次密码不一致/重名）
        username: 表单回填（密码不回填，避免明文留在 DOM）
    """
    safe_next = _safe(next_url, "/hr")
    safe_username = html.escape(username) if username else ""
    error_block = ""
    if error:
        error_block = f'<p class="error">{_safe(error)}</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>注册 · HR 简历评测</title>
    <style>
        * {{ box-sizing: border-box; }}
        html, body {{
            height: 100%;
            margin: 0;
            font-family: -apple-system, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
        }}
        body {{
            background:
                linear-gradient(rgba(15, 23, 42, 0.55), rgba(15, 23, 42, 0.55)),
                url('{BG_URL}') center/cover no-repeat fixed;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
            color: #1f2937;
        }}
        .card {{
            width: 100%;
            max-width: 420px;
            background: rgba(255, 255, 255, 0.96);
            backdrop-filter: blur(8px);
            border-radius: 18px;
            padding: 36px 32px 28px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
        }}
        .brand {{
            text-align: center;
            margin-bottom: 24px;
        }}
        .brand .logo {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 56px;
            height: 56px;
            border-radius: 14px;
            background: linear-gradient(135deg, #2858ff, #1d4ed8);
            color: white;
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 12px;
        }}
        .brand h1 {{
            margin: 0 0 4px;
            font-size: 22px;
            font-weight: 700;
            color: #1f2937;
        }}
        .brand p {{
            margin: 0;
            font-size: 14px;
            color: #6b7280;
        }}
        form {{ display: grid; gap: 14px; }}
        label {{
            display: grid;
            gap: 6px;
            font-size: 13px;
            font-weight: 600;
            color: #374151;
        }}
        .hint {{
            font-size: 11px;
            font-weight: 400;
            color: #9ca3af;
        }}
        input[type="text"],
        input[type="password"] {{
            font-size: 15px;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid #d9deeb;
            outline: none;
            transition: border-color .15s, box-shadow .15s;
            background: #fff;
        }}
        input:focus {{
            border-color: #2858ff;
            box-shadow: 0 0 0 3px rgba(40, 88, 255, 0.18);
        }}
        button {{
            margin-top: 6px;
            padding: 12px 16px;
            font-size: 15px;
            font-weight: 600;
            color: white;
            background: linear-gradient(135deg, #2858ff, #1d4ed8);
            border: none;
            border-radius: 10px;
            cursor: pointer;
            transition: transform .1s, box-shadow .15s;
        }}
        button:hover {{
            box-shadow: 0 6px 18px rgba(40, 88, 255, 0.35);
        }}
        button:active {{ transform: translateY(1px); }}
        .error {{
            margin: 0 0 8px;
            padding: 10px 12px;
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #b91c1c;
            border-radius: 10px;
            font-size: 13px;
        }}
        .alt-link {{
            text-align: center;
            margin: 18px 0 0;
            font-size: 13px;
            color: #6b7280;
        }}
        .alt-link a {{
            color: #2858ff;
            text-decoration: none;
            font-weight: 600;
            margin-left: 4px;
        }}
        .alt-link a:hover {{ text-decoration: underline; }}
        .footer {{
            text-align: center;
            margin-top: 22px;
            color: #9ca3af;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="brand">
            <div class="logo">HR</div>
            <h1>创建 HR 账号</h1>
            <p>第一次使用？填写信息完成注册</p>
        </div>
        <form method="POST" action="/api/register" autocomplete="on">
            <input type="hidden" name="next" value="{safe_next}" />
            {error_block}
            <label>
                账号
                <input type="text" name="username" required autofocus placeholder="字母开头，3-32 字符" value="{safe_username}" autocomplete="username" />
                <span class="hint">仅含字母、数字、下划线、连字符</span>
            </label>
            <label>
                密码
                <input type="password" name="password" required placeholder="至少 6 位" autocomplete="new-password" />
            </label>
            <label>
                确认密码
                <input type="password" name="confirm_password" required placeholder="再次输入密码" autocomplete="new-password" />
            </label>
            <button type="submit">注 册</button>
        </form>
        <p class="alt-link">已有账号？<a href="/login{("?next=" + safe_next) if next_url and next_url != "/hr" else ""}">立即登录</a></p>
        <div class="footer">途途 · 高途 HR 助理</div>
    </div>
</body>
</html>"""