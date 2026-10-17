from __future__ import annotations

import html
import logging
from pathlib import Path

import gradio as gr

from resume_parser import parse_resume_text
from config import settings
from app.knowledge_base import list_jobs
from app.resume_reader import read_resume_text
from app.session_store import create_session, get_session, list_sessions

logger = logging.getLogger(__name__)

SUPPORTED_RESUME_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}


def _resolve_base_url(request: gr.Request | None = None) -> str:
    """候选人链接前缀，按优先级解析：
    1) settings.PUBLIC_BASE_URL（.env 显式设置）
    2) 当前请求的 host + scheme（支持 X-Forwarded-Proto/Host，反代场景）
    3) 空串，调用方决定怎么处理
    """
    explicit = settings.PUBLIC_BASE_URL.strip().rstrip("/")
    if explicit:
        return explicit
    if request is not None:
        proto = (
            request.headers.get("x-forwarded-proto")
            or request.headers.get("x-forwarded-protocol")
            or request.headers.get("scheme")
            or "http"
        ).split(",")[0].strip()
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or "localhost:7860"
        ).split(",")[0].strip()
        return f"{proto}://{host}"
    return ""

HR_UI_CSS = """
.gradio-container {
    width: 100% !important;
    max-width: none !important;
    min-height: 100vh !important;
}
.main.app {
    width: 100% !important;
    max-width: none !important;
    padding: 0 20px !important;
    box-sizing: border-box !important;
}
main.contain {
    width: 100% !important;
    max-width: none !important;
    min-height: calc(100vh - 24px) !important;
}
.page-title {padding: 8px 0 12px;}
#workbench-row {
    height: calc(100vh - 100px) !important;
    min-height: 480px !important;
    align-items: stretch !important;
}
.workbench-column {height: 100% !important; min-height: 0 !important;}
#left-panel, #right-panel {
    height: 100% !important;
    min-height: 0 !important;
    box-sizing: border-box !important;
}
.result-card {padding: 18px; border-radius: 12px; background: #f1f5ff; line-height: 2;}
.result-card a {color: #2457f5; word-break: break-all;}
.result-card pre {
    white-space: pre-wrap;
    font: inherit;
    line-height: 1.7;
    padding: 12px;
    border-radius: 8px;
    background: #f8fafc;
    margin-bottom: 0;
}
.result-empty {min-height: 220px; box-sizing: border-box;}
#sessions-table .table-wrap {
    max-height: 240px !important;
    overflow-y: auto !important;
}
#candidate-picker {position: relative !important;}
#candidate-picker input {padding-right: 52px !important;}
#candidate-picker .icon-wrap {right: 52px !important;}
#refresh-button {
    position: absolute !important;
    top: 45px !important;
    right: 12px !important;
    z-index: 10 !important;
    width: 38px !important;
    min-width: 38px !important;
    height: 38px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    border: 1px solid #cbd5e1 !important;
    background: white !important;
    color: #334155 !important;
    font-size: 24px !important;
}
"""


def _candidate_url(session_id: str, request: gr.Request | None = None) -> str:
    base_url = _resolve_base_url(request)
    if not base_url:
        # 没拿到 request 也没设 PUBLIC_BASE_URL（理论只发生在后台任务），
        # 不再硬编魔搭域，避免误把本机 session 指向远端域名
        base_url = "http://localhost:7860"
    return f"{base_url}/candidate/{session_id}"


def _session_choices(sessions: list[dict] | None = None) -> list[tuple[str, str]]:
    choices = []
    for session in (sessions if sessions is not None else list_sessions()):
        name = session.get("candidate_name") or "未知候选人"
        job = session.get("job_name") or "未填写岗位"
        status = "已完成" if session.get("status") == "finished" else "进行中"
        choices.append((f"{name} · {job} · {status}", session["session_id"]))
    return choices


def _sessions_table(sessions: list[dict] | None = None) -> list[list[str]]:
    return [
        [
            session.get("candidate_name") or "未知候选人",
            session.get("job_name") or "未填写岗位",
            "已完成" if session.get("status") == "finished" else "进行中",
            session.get("created_at") or "",
        ]
        for session in (sessions if sessions is not None else list_sessions())
    ]


async def create_candidate(
    candidate_name: str,
    job_name: str,
    resume_path: str | None,
    request: gr.Request,
) -> tuple[str, list[list[str]], gr.Dropdown]:
    name = candidate_name.strip()
    if not name:
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "请填写候选人姓名。</div>",
            gr.update(),
            gr.update(),
        )
    if not resume_path:
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "请先上传候选人简历。</div>",
            gr.update(),
            gr.update(),
        )

    path = Path(resume_path)
    if path.suffix.lower() not in SUPPORTED_RESUME_EXTENSIONS:
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "仅支持 PDF、DOC、DOCX 或 TXT 格式的简历。</div>",
            gr.update(),
            gr.update(),
        )
    if job_name not in list_jobs(ready_only=True):
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "请选择有效岗位。</div>",
            gr.update(),
            gr.update(),
        )

    resume_text = await read_resume_text(str(path))
    profile = parse_resume_text(resume_text)
    session_id = create_session(
        candidate_name=name,
        job_name=job_name,
        resume_json=profile.model_dump(),
        candidate_summary=resume_text[:1500],
    )

    link = _candidate_url(session_id, request)
    result_html = (
        '<div class="result-card">'
        f"<strong>候选人：</strong>{html.escape(name)}<br>"
        f"<strong>岗位：</strong>{html.escape(job_name)}<br>"
        f'<strong>候选人测评链接：</strong><a href="{html.escape(link, quote=True)}" '
        f'target="_blank" rel="noopener">{html.escape(link)}</a>'
        "</div>"
    )
    return result_html, _sessions_table(), gr.update(
        choices=_session_choices(),
        value=session_id,
    )


def show_candidate_link(session_id: str | None, request: gr.Request) -> str:
    if not session_id:
        return ""
    session = get_session(session_id)
    if not session:
        raise gr.Error("候选人会话不存在，请刷新候选人列表。")

    link = _candidate_url(session_id, request)
    return (
        f'<a href="{html.escape(link, quote=True)}" target="_blank" rel="noopener">'
        "打开候选人测评页面</a>"
    )


def show_assessment_result(session_id: str | None) -> str:
    if not session_id:
        return '<div class="result-card">请先选择候选人。</div>'
    session = get_session(session_id)
    if not session:
        return '<div class="result-card">候选人会话不存在，请刷新列表。</div>'

    if session.get("status") != "finished":
        result = (
            '<div class="result-card"><strong>测评进行中</strong><br>'
            "候选人完成测评后，可在这里查看结果。</div>"
        )
    else:
        result = (
            '<div class="result-card">'
            f"<strong>候选人：</strong>{html.escape(session.get('candidate_name') or '未知')}<br>"
            f"<strong>岗位：</strong>{html.escape(session.get('job_name') or '未填写')}<br>"
            f"<strong>最终结论：</strong>{html.escape(session.get('final_decision') or '未完成')}<br>"
            "<strong>总结理由：</strong>"
            f'<pre>{html.escape(session.get("summary") or "暂无总结")}</pre>'
            "</div>"
        )
    return result


def select_candidate(session_id: str | None, request: gr.Request) -> tuple[str, str]:
    link = show_candidate_link(session_id, request)
    return link, _empty_result()


def _empty_result() -> str:
    return '<div class="result-card result-empty"></div>'


def refresh_workbench(
    selected_session_id: str | None,
    selected_job_name: str | None,
    request: gr.Request,
) -> tuple[list[list[str]], dict, dict, str, str]:
    sessions = list_sessions()
    session_ids = {session["session_id"] for session in sessions}
    selected_session_id = (
        selected_session_id if selected_session_id in session_ids else None
    )
    job_choices = list_jobs(ready_only=True)
    return (
        _sessions_table(sessions),
        gr.update(choices=_session_choices(sessions), value=selected_session_id),
        gr.update(
            choices=job_choices,
            value=selected_job_name if selected_job_name in job_choices
            else job_choices[0] if job_choices else None,
        ),
        show_candidate_link(selected_session_id, request),
        _empty_result(),
    )


def build_hr_ui() -> gr.Blocks:
    try:
        job_choices = list_jobs(ready_only=True)
    except Exception as exc:
        logger.exception("Failed to load job list on startup: %s", exc)
        job_choices = []
    with gr.Blocks(
        title="HR 简历评测",
        fill_height=True,
        fill_width=True,
    ) as demo:
        gr.Markdown("# 简历评测 · HR工作台", elem_classes=["page-title"])
        with gr.Row(equal_height=True, elem_id="workbench-row"):
            with gr.Column(scale=5, elem_classes=["workbench-column"]):
                with gr.Group(elem_id="left-panel"):
                    gr.Markdown("### 创建候选人测评")
                    candidate_name = gr.Textbox(
                        label="候选人姓名",
                        placeholder="例如：张三",
                    )
                    job_name = gr.Dropdown(
                        choices=job_choices,
                        value=job_choices[0] if job_choices else None,
                        label="岗位名称",
                    )
                    resume_file = gr.File(
                        label="上传简历（PDF / DOC / DOCX / TXT）",
                        file_types=[".pdf", ".doc", ".docx", ".txt"],
                        type="filepath",
                    )
                    create_button = gr.Button("生成候选人链接", variant="primary")
                    created_link = gr.HTML(label="候选人测评链接")
            with gr.Column(scale=7, elem_classes=["workbench-column"]):
                with gr.Group(elem_id="right-panel"):
                    gr.Markdown("### 候选人筛选列表")
                    sessions_table = gr.Dataframe(
                        headers=["候选人", "岗位", "状态", "创建时间"],
                        datatype=["str", "str", "str", "str"],
                        value=_sessions_table(),
                        interactive=False,
                        wrap=True,
                        elem_id="sessions-table",
                        max_height=200,
                        label="",
                    )
                    with gr.Row(elem_id="candidate-picker"):
                        selected_session = gr.Dropdown(
                            choices=_session_choices(),
                            label="选择候选人查看测评",
                            scale=1,
                        )
                        refresh_button = gr.Button(
                            "↻",
                            variant="secondary",
                            elem_id="refresh-button",
                            scale=0,
                            min_width=42,
                        )
                    selected_link = gr.HTML()
                    result_button = gr.Button("查看测评结果")
                    result_panel = gr.HTML(value=_empty_result())

        create_button.click(
            create_candidate,
            inputs=[candidate_name, job_name, resume_file],
            outputs=[created_link, sessions_table, selected_session],
        )
        selected_session.change(
            select_candidate,
            inputs=selected_session,
            outputs=[selected_link, result_panel],
        )
        result_button.click(
            show_assessment_result,
            inputs=selected_session,
            outputs=result_panel,
        )
        refresh_button.click(
            refresh_workbench,
            inputs=[selected_session, job_name],
            outputs=[
                sessions_table,
                selected_session,
                job_name,
                selected_link,
                result_panel,
            ],
        )
        demo.load(
            refresh_workbench,
            inputs=[selected_session, job_name],
            outputs=[
                sessions_table,
                selected_session,
                job_name,
                selected_link,
                result_panel,
            ],
        )
    return demo
