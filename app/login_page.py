"""HR 工作台登录 / 注册页（HTML 渲染）。

参考现有 render_candidate_page 风格：内联 HTML + <style> + <script>，避免外部依赖。

**卡片顶部用「高途外观」当横幅背景** —— 图就放在
``<data_root>/static/dist/高途外观.png``（由 main.py 的 /static 路由服务）。
图里自带高途 logo 和「老师出高途」题字，正好落在横幅下半部分，
所以正文里不再另外摆品牌角标。

登录页和注册页共用同一份样式（_PAGE_CSS），改一处两个页面一起生效。
"""
from __future__ import annotations

import html
from typing import Optional

# 卡片横幅图（高途外观）。文件名是中文，这里写死百分号编码后的 URL，
# 浏览器和 StaticFiles 都能正常解析，也不用在服务端再转义一次。
CAMPUS_IMAGE_URL = "/static/dist/%E9%AB%98%E9%80%94%E5%A4%96%E8%A7%82.png"

# 登录 / 注册两页共用的样式。写成普通字符串（不是 f-string），
# 所以里面的 CSS 大括号不用转义。
_PAGE_CSS = """
* { box-sizing: border-box; }

:root {
    --lg-blue: #2457f5;
    --lg-blue-dark: #1d4ed8;
    --lg-ink: #16213a;
    --lg-ink-2: #4d5b73;
    --lg-ink-3: #8695ad;
    --lg-line: #dbe1ef;
    --lg-line-2: #c6d0e6;
    --lg-danger-bg: #fef2f2;
    --lg-danger-line: #fecaca;
    --lg-danger-ink: #b91c1c;
    --lg-r-card: 20px;
    --lg-r-ctl: 10px;
    --lg-font: "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
}

html, body {
    height: 100%;
    margin: 0;
    font-family: var(--lg-font);
    color: var(--lg-ink);
}

body {
    /* 整页底色：深蓝底 + 两团柔光，让白色卡片浮起来 */
    background:
        radial-gradient(880px 520px at 12% -10%, rgba(36, 87, 245, .30), rgba(36, 87, 245, 0) 62%),
        radial-gradient(760px 460px at 92% 2%, rgba(124, 92, 255, .22), rgba(124, 92, 255, 0) 60%),
        linear-gradient(160deg, #16297a 0%, #0b1440 56%, #050a20 100%);
    background-attachment: fixed;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 28px 20px;
}

/* ---------------------------------- 卡片 --------------------------------- */
.card {
    width: 100%;
    max-width: 420px;
    background: #fff;
    border-radius: var(--lg-r-card);
    box-shadow: 0 26px 64px rgba(4, 10, 34, .46);
    overflow: hidden;
}

/* 卡片顶部的高途外观横幅。
   cover 会按宽度铺满，600×346 的图在 420×200 的框里缩放成 420×242，
   再按 center 对齐裁掉上下多余部分 —— 图里的高途 logo 和题字落在横幅下半段，
   正好避开左角的标题。 */
.card-banner {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: 14px;
    min-height: 200px;
    padding: 26px 28px;
    background-image:
        linear-gradient(180deg, rgba(7, 12, 38, .60) 0%, rgba(7, 12, 38, .22) 52%, rgba(7, 12, 38, .30) 100%),
        url('""" + CAMPUS_IMAGE_URL + """');
    background-size: cover, cover;
    background-position: center center, center center;
    background-repeat: no-repeat, no-repeat;
    color: #fff;
}
.brand .logo {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 46px;
    height: 46px;
    border-radius: 13px;
    background: linear-gradient(135deg, #3d6bff, #1d4ed8);
    box-shadow: 0 8px 20px rgba(5, 12, 44, .40);
    color: #fff;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: .5px;
}
.brand-text { min-width: 0; }
.brand h1 {
    margin: 2px 0 5px;
    font-size: 20px;
    font-weight: 700;
    line-height: 1.35;
    color: #fff;
    text-shadow: 0 2px 10px rgba(4, 10, 34, .55);
}
.brand p {
    margin: 0;
    font-size: 13px;
    line-height: 1.6;
    color: rgba(226, 234, 255, .88);
    text-shadow: 0 1px 8px rgba(4, 10, 34, .55);
}

/* --------------------------------- 表单 ---------------------------------- */
.card-body { padding: 28px 30px 24px; }
form { display: grid; gap: 14px; }
label {
    display: grid;
    gap: 7px;
    font-size: 13px;
    font-weight: 600;
    color: var(--lg-ink-2);
}
.hint {
    font-size: 11px;
    font-weight: 400;
    color: var(--lg-ink-3);
}
input[type="text"],
input[type="password"] {
    font-family: inherit;
    font-size: 15px;
    padding: 12px 14px;
    border-radius: var(--lg-r-ctl);
    border: 1px solid var(--lg-line);
    outline: none;
    transition: border-color .15s, box-shadow .15s;
    background: #fff;
    color: var(--lg-ink);
}
input::placeholder { color: #a9b4c8; }
input[type="text"]:focus,
input[type="password"]:focus {
    border-color: var(--lg-blue);
    box-shadow: 0 0 0 3px rgba(36, 87, 245, .16);
}
/* 浏览器自动填充会自带一个黄底，把字盖成看不清 —— 用内白底盖掉 */
input:-webkit-autofill,
input:-webkit-autofill:hover,
input:-webkit-autofill:focus {
    -webkit-text-fill-color: var(--lg-ink);
    -webkit-box-shadow: 0 0 0 1000px #ffffff inset;
    caret-color: var(--lg-ink);
}
button {
    margin-top: 4px;
    padding: 13px 16px;
    font-family: inherit;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 1px;
    color: #fff;
    background: linear-gradient(135deg, #3d6bff, var(--lg-blue-dark));
    border: none;
    border-radius: var(--lg-r-ctl);
    cursor: pointer;
    box-shadow: 0 8px 20px rgba(36, 87, 245, .32);
    transition: transform .12s, box-shadow .15s, filter .15s;
}
button:hover { filter: brightness(1.05); box-shadow: 0 12px 26px rgba(36, 87, 245, .40); }
button:active { transform: translateY(1px); }
.error {
    margin: 0 0 4px;
    padding: 10px 12px;
    background: var(--lg-danger-bg);
    border: 1px solid var(--lg-danger-line);
    color: var(--lg-danger-ink);
    border-radius: var(--lg-r-ctl);
    font-size: 13px;
    line-height: 1.6;
}
.alt-link {
    text-align: center;
    margin: 18px 0 0;
    font-size: 13px;
    color: var(--lg-ink-2);
}
.alt-link a {
    color: var(--lg-blue);
    text-decoration: none;
    font-weight: 600;
    margin-left: 4px;
}
.alt-link a:hover { text-decoration: underline; }
.footer {
    text-align: center;
    margin-top: 20px;
    color: var(--lg-ink-3);
    font-size: 12px;
}

/* 键盘可达性：鼠标点击不显示焦点环，Tab 聚焦才显示 */
input:focus-visible,
button:focus-visible,
a:focus-visible {
    outline: 2px solid var(--lg-blue);
    outline-offset: 2px;
}
.card-banner :focus-visible { outline-color: #fff; }

@media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
}
"""


def _safe(value: Optional[str], default: str = "") -> str:
    return html.escape(value) if value else default


def _next_query(next_url: str) -> str:
    """登录 / 注册互相跳转时，把回跳地址带过去（默认 /hr 不带）。"""
    if next_url and next_url != "/hr":
        return "?next=" + html.escape(next_url, quote=True)
    return ""


def _banner(title: str, subtitle: str) -> str:
    """卡片顶部的横幅：左角放标题，右边是高途外观图。"""
    return (
        '<div class="card-banner">'
        '<div class="brand">'
        '<span class="logo">HR</span>'
        '<span class="brand-text">'
        f"<h1>{_safe(title)}</h1>"
        f"<p>{_safe(subtitle)}</p>"
        "</span>"
        "</div>"
        "</div>"
    )


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
    <style>{_PAGE_CSS}</style>
</head>
<body>
    <div class="card">
        {_banner("HR 简历评测工作台", "请使用 HR 账号登录")}
        <div class="card-body">
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
            <p class="alt-link">还没有账号？<a href="/register{_next_query(next_url)}">立即注册</a></p>
            <div class="footer">途途 · 高途 HR 助理</div>
        </div>
    </div>
</body>
</html>"""


def render_register_page(next_url: str = "/hr", error: str = "", username: str = "") -> str:
    """渲染注册页 HTML（与登录页同款卡片 + 同一张横幅图）。

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
    <style>{_PAGE_CSS}</style>
</head>
<body>
    <div class="card">
        {_banner("创建 HR 账号", "第一次使用？填写信息完成注册")}
        <div class="card-body">
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
            <p class="alt-link">已有账号？<a href="/login{_next_query(next_url)}">立即登录</a></p>
            <div class="footer">途途 · 高途 HR 助理</div>
        </div>
    </div>
</body>
</html>"""
