import html
from contextlib import asynccontextmanager
from typing import Optional
from pathlib import Path

import gradio as gr
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from resume_parser import parse_resume_text
from config import settings
from app.gradio_ui import HR_UI_CSS, build_hr_ui
from app.paths import data_root
from app.assessment_service import AssessmentService
from app.knowledge_base import get_job_knowledge, initialize_knowledge_base, list_jobs
from app.resume_reader import read_resume_text
from app.session_store import create_session, get_session, init_db, list_sessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    initialize_knowledge_base()
    await assessment_service.prepare_job_knowledge()
    yield


app = FastAPI(title="Resume Assessment Agent", lifespan=lifespan)
assessment_service = AssessmentService()
init_db()
initialize_knowledge_base()


def resolve_public_base_url(request: Request | None = None) -> str:
    """候选人链接前缀，按优先级解析：
    1) settings.PUBLIC_BASE_URL（.env 显式设置）
    2) 当前请求的 host + scheme（支持 X-Forwarded-Proto/Host，反代场景，如魔搭）
    3) request.base_url（直接访问 FastAPI，无反代）
    """
    explicit = settings.PUBLIC_BASE_URL.strip().rstrip("/") if settings.PUBLIC_BASE_URL else ""
    if explicit:
        return explicit
    if request is not None:
        proto = (
            request.headers.get("x-forwarded-proto")
            or request.headers.get("x-forwarded-protocol")
            or "http"
        ).split(",")[0].strip()
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or ""
        ).split(",")[0].strip()
        if host:
            return f"{proto}://{host}"
        return str(request.base_url).rstrip("/")
    return ""


def render_hr_page() -> str:
    job_options = "\n".join(
        f"<option>{html.escape(job_name)}</option>"
        for job_name in list_jobs(ready_only=True)
    )
    return """
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>HR 上传简历</title>
        <style>
            body { font-family: Arial, sans-serif; background: linear-gradient(180deg, #f4f7ff, #eef5ff); margin: 0; padding: 32px; }
            .layout { max-width: 1100px; margin: 0 auto; display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 24px; }
            .card { background: white; border-radius: 18px; padding: 28px; box-shadow: 0 12px 28px rgba(26,42,73,0.08); }
            h1 { margin-top: 0; }
            form { display: grid; gap: 16px; }
            label { display: grid; gap: 8px; font-weight: 600; }
            input, select, button { font-size: 16px; padding: 12px 14px; border-radius: 10px; border: 1px solid #d9deeb; }
            button { background: linear-gradient(135deg, #2858ff, #1d4ed8); color: white; border: none; cursor: pointer; }
            .result { margin-top: 20px; padding: 18px; background: #eef3ff; border-radius: 12px; display:none; }
            a { color: #2457f5; word-break: break-all; }
            table { width: 100%; border-collapse: collapse; margin-top: 18px; }
            th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #edf0f7; }
            .status { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }
            .running { background: #fff5da; color: #9a6700; }
            .done { background: #e8f9ef; color: #0d7a4d; cursor: pointer; }
            .done:hover { opacity: 0.8; }
            .empty { color: #75829a; }
            .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; }
            .modal-content { background: white; max-width: 700px; margin: 50px auto; border-radius: 16px; padding: 24px; max-height: 80vh; overflow-y: auto; }
            .modal-close { float: right; cursor: pointer; font-size: 20px; }
        </style>
    </head>
    <body>
      <div class="layout">
        <div class="card">
          <h1>简历评测 HR 上传</h1>
          <form id="hr-form">
            <label>候选人姓名 <input name="candidate_name" required placeholder="例如：张三" /></label>
            <label>岗位名称 <select name="job_name">""" + job_options + """</select></label>
            <label>上传简历 <input type="file" name="file" accept=".pdf,.doc,.docx,.txt" required /></label>
            <button type="submit">生成候选人链接</button>
          </form>
          <div id="result" class="result"></div>
        </div>

        <div class="card">
          <h2>候选人筛选列表</h2>
          <table>
            <thead>
              <tr>
                <th>候选人</th>
                <th>岗位</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody id="sessions-body"></tbody>
          </table>
        </div>
      </div>
      
      <!-- 结果弹窗 -->
      <div id="resultModal" class="modal">
        <div class="modal-content">
          <span class="modal-close" onclick="closeModal()">&times;</span>
          <h3>测评结果</h3>
          <div id="modal-body"></div>
        </div>
      </div>
      
      <script>
        const form = document.getElementById('hr-form');
        const resultBox = document.getElementById('result');
        const sessionsBody = document.getElementById('sessions-body');
        const modal = document.getElementById('resultModal');

        async function loadSessions() {
          const resp = await fetch('/hr/sessions');
          const data = await resp.json();
          if (!data.length) {
            sessionsBody.innerHTML = '<tr><td colspan="3" class="empty">暂无候选人数据</td></tr>';
            return;
          }
          sessionsBody.innerHTML = data.map(item => {
            const isDone = item.status === 'finished';
            const nameHtml = isDone 
              ? `<span style="color:#333">${item.candidate_name || '未知'}</span>`
              : `<a href="/candidate/${item.session_id}" target="_blank">${item.candidate_name || '未知'}</a>`;
            const statusHtml = isDone
              ? `<span class="status done" onclick="viewResult('${item.session_id}')">已完成</span>`
              : `<span class="status running">进行中</span>`;
            return `<tr><td>${nameHtml}</td><td>${item.job_name || '未填写'}</td><td>${statusHtml}</td></tr>`;
          }).join('');
        }
        
        async function viewResult(sessionId) {
          const resp = await fetch(`/hr/result/${sessionId}`);
          const data = await resp.json();
          document.getElementById('modal-body').innerHTML = `
            <p><strong>候选人：</strong>${data.candidate_name}</p>
            <p><strong>岗位：</strong>${data.job_name}</p>
            <p><strong>最终结论：</strong>${data.final_decision}</p>
            <p><strong>总结理由：</strong></p>
            <p style="white-space:pre-wrap; background:#f5f7fa; padding:12px; border-radius:8px;">${data.summary}</p>
          `;
          modal.style.display = 'block';
        }
        
        function closeModal() {
          modal.style.display = 'none';
        }
        
        window.onclick = function(event) {
          if (event.target == modal) closeModal();
        }

        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const formData = new FormData(form);
          const resp = await fetch('/hr/parse-link', { method: 'POST', body: formData });
          const data = await resp.json();
          resultBox.style.display = 'block';
          resultBox.innerHTML = `
            <div><strong>session_id:</strong> ${data.session_id}</div>
            <div><strong>候选人链接:</strong> <a href="${data.public_link}" target="_blank">${data.public_link}</a></div>
            <div><strong>岗位:</strong> ${data.job_name}</div>
          `;
          await loadSessions();
        });

        loadSessions();
      </script>
    </body>
    </html>
    """


def render_candidate_page(session_id: str) -> str:
    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>候选人测评</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f3f5fb; margin: 0; padding: 24px; }}
            .panel {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 18px; padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,0.08); }}
            h2 {{ margin-top: 0; }}
            .messages {{ display: grid; gap: 12px; margin: 20px 0; }}
            .msg {{ max-width: 75%; padding: 12px 14px; border-radius: 12px; line-height: 1.6; }}
            .assistant {{ background: #edf3ff; justify-self: start; }}
            .user {{ background: #deffeb; justify-self: end; }}
            form {{ display: flex; gap: 10px; margin-top: 16px; }}
            input {{ flex: 1; padding: 12px 14px; border: 1px solid #d5dbee; border-radius: 10px; }}
            button {{ background: #2457f5; color: white; border: none; border-radius: 10px; padding: 12px 20px; cursor: pointer; }}
            .result-box {{ margin-top: 18px; background: #eef8f1; border-radius: 12px; padding: 16px; display: none; }}
        </style>
    </head>
    <body>
      <div class="panel">
        <h2>途途测评</h2>
        <div id="meta"></div>
        <div id="messages" class="messages"></div>
        <form id="chat-form">
          <input id="answer" autocomplete="off" placeholder="请输入你的回答" />
          <button type="submit">发送</button>
        </form>
        <div id="result-box" class="result-box"></div>
      </div>
      <script>
        const sessionId = '{session_id}';
        const messagesEl = document.getElementById('messages');
        const metaEl = document.getElementById('meta');
        const resultBox = document.getElementById('result-box');
        const form = document.getElementById('chat-form');
        const input = document.getElementById('answer');

        function appendMessage(role, text) {{
          const div = document.createElement('div');
          div.className = 'msg ' + role;
          div.textContent = text;
          messagesEl.appendChild(div);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }}

        async function startSession() {{
          const resp = await fetch('/candidate/start', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
            body: new URLSearchParams({{ session_id: sessionId }})
          }});
          const data = await resp.json();
          metaEl.innerHTML = `<strong>候选人:</strong> ${{data.candidate_name}} &nbsp; <strong>岗位:</strong> ${{data.job_name}}`;
          appendMessage('assistant', data.first_question);
        }}

        async function submitAnswer(event) {{
          event.preventDefault();
          const text = input.value.trim();
          if (!text) return;
          appendMessage('user', text);
          input.value = '';

          const resp = await fetch('/assessment/next', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
            body: new URLSearchParams({{ session_id: sessionId, user_answer: text }})
          }});
          const data = await resp.json();
          appendMessage('assistant', data.next_question);

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


@app.get("/")
async def read_root(session_id: Optional[str] = None, admin_pwd: Optional[str] = None) -> dict:
    if session_id:
        session = get_session(session_id)
        if session:
            payload = await assessment_service.launch_candidate_session(session_id, session)
            return payload
        raise HTTPException(status_code=404, detail="Invalid session_id")

    if admin_pwd == settings.ADMIN_PASSWORD:
        return {
            "mode": "hr",
            "message": "HR admin access granted",
            "admin_username": settings.ADMIN_USERNAME,
        }

    return {
        "message": "Resume Assessment Agent is running",
        "hint": "Use HR admin link or candidate session link",
    }


@app.post("/hr/login")
async def hr_login(username: str = Form(...), password: str = Form(...)) -> dict:
    if username != settings.ADMIN_USERNAME or password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid HR credentials")
    return {"status": "ok", "message": "HR login successful"}


@app.post("/hr/parse-link")
async def hr_parse_link(
    request: Request,
    file: UploadFile = File(...),
    candidate_name: str = Form(...),
    job_name: str = Form(...),
) -> dict:
    try:
        get_job_knowledge(job_name)
    except KeyError as error:
        raise HTTPException(status_code=400, detail="请选择知识库中的有效岗位。") from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail="该岗位筛选规则尚未生成，请检查模型配置并重启服务。") from error

    file_bytes = await file.read()
    upload_dir = data_root() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / Path(file.filename).name
    with temp_path.open("wb") as f:
        f.write(file_bytes)

    resume_text = await read_resume_text(str(temp_path))
    profile = parse_resume_text(resume_text)
    session_id = create_session(
        candidate_name=candidate_name,
        job_name=job_name,
        resume_json=profile.model_dump(),
        candidate_summary=resume_text[:1500],
    )

    public_link = f"{resolve_public_base_url(request)}/candidate/{session_id}"
    return {
        "status": "ok",
        "session_id": session_id,
        "candidate_name": candidate_name,
        "job_name": job_name,
        "candidate_data": profile.model_dump(),
        "public_link": public_link,
    }


@app.get("/hr/sessions")
async def hr_sessions() -> list[dict]:
    return list_sessions()


@app.get("/hr/result/{session_id}")
async def hr_result(session_id: str) -> dict:
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session_id,
        "candidate_name": session.get("candidate_name"),
        "job_name": session.get("job_name"),
        "final_decision": session.get("final_decision", "未完成"),
        "summary": session.get("summary", "暂无总结"),
        "status": session.get("status"),
    }


@app.get("/candidate/{session_id}")
async def candidate_page(session_id: str) -> HTMLResponse:
    session = get_session(session_id)
    if not session:
        # 返回友好 HTML 页面，避免手机浏览器看到裸 JSON
        return HTMLResponse(
            content="""
            <!doctype html>
            <html lang="zh-CN">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>测评链接已失效</title>
                <style>
                    body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
                           background: #f3f5fb; display: flex; justify-content: center; align-items: center;
                           min-height: 100vh; margin: 0; padding: 24px; }
                    .panel { background: white; border-radius: 18px; padding: 40px 32px;
                             box-shadow: 0 8px 30px rgba(0,0,0,0.08); text-align: center; max-width: 480px; }
                    h2 { margin-top: 0; color: #1f2937; }
                    p { color: #4b5563; line-height: 1.7; }
                    .reason { background: #f8fafc; border-radius: 10px; padding: 14px 16px;
                              color: #475569; font-size: 14px; line-height: 1.6; text-align: left; }
                </style>
            </head>
            <body>
                <div class="panel">
                    <h2>测评链接已失效</h2>
                    <p>这个测评链接找不到对应的候选人记录。</p>
                    <div class="reason">
                        <strong>可能原因：</strong><br>
                        • 测评链接不是从本工作台生成的（在别的环境创建）<br>
                        • 服务重启过，会话数据未持久化<br>
                        • 链接复制时被截断
                    </div>
                    <p style="margin-top:20px">请回到 HR 工作台，让 HR 重新生成候选人链接。</p>
                </div>
            </body>
            </html>
            """,
            status_code=404,
            media_type="text/html",
        )

    # 已完成的测评直接显示结束页面
    if session.get("status") == "finished":
        return HTMLResponse(content="""
        <!doctype html>
        <html lang="zh-CN">
        <head>
            <meta charset="utf-8" />
            <title>测评已结束</title>
            <style>
                body { font-family: Arial, sans-serif; background: #f3f5fb; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
                .panel { background: white; border-radius: 18px; padding: 40px; box-shadow: 0 8px 30px rgba(0,0,0,0.08); text-align: center; }
            </style>
        </head>
        <body>
            <div class="panel">
                <h2>测评已结束</h2>
                <p>感谢你的参与，结果将由HR后续通知。</p>
            </div>
        </body>
        </html>
        """, media_type="text/html")
    
    return HTMLResponse(content=render_candidate_page(session_id), media_type="text/html")


@app.post("/resume/parse")
async def parse_resume(file: UploadFile = File(...)) -> dict:
    return await assessment_service.parse_resume(file)


@app.get("/session/{session_id}")
async def get_session_by_id(session_id: str) -> dict:
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "status": "ok",
        "session": session,
    }


@app.post("/candidate/start")
async def candidate_start(session_id: str = Form(...)) -> dict:
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await assessment_service.launch_candidate_session(session_id, session)


@app.post("/assessment/start")
async def start_assessment(candidate_id: str = Form(...), job_name: str = Form(...)) -> dict:
    return await assessment_service.start_assessment(candidate_id, job_name)


@app.post("/assessment/next")
async def next_question(session_id: str = Form(...), user_answer: str = Form(...)) -> dict:
    return await assessment_service.next_question(session_id, user_answer)


@app.post("/assessment/finish")
async def finish_assessment(session_id: str = Form(...)) -> dict:
    return await assessment_service.finish_assessment(session_id)


app = gr.mount_gradio_app(
    app,
    build_hr_ui(),
    # 把 Gradio 挂到根路径（魔搭 sdk=gradio 要求 /config /invoke /info 在根），
    # FastAPI 路由（/candidate /assessment /hr-parse-link /session 等）不冲突，照常工作
    path="/",
    theme=gr.themes.Soft(primary_hue="blue", neutral_hue="slate"),
    css=HR_UI_CSS,
)
