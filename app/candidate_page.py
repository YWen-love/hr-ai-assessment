"""候选人测评页（HTML 渲染）。

与 app/login_page.py 同一套路：内联 HTML + <style> + <script>，避免外部依赖。

**候选人 / 岗位由服务端直接渲染进 HTML**，不再依赖前端再调一次接口取。
原因：头部一旦依赖接口，接口任何一次非 200（路由不存在、跨站拦截、
岗位配置被删）都会让页面显示"候选人: undefined 岗位: undefined" ——
而这两个值本来就在数据库里躺着，渲染页面时已经拿到了。
前端仍然调 /candidate/start 取开场白，取不到时给出明确提示，
不再把空值渲染到页面上。

**页面背景用「途途半身像」**：图放在 ``<data_root>/static/dist/途途半身像.png``，
固定贴在右下角。这张 PNG 两侧是**透明**留白（不是白色），所以贴边放不会
露出一块方形边界；正文卡片往左让一让，别把人物压在卡片底下。
顶部有一条入口栏，右上角挂「高途大字」。

登录页和岗位管理页也各用一张品牌图，见 app/login_page.py 与 app/gradio_ui.py。
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, Optional

# 品牌静态资源（由 main.py 把 <data_root>/static mount 到 /static）。
# 文件名是中文，这里写死百分号编码后的 URL —— 浏览器和 StaticFiles 都能解析。
#   途途半身像.png → 候选人测评页的整页背景（右下角）
#   高途大字.png   → 入口栏右上角的品牌角标
MASCOT_URL = "/static/dist/%E9%80%94%E9%80%94%E5%8D%8A%E8%BA%AB%E5%83%8F.png"
BRAND_MARK_URL = "/static/dist/%E9%AB%98%E9%80%94%E5%A4%A7%E5%AD%97.png"

# 页面样式。写成普通字符串（不是 f-string），所以里面的 CSS 大括号不用转义。
# 变量名跟登录页（--lg-*）保持一致，两套页面看起来才像一家人。
_PAGE_CSS = """
* { box-sizing: border-box; }

:root {
    --lg-blue: #2457f5;
    --lg-blue-dark: #1d4ed8;
    --lg-blue-soft: #eef3ff;
    --lg-blue-line: #dbe4ff;
    --lg-ink: #16213a;
    --lg-ink-2: #4d5b73;
    --lg-ink-3: #8695ad;
    --lg-line: #dbe1ef;
    --lg-line-2: #c6d0e6;
    --lg-user-bg: #e8f8f0;
    --lg-user-line: #cdeada;
    --lg-ok-bg: #eef8f1;
    --lg-ok-line: #cfe9d8;
    --lg-danger-bg: #fff1f0;
    --lg-danger-line: #ffd3cf;
    --lg-danger-ink: #b42318;
    --lg-r-card: 20px;
    --lg-r-ctl: 10px;
    --lg-r-bubble: 14px;
    --lg-shadow: 0 18px 48px rgba(11, 20, 64, .14), 0 2px 8px rgba(11, 20, 64, .05);
    --lg-font: "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
}

html, body { height: 100%; margin: 0; }

body {
    font-family: var(--lg-font);
    color: var(--lg-ink);
    min-height: 100vh;
    padding: 0 0 44px;
    /* 底色：浅蓝渐变 + 两团柔光，页面整体还是蓝白那套 */
    background:
        radial-gradient(900px 520px at 6% -12%, rgba(36, 87, 245, .16), rgba(36, 87, 245, 0) 62%),
        radial-gradient(760px 460px at 96% 4%, rgba(124, 92, 255, .12), rgba(124, 92, 255, 0) 60%),
        linear-gradient(180deg, #f8faff 0%, #eef3ff 52%, #e7eeff 100%);
    background-attachment: fixed;
}

/* ------------------------------- 背景人物 -------------------------------- */
/* 途途半身像。PNG 左右是**透明**留白（不是白色），所以贴到页面右边也不会露出
   一块方形边界。
   图里人物实际只占 x 89~332（宽 243 / 总宽 408），右边的透明留白有 76px。
   right:-90px 正好把这截透明留白推到视口外 —— 人物右缘贴着视口右边，
   不会像 right:-58px 那样把耳朵切掉一截。
   视口不够宽时人物会被卡片压住，所以下面按宽度分三档：人物大小、卡片宽度
   和卡片位置都跟着调，保证人和卡片各占各的位置。 */
.mascot-bg {
    position: fixed;
    right: -90px;
    bottom: 0;
    height: min(90vh, 760px);
    width: auto;
    max-width: none;
    z-index: 0;
    pointer-events: none;
    user-select: none;
    filter: drop-shadow(0 26px 54px rgba(11, 20, 64, .16));
}

/* ------------------------------- 入口栏 ---------------------------------- */
.topbar {
    position: relative;
    z-index: 3;
    display: flex;
    align-items: center;
    gap: 13px;
    padding: 18px 30px 6px;
}
.topbar .brand-logo {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 42px;
    height: 42px;
    border-radius: 13px;
    background: linear-gradient(135deg, #3d6bff, #1d4ed8);
    box-shadow: 0 8px 18px rgba(36, 87, 245, .30);
    color: #fff;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: .5px;
}
.topbar-text { min-width: 0; }
.topbar h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
    line-height: 1.35;
    letter-spacing: .3px;
    color: var(--lg-ink);
}
.topbar p {
    margin: 2px 0 0;
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--lg-ink-3);
}
.topbar-right { margin-left: auto; display: flex; align-items: center; }
/* 高途大字：钉在入口栏右上角 */
.brand-mark {
    width: 44px;
    height: 44px;
    border-radius: 13px;
    overflow: hidden;
    background: #fff;
    border: 1px solid var(--lg-line);
    box-shadow: 0 6px 16px rgba(11, 20, 64, .10);
}
.brand-mark img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
}

/* -------------------------------- 卡片 ----------------------------------- */
.panel {
    position: relative;
    z-index: 2;
    width: 100%;
    max-width: 880px;
    margin: 14px auto 0;
    padding: 26px 28px 24px;
    background: rgba(255, 255, 255, .93);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    border: 1px solid var(--lg-line);
    border-radius: var(--lg-r-card);
    box-shadow: var(--lg-shadow);
}
/* 人物在右边、卡片在左边，中间要留得开。宽度不够时人物会被卡片压住，
   所以按视口宽度分三档，每档同时调「人物大小」「卡片宽度」「卡片位置」：
     ≥1400px  人物满血、卡片靠左，两者中间留出空档；
     1100~1399 人物缩小、卡片收窄，仍然并排；
     <1100   放不下了，人物退成很淡的底纹，卡片居中。 */
@media (min-width: 1400px) {
    .panel {
        max-width: 820px;
        margin-left: clamp(24px, 4.5vw, 110px);
        margin-right: auto;
    }
}
@media (min-width: 1100px) and (max-width: 1399px) {
    .mascot-bg { right: -60px; height: min(66vh, 500px); }
    .panel {
        max-width: 680px;
        margin-left: 36px;
        margin-right: auto;
    }
}
@media (max-width: 1099px) {
    /* 窄屏放不下人物，降到很淡，只当个底纹 */
    .mascot-bg { right: -30px; height: min(58vh, 440px); opacity: .16; }
    .panel {
        max-width: 880px;
        margin-left: auto;
        margin-right: auto;
    }
}

.panel h2 {
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    line-height: 1.4;
    color: var(--lg-ink);
}
.panel-sub {
    margin: 5px 0 0;
    font-size: 13px;
    color: var(--lg-ink-3);
}

/* 候选人 / 岗位：服务端渲染好的两个小胶囊 */
.meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 14px 0 0;
}
.meta-chip {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px 13px;
    border-radius: 999px;
    background: var(--lg-blue-soft);
    border: 1px solid var(--lg-blue-line);
    font-size: 13px;
    color: var(--lg-ink-2);
}
.meta-chip strong { font-weight: 600; color: var(--lg-ink); }

/* ------------------------------- 对话气泡 -------------------------------- */
.messages {
    display: grid;
    gap: 12px;
    margin: 20px 0 4px;
}
.msg {
    max-width: 78%;
    padding: 12px 15px;
    border-radius: var(--lg-r-bubble);
    font-size: 14.5px;
    line-height: 1.75;
    word-break: break-word;
    box-shadow: 0 1px 2px rgba(11, 20, 64, .05);
}
.assistant {
    justify-self: start;
    background: var(--lg-blue-soft);
    border: 1px solid var(--lg-blue-line);
    border-top-left-radius: 6px;
}
.user {
    justify-self: end;
    background: var(--lg-user-bg);
    border: 1px solid var(--lg-user-line);
    border-top-right-radius: 6px;
}
.msg.error {
    background: var(--lg-danger-bg);
    border-color: var(--lg-danger-line);
    color: var(--lg-danger-ink);
}

/* -------------------------------- 输入区 --------------------------------- */
form {
    display: flex;
    gap: 10px;
    margin-top: 18px;
}
input {
    flex: 1;
    min-width: 0;
    font-family: inherit;
    font-size: 15px;
    padding: 12px 14px;
    border: 1px solid var(--lg-line);
    border-radius: var(--lg-r-ctl);
    background: #fff;
    color: var(--lg-ink);
    outline: none;
    transition: border-color .15s, box-shadow .15s;
}
input::placeholder { color: #a9b4c8; }
input:focus {
    border-color: var(--lg-blue);
    box-shadow: 0 0 0 3px rgba(36, 87, 245, .16);
}
button {
    flex: 0 0 auto;
    font-family: inherit;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 1px;
    color: #fff;
    border: none;
    border-radius: var(--lg-r-ctl);
    padding: 12px 22px;
    cursor: pointer;
    background: linear-gradient(135deg, #3d6bff, var(--lg-blue-dark));
    box-shadow: 0 8px 20px rgba(36, 87, 245, .32);
    transition: transform .12s, box-shadow .15s, filter .15s;
}
button:hover {
    filter: brightness(1.05);
    box-shadow: 0 12px 26px rgba(36, 87, 245, .40);
}
button:active { transform: translateY(1px); }

/* -------------------------------- 结果区 --------------------------------- */
.result-box {
    margin-top: 18px;
    padding: 16px 18px;
    background: var(--lg-ok-bg);
    border: 1px solid var(--lg-ok-line);
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.8;
    color: #21503a;
    display: none;
}

.panel-foot {
    margin-top: 18px;
    padding-top: 14px;
    border-top: 1px solid var(--lg-line);
    text-align: center;
    font-size: 12px;
    color: var(--lg-ink-3);
}

/* 键盘可达性：鼠标点击不显示焦点环，Tab 聚焦才显示 */
input:focus-visible,
button:focus-visible {
    outline: 2px solid var(--lg-blue);
    outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
}
"""


def _safe(value: Optional[str], default: str = "") -> str:
    return html.escape(str(value)) if value else default


def render_candidate_page(session_id: str, session: Optional[Dict[str, Any]] = None) -> str:
    """渲染候选人测评页。

    Args:
        session_id: 会话 ID（拼进 JS，必须走 JSON 转义，不能裸拼进引号里）
        session: 会话记录（sessions 表一行）。取 candidate_name / job_name 渲染头部。
                 传空也能渲染 —— 头部显示占位符，不显示空值。
    """
    session = session or {}
    candidate_name = _safe(session.get("candidate_name"), "候选人")
    job_name = _safe(session.get("job_name"), "未填写")
    # session_id 来自 URL 路径，拼进 JS 字符串必须用 JSON 转义。
    session_id_js = json.dumps(str(session_id), ensure_ascii=False)

    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>候选人测评</title>
        <style>{_PAGE_CSS}</style>
    </head>
    <body>
      <img class="mascot-bg" src="{MASCOT_URL}" alt="" aria-hidden="true" />
      <header class="topbar">
        <span class="brand-logo">途</span>
        <span class="topbar-text">
          <h1>候选人测评</h1>
          <p>途途会一题一题地和你聊，请尽量真实作答</p>
        </span>
        <span class="topbar-right">
          <span class="brand-mark"><img src="{BRAND_MARK_URL}" alt="高途" /></span>
        </span>
      </header>
      <main class="panel">
        <h2>途途测评</h2>
        <p class="panel-sub">下面这些问题没有标准答案，HR 更关心你的思考过程。</p>
        <div id="meta" class="meta">
          <span class="meta-chip"><strong>候选人</strong> {candidate_name}</span>
          <span class="meta-chip"><strong>岗位</strong> {job_name}</span>
        </div>
        <div id="messages" class="messages"></div>
        <form id="chat-form">
          <input id="answer" autocomplete="off" placeholder="请输入你的回答" />
          <button type="submit">发送</button>
        </form>
        <div id="result-box" class="result-box"></div>
        <div class="panel-foot">途途 · 高途 HR 助理</div>
      </main>
      <script>
        const sessionId = {session_id_js};
        const messagesEl = document.getElementById('messages');
        const resultBox = document.getElementById('result-box');
        const form = document.getElementById('chat-form');
        const input = document.getElementById('answer');

        function appendMessage(role, text) {{
          const div = document.createElement('div');
          div.className = 'msg ' + role;
          div.textContent = text;
          messagesEl.appendChild(div);
          messagesEl.scrollTop = messagesEl.scrollHeight;
          return div;
        }}

        function appendError(text) {{
          appendMessage('assistant error', text);
        }}

        // 非 200 / 非 JSON 一律转成可读的中文提示，绝不让空值被渲染到页面上。
        async function postForm(url, params) {{
          const resp = await fetch(url, {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
            body: new URLSearchParams(params)
          }});
          let data = null;
          try {{
            data = await resp.json();
          }} catch (err) {{
            data = null;
          }}
          if (!resp.ok) {{
            const detail = (data && data.detail) ? String(data.detail) : ('HTTP ' + resp.status);
            throw new Error(detail);
          }}
          return data || {{}};
        }}

        async function startSession() {{
          let data;
          try {{
            data = await postForm('/candidate/start', {{ session_id: sessionId }});
          }} catch (err) {{
            appendError('测评加载失败：' + (err && err.message ? err.message : '未知错误')
              + '。请刷新页面重试；若仍失败，请联系 HR 重新发送测评链接。');
            return;
          }}
          if (data.error) {{
            appendError(data.error);
          }}
          if (data.first_question) {{
            appendMessage('assistant', data.first_question);
          }} else if (!data.error) {{
            appendError('没有取到测评开场白，请刷新页面重试，或联系 HR。');
          }}
        }}

        async function submitAnswer(event) {{
          event.preventDefault();
          const text = input.value.trim();
          if (!text) return;
          appendMessage('user', text);
          input.value = '';

          let data;
          try {{
            data = await postForm('/assessment/next', {{ session_id: sessionId, user_answer: text }});
          }} catch (err) {{
            appendError('发送失败：' + (err && err.message ? err.message : '未知错误')
              + '。请稍后重试。');
            return;
          }}
          // /assessment/next 返回的字段是 message（与 assessment_service.handle_turn 一致）
          appendMessage('assistant', data.message || '（本次没有回复内容，请再发一次）');

          // 如果后端返回已结束，隐藏输入框
          if (data.status === 'finished') {{
            form.style.display = 'none';
          }}
        }}

        form.addEventListener('submit', submitAnswer);
        startSession();
      </script>
    </body>
    </html>
    """
