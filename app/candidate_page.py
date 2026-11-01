"""候选人测评页（HTML 渲染）。

与 app/login_page.py 同一套路：内联 HTML + <style> + <script>，避免外部依赖。

**候选人 / 岗位由服务端直接渲染进 HTML**，不再依赖前端再调一次接口取。
原因：头部一旦依赖接口，接口任何一次非 200（路由不存在、跨站拦截、
岗位配置被删）都会让页面显示"候选人: undefined 岗位: undefined" ——
而这两个值本来就在数据库里躺着，渲染页面时已经拿到了。
前端仍然调 /candidate/start 取开场白，取不到时给出明确提示，
不再把 undefined 渲染到页面上。
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, Optional

# 背景图基础 URL（由 main.py 把 /static 目录 mount 到这里）
BG_URL = "/static/测评端.jpg"


def _safe(value: Optional[str], default: str = "") -> str:
    return html.escape(str(value)) if value else default


def render_candidate_page(session_id: str, session: Optional[Dict[str, Any]] = None) -> str:
    """渲染候选人测评页。

    Args:
        session_id: 会话 ID（拼进 JS，必须走 JSON 转义，不能裸拼进引号里）
        session: 会话记录（sessions 表一行）。取 candidate_name / job_name 渲染头部。
                 传空也能渲染 —— 头部显示占位符，不显示 undefined。
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
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 24px;
                min-height: 100vh;
                background:
                    linear-gradient(rgba(255,255,255,0.82), rgba(255,255,255,0.82)),
                    url('{BG_URL}') center/cover no-repeat fixed;
                background-attachment: fixed;
            }}
            .panel {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 18px; padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,0.08); }}
            h2 {{ margin-top: 0; }}
            .messages {{ display: grid; gap: 12px; margin: 20px 0; }}
            .msg {{ max-width: 75%; padding: 12px 14px; border-radius: 12px; line-height: 1.6; }}
            .assistant {{ background: #edf3ff; justify-self: start; }}
            .user {{ background: #deffeb; justify-self: end; }}
            .msg.error {{ background: #fff1f0; color: #b42318; }}
            form {{ display: flex; gap: 10px; margin-top: 16px; }}
            input {{ flex: 1; padding: 12px 14px; border: 1px solid #d5dbee; border-radius: 10px; }}
            button {{ background: #2457f5; color: white; border: none; border-radius: 10px; padding: 12px 20px; cursor: pointer; }}
            .result-box {{ margin-top: 18px; background: #eef8f1; border-radius: 12px; padding: 16px; display: none; }}
        </style>
    </head>
    <body>
      <div class="panel">
        <h2>途途测评</h2>
        <div id="meta"><strong>候选人:</strong> {candidate_name} &nbsp; <strong>岗位:</strong> {job_name}</div>
        <div id="messages" class="messages"></div>
        <form id="chat-form">
          <input id="answer" autocomplete="off" placeholder="请输入你的回答" />
          <button type="submit">发送</button>
        </form>
        <div id="result-box" class="result-box"></div>
      </div>
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
