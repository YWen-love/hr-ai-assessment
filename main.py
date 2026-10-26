import html
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional
from pathlib import Path

import gradio as gr
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles

from resume_parser import parse_resume_text
from config import settings
from app.gradio_ui import HR_UI_CSS, build_job_manager_ui, build_workbench_ui
from app.hr_auth import init_db as init_hr_db, seed_default_admin, verify_login, create_user
from app.login_page import render_login_page, render_register_page
from app.paths import data_root, resource_path
from app.assessment_service import AssessmentError, AssessmentService
from app.job_config_store import (
    JobConfigError,
    delete_job_config,
    get_job_config,
    get_job_config_object,
    list_job_configs,
    save_job_config,
    seed_from_files,
    update_job_config_meta,
)
from app.job_generator import JobConfigGenerator, JobGenerationError
from app.job_schema import is_valid_job_key
from app.knowledge_base import get_job_knowledge, initialize_knowledge_base, list_jobs
from app.rag_bypass import init_rag_store
from app.resume_reader import read_resume_text
from app.session_store import create_session, get_candidate_view, get_session, init_db, list_sessions

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_hr_db()
    seed_default_admin()
    initialize_knowledge_base()
    # 岗位配置从 job_configs/*.json 种子化入库；HR 在后台改的是数据库，文件是初值
    seeded = seed_from_files(resource_path("job_configs"))
    if seeded:
        logger.info("已导入岗位配置：%s", ", ".join(seeded))
    init_rag_store()
    yield


app = FastAPI(title="Resume Assessment Agent", lifespan=lifespan)


# 强制登录中间件（INNER）：/hr/* 未登录 → 401 JSON / 302 跳登录页；候选人 /candidate/* 与静态资源放行
# 用 BaseHTTPMiddleware 类形式，确保可在 SessionMiddleware 之后被注册（晚注册 = 更外层）
class RequireHRAuthMiddleware(BaseHTTPMiddleware):
    # 注意："/" 单独处理（path == "/"），其余都是前缀匹配
    EXACT_PUBLIC = {"/"}
    PUBLIC_PREFIXES = ("/login", "/register", "/api/login", "/api/register", "/api/logout", "/static", "/candidate/", "/docs", "/openapi.json")

    async def dispatch(self, request, call_next):
        path = request.url.path
        # 公开路径白名单
        if path in self.EXACT_PUBLIC:
            return await call_next(request)
        for prefix in self.PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)
        # /hr/* 强制鉴权（Gradio 内部 API 也走这里）
        if path.startswith("/hr"):
            if not request.session.get("hr_user_id"):
                accept = request.headers.get("accept", "")
                if "application/json" in accept or path.startswith("/hr/api/"):
                    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
                return RedirectResponse(url=f"/login?next={path}", status_code=302)
        return await call_next(request)


# 注册顺序：先注册的在内层、后注册的在更外层（包裹前一个）
# 我们要 SessionMiddleware 在最外层（先于 auth 跑、填充 session）
# 所以：先 add auth middleware（内层），再 add session（外层）
app.add_middleware(RequireHRAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    session_cookie=settings.SESSION_COOKIE_NAME,
    max_age=settings.SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,
)


# HR 工作台页面注入：关闭标签页自动登出
# 监听 pagehide（标签页关闭、浏览器关闭都触发；副作用：刷新也会登出）
# 用 sendBeacon 在 pagehide 期间最可靠；fetch keepalive 兜底
_AUTO_LOGOUT_SCRIPT = (
    "<script>(function(){"
    "function l(){"
    "try{"
    "var s=navigator.sendBeacon&&navigator.sendBeacon('/api/logout');"
    "if(!s){fetch('/api/logout',{method:'POST',keepalive:true}).catch(function(){});}"
    "}catch(e){}"
    "}"
    "window.addEventListener('pagehide',l);"
    "window.addEventListener('beforeunload',l);"
    "})();</script>"
)


class InjectLogoutJSMiddleware(BaseHTTPMiddleware):
    """给 /hr 路径的 HTML 响应注入 pagehide 自动登出脚本。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/hr"):
            return response
        ct = response.headers.get("content-type", "")
        if "text/html" not in ct:
            return response

        # 读取整个 body
        chunks = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                chunks.append(chunk.encode("utf-8"))
            else:
                chunks.append(chunk)
        body = b"".join(chunks)

        # 在 </head> 前注入（大小写都匹配；找不到就插到 body 开头）
        marker = b"</head>"
        idx = body.lower().find(marker)
        if idx == -1:
            new_body = _AUTO_LOGOUT_SCRIPT.encode("utf-8") + body
        else:
            new_body = body[:idx] + _AUTO_LOGOUT_SCRIPT.encode("utf-8") + body[idx:]

        # 重建响应，删掉旧的 content-length
        new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=new_headers,
            media_type="text/html",
        )


app.add_middleware(InjectLogoutJSMiddleware)

# 静态资源（背景图 bg.jpg 放在 <data_root>/static/ 下，自动随魔搭/本地路径解析）
STATIC_DIR = data_root() / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

assessment_service = AssessmentService()
init_db()
init_hr_db()
seed_default_admin()
initialize_knowledge_base()


def _require_hr_user(request: Request) -> str:
    """FastAPI Depends：要求已登录 HR。"""
    user = request.session.get("hr_user_id")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _set_hr_session(request: Request, username: str) -> None:
    """写入 HR 登录 session。登录、注册成功后调用。"""
    request.session["hr_user_id"] = username
    request.session["hr_login_at"] = int(time.time())


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
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 24px;
                min-height: 100vh;
                background:
                    linear-gradient(rgba(255,255,255,0.82), rgba(255,255,255,0.82)),
                    url('/static/测评端.jpg') center/cover no-repeat fixed;
                background-attachment: fixed;
            }}
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
async def read_root(session_id: Optional[str] = None, admin_pwd: Optional[str] = None):
    if session_id:
        # 会话已在 /assessment/start 建好并写入首轮开场白，
        # 这里只回读，不重新生成 —— 刷新页面不该让对话重来一遍。
        session = get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Invalid session_id")
        return {
            "mode": "candidate",
            "session_id": session_id,
            "job_name": session.get("job_name"),
            "candidate_name": session.get("candidate_name"),
            "status": session.get("status"),
            "candidate_msg": session.get("candidate_msg"),
        }

    if admin_pwd == settings.ADMIN_PASSWORD:
        return {
            "mode": "hr",
            "message": "HR admin access granted",
            "admin_username": settings.ADMIN_USERNAME,
        }

    # 魔搭 Studio 预览默认加载根路径：返回自动跳转到 HR 工作台的页面。
    # 保持 HTTP 200 以通过平台健康检查，同时用户打开项目即进入工作台（/hr 未登录会再跳 /login）。
    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='0;url=/hr'>"
            "<title>HR 工作台</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding-top:80px;color:#555'>"
            "<p>正在跳转 HR 工作台…</p>"
            "<p><a href='/hr'>如果未自动跳转，点击这里</a></p>"
            "</body></html>"
        ),
        media_type="text/html",
    )


@app.get("/login")
async def login_page(request: Request, next: str = "/hr") -> HTMLResponse:
    """登录页（照片背景 + 表单）。"""
    return HTMLResponse(content=render_login_page(next_url=next or "/hr", error=""), media_type="text/html")


@app.post("/api/login")
async def api_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/hr"),
) -> HTMLResponse:
    """登录：bcrypt 校验 → 写 session → 302 回跳。失败回 401 渲染错误。"""
    target = next if next.startswith("/") else "/hr"
    if verify_login(username, password):
        _set_hr_session(request, username)
        return RedirectResponse(url=target, status_code=302)
    return HTMLResponse(
        content=render_login_page(next_url=target, error="账号或密码错误，请重试"),
        status_code=401,
        media_type="text/html",
    )


@app.get("/register")
async def register_page(request: Request, next: str = "/hr") -> HTMLResponse:
    """注册页（与登录页同款背景图 + 卡片）。"""
    return HTMLResponse(
        content=render_register_page(next_url=next or "/hr"),
        media_type="text/html",
    )


@app.post("/api/register")
async def api_register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    next: str = Form("/hr"),
) -> HTMLResponse:
    """自注册：校验 → 写库 → 自动登录 → 302 回跳。失败回 400 渲染错误。"""
    target = next if next.startswith("/") else "/hr"

    if password != confirm_password:
        return HTMLResponse(
            content=render_register_page(next_url=target, error="两次输入的密码不一致", username=username),
            status_code=400,
            media_type="text/html",
        )

    ok, msg = create_user(username, password)
    if not ok:
        return HTMLResponse(
            content=render_register_page(next_url=target, error=msg, username=username),
            status_code=400,
            media_type="text/html",
        )

    _set_hr_session(request, username)
    return RedirectResponse(url=target, status_code=302)


@app.post("/api/logout")
async def api_logout(request: Request) -> RedirectResponse:
    """登出：清 session，跳回登录页。"""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.post("/hr/parse-link")
async def hr_parse_link(
    request: Request,
    file: UploadFile = File(...),
    candidate_name: str = Form(...),
    job_name: str = Form(...),
    _user: str = Depends(_require_hr_user),
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
        resume_json=profile,
        candidate_summary=resume_text[:1500],
    )

    public_link = f"{resolve_public_base_url(request)}/candidate/{session_id}"
    return {
        "status": "ok",
        "session_id": session_id,
        "candidate_name": candidate_name,
        "job_name": job_name,
        "candidate_data": profile,
        "public_link": public_link,
    }


@app.get("/hr/sessions")
async def hr_sessions(_user: str = Depends(_require_hr_user)) -> list[dict]:
    return list_sessions()


@app.get("/hr/result/{session_id}")
async def hr_result(session_id: str, _user: str = Depends(_require_hr_user)) -> dict:
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
async def parse_resume(file: UploadFile = File(...), job_key: str = Form(...)) -> dict:
    """解析简历。必须带 job_key —— 经验标签是岗位驱动的封闭词表。"""
    upload_dir = data_root() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / Path(file.filename or "resume.txt").name
    temp_path.write_bytes(await file.read())

    resume_text = await read_resume_text(str(temp_path))
    tags = await assessment_service.resume_parser.parse(resume_text, _require_config(job_key))
    return {"status": "ok", "candidate": tags}


def _require_config(job_key: str) -> dict:
    """取岗位配置，缺失直接 400。配置错了不该等到解析完才发现。"""
    config = get_job_config_object(job_key)
    if config is None:
        raise HTTPException(status_code=400, detail=f"岗位配置不存在或已删除：{job_key}")
    return config


@app.get("/session/{session_id}")
async def get_session_by_id(session_id: str) -> dict:
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "status": "ok",
        "session": session,
    }


@app.get("/debug/ai-status")
async def debug_ai_status(test: bool = True) -> dict:
    """诊断 AI 配置：在浏览器打开 https://your-space.ms.show/debug/ai-status
    会显示：key 是否读到、base_url / model 是什么、（可选）实际调一次 AI 看回什么错。
    不暴露完整 key。"""
    from app.llm_client import LLMClient
    client = LLMClient()
    status = client.config_status()
    if test:
        try:
            status["test_call"] = (await client.chat("你是助手。", "回复两个字：正常"))[:200]
        except Exception as exc:
            status["test_call"] = f"调用失败：{type(exc).__name__}: {str(exc)[:300]}"
    return status


@app.post("/assessment/start")
async def start_assessment(
    job_key: str = Form(...),
    file: UploadFile = File(...),
    candidate_name: str = Form(""),
) -> dict:
    """上传简历开始测评。"""
    upload_dir = data_root() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / Path(file.filename or "resume.txt").name
    temp_path.write_bytes(await file.read())

    resume_text = await read_resume_text(str(temp_path))
    try:
        return await assessment_service.start(
            job_key=job_key, resume_text=resume_text, candidate_name=candidate_name
        )
    except AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/assessment/next")
async def next_question(session_id: str = Form(...), user_answer: str = Form(...)) -> dict:
    """候选人回一句话。内部会自动判断是否触发 RAG 旁路与是否收尾。"""
    try:
        return await assessment_service.handle_turn(session_id, user_answer)
    except AssessmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/assessment/finish")
async def finish_assessment(session_id: str = Form(...)) -> dict:
    try:
        return await assessment_service.finish(session_id)
    except AssessmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/hr/report/{session_id}")
async def hr_report(
    session_id: str,
    _user: str = Depends(_require_hr_user),
) -> dict:
    """完整测评报告：静态分 + 对话意向 + 对话摘要 + 备选岗位推荐。

    与 /hr/result 的区别：这里用组装层把三层数据合并成一份可读结论。
    """
    return assessment_service.build_report(session_id)


@app.get("/candidate/result/{session_id}")
async def candidate_result(session_id: str) -> dict:
    """候选人侧结果。物理上只返回 candidate_msg，不含分数与备选岗。"""
    view = get_candidate_view(session_id)
    if not view:
        raise HTTPException(status_code=404, detail="Session not found")
    return view


# 挂载顺序是硬约束，不能调换：Starlette 的 Mount 正则是 ^/hr/jobs/(?P<path>.*)$，
# 而 ^/hr/(?P<path>.*)$ 会把 /hr/jobs 也完整匹配掉。所以岗位管理必须先挂，
# 工作台后挂，否则 /hr/jobs/ 会被工作台吞掉。
_HR_THEME = gr.themes.Soft(primary_hue="blue", neutral_hue="slate")

# 手敲 /hr/jobs（不带尾斜杠）要走这里：它不匹配上面的 Mount，会掉进
# /hr mount 当成工作台的子路径，直接 404。挂载前的普通路由优先于 Mount。
# 页面里的导航链接本来就带斜杠，但登录跳转的 ?next= 不会 —— session 过期
# 落回来时带的就是用户原来敲的地址。
@app.get("/hr/jobs", include_in_schema=False)
async def redirect_job_manager() -> RedirectResponse:
    return RedirectResponse(url="/hr/jobs/", status_code=307)

app = gr.mount_gradio_app(
    app,
    build_job_manager_ui(),
    # HR 工作台挂到 /hr：避开 FastAPI @app.get("/")（返回 JSON health check），
    # 这样 /hr 真正显示 Gradio UI，/ 仍然是轻量 health check 端点。
    # FastAPI 的 /candidate /assessment /hr/* 路由不受影响。
    path="/hr/jobs",
    theme=_HR_THEME,
    css=HR_UI_CSS,
)

app = gr.mount_gradio_app(
    app,
    build_workbench_ui(),
    path="/hr",
    theme=_HR_THEME,
    css=HR_UI_CSS,
)


# ---------------------------------------------------------------------------
# 岗位配置管理（HR 后台）
# ---------------------------------------------------------------------------
# 全部要求登录。生成与保存是两步：/generate 只做预览不落库，
# HR 改完预览里的分值/阈值，再调 /save 落库 —— 落库默认 pending，
# 还要再调 /review 标记复核，两个动作不能合并。
# 合并的话「AI 生成的配置」和「HR 确认过的配置」就再也分不开了。

@app.get("/api/job/list")
async def api_job_list(
    include_deleted: bool = False,
    _user: str = Depends(_require_hr_user),
) -> dict:
    configs = list_job_configs(include_deleted=include_deleted)
    return {"items": configs, "total": len(configs)}


@app.get("/api/job/{job_key}")
async def api_job_get(
    job_key: str,
    include_deleted: bool = False,
    _user: str = Depends(_require_hr_user),
) -> dict:
    record = get_job_config(job_key, include_deleted=include_deleted)
    if not record:
        raise HTTPException(status_code=404, detail=f"岗位不存在：{job_key}")
    return record


@app.post("/api/job/generate")
async def api_job_generate(
    request: Request,
    _user: str = Depends(_require_hr_user),
) -> dict:
    """JD → 配置预览。**不落库。**"""
    payload = await _read_json(request)
    jd_text = str(payload.get("jd") or "").strip()
    if not jd_text:
        raise HTTPException(status_code=400, detail="jd 不能为空")

    try:
        result = await JobConfigGenerator().generate(
            jd_text, job_key=payload.get("job_key")
        )
    except JobGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if result["errors"]:
        raise HTTPException(status_code=422, detail=result["errors"])
    return result


@app.post("/api/job/save")
async def api_job_save(
    request: Request,
    _user: str = Depends(_require_hr_user),
) -> dict:
    """保存岗位配置。source=auto_llm 表示来自 JD 生成，默认 pending 待复核。"""
    payload = await _read_json(request)
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="config 必须是对象")

    source_type = str(payload.get("source_type") or "manual")
    raw_jd = payload.get("raw_jd")
    # 待复核状态由 review_status 显式传入，不从 config 里读 ——
    # config 里的 review_status 是模型给的，不能当成人的意思
    review_status = str(payload.get("review_status") or "pending")

    try:
        return save_job_config(
            config,
            source_type=source_type,
            raw_jd=str(raw_jd) if raw_jd else None,
            review_status=review_status,
        )
    except JobConfigError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc


@app.post("/api/job/{job_key}/review")
async def api_job_review(
    job_key: str,
    request: Request,
    _user: str = Depends(_require_hr_user),
) -> dict:
    """人工复核标记。pending → reviewed。

    仅供追溯「这份规则谁确认过」。测评链路不读这个字段，
    pending 的配置同样可以正常发起测评。
    """
    payload = await _read_json(request)
    review_status = str(payload.get("review_status") or "")
    if review_status not in ("pending", "reviewed"):
        raise HTTPException(status_code=400, detail="review_status 必须是 pending 或 reviewed")
    if not get_job_config(job_key):
        raise HTTPException(status_code=404, detail=f"岗位不存在：{job_key}")
    return update_job_config_meta(job_key, review_status=review_status)


@app.post("/api/job/{job_key}/copy")
async def api_job_copy(
    job_key: str,
    request: Request,
    _user: str = Depends(_require_hr_user),
) -> dict:
    """复制岗位配置。复制品一律 pending —— 改过了就得重看一遍。"""
    payload = await _read_json(request)
    source = get_job_config_object(job_key)
    if not source:
        raise HTTPException(status_code=404, detail=f"岗位不存在：{job_key}")

    new_key = str(payload.get("job_key") or "").strip() or f"{job_key}_copy"
    if not is_valid_job_key(new_key):
        raise HTTPException(status_code=400, detail="job_key 只能包含小写字母、数字和下划线")
    if get_job_config(new_key):
        raise HTTPException(status_code=409, detail=f"岗位已存在：{new_key}")

    duplicate = dict(source)
    duplicate["job_key"] = new_key
    if payload.get("job_name"):
        duplicate["job_name"] = str(payload["job_name"])
    # 一岗一库：复制品必须换 rag_doc_id，否则两个岗位共用一份知识库，
    # 违反 RAG 约定 4（各岗位独立 rag_doc_id 隔离）。
    duplicate["rag_doc_id"] = new_key
    duplicate["review_status"] = "pending"

    try:
        return save_job_config(duplicate, source_type="manual", review_status="pending")
    except JobConfigError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc


@app.delete("/api/job/{job_key}")
async def api_job_delete(
    job_key: str,
    hard: bool = False,
    _user: str = Depends(_require_hr_user),
) -> dict:
    """默认软删。历史测评记录按 job_key 关联岗位，硬删会留下悬空引用。"""
    if not delete_job_config(job_key, hard=hard):
        raise HTTPException(status_code=404, detail=f"岗位不存在或已删除：{job_key}")
    return {"job_key": job_key, "hard_deleted": hard}


async def _read_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return payload
