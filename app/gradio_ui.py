"""HR 后台的两个页面。

拆成两个独立 Blocks 是因为职责不同：
- 【测评工作台】(/)：HR 高频页面，只管建测评链接、看候选人列表、查结果
- 【岗位管理】(/jobs)：低频页面，上传 JD 生成打分规则、增删改岗位

曾经把岗位管理做成工作台里的一个 Tab，结果是：新建岗位的表单塞在工作台左栏，
和「上传简历」只隔一层 Tab，两件事挤在一个屏幕上，谁都看不清。

同一个 Gradio 应用里没有「跳转页面」这种原语（Button 只能触发 Python 函数），
所以两页之间靠 <a href> 直连，各挂各的 Blocks。
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path

import gradio as gr

from resume_parser import parse_resume_text
from config import settings
from app.job_config_store import (
    JobConfigError,
    delete_job_config,
    get_job_config,
    get_job_config_object,
    list_job_configs,
    save_job_config,
)
from app.job_generator import JobConfigGenerator, JobGenerationError, soft_gap_warnings
from app.job_schema import normalize_job_config, split_issues, validate_job_config
from app.resume_reader import read_resume_text
from app.session_store import create_session, get_session, list_sessions

logger = logging.getLogger(__name__)

# 简历和 JD 走同一个解析器（app/resume_reader.py），支持同样的格式。
SUPPORTED_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}
SUPPORTED_FILE_TYPES = sorted(SUPPORTED_FILE_EXTENSIONS)

# job_config_table.source_type → 列表里给人看的中文
_SOURCE_LABELS = {"auto_llm": "AI 生成", "manual": "手动"}

# 岗位列表的列序。操作列靠下标区分，所以这个顺序是契约，改了要同步改
# _job_table() 和 select_job_action()。
COL_NAME, COL_SOURCE, COL_CREATED, COL_EDIT, COL_DELETE = range(5)

WORKBENCH_PATH = "/hr"
# 尾斜杠不能省。Starlette 的 Mount 正则是 ^/hr/jobs/(?P<path>.*)$，
# 不带斜杠的 /hr/jobs 会被后注册的 /hr mount 完整匹配走（当成工作台的子路径），
# 结果是 404 —— 看起来像挂载顺序错了，其实是斜杠。
JOB_MANAGER_PATH = "/hr/jobs/"


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
            or "ywen666-hr-ai-assessment.ms.show"
        ).split(",")[0].strip()
        return f"{proto}://{host}"
    return ""


HR_UI_CSS = """
html, body, .gradio-container, .main.app, main.contain {
    background:
        linear-gradient(rgba(255,255,255,0.86), rgba(255,255,255,0.86)),
        url('/static/hr.jpg') center/cover no-repeat fixed !important;
    background-attachment: fixed !important;
}
/* Gradio 默认给根元素和容器加了浅灰底色，把背景图遮住了。强制设透明。*/
.gradio-app, .gradio-container, .app {
    background: transparent !important;
}
/* panel / block / wrap 用半透明白底，既透出背景图又保证文字可读*/
.gradio-group, .block, .wrap, .panel, .form, .styler, .gr-group, .container {
    background: rgba(255, 255, 255, 0.78) !important;
}
/* Gradio 6 在 Group 里包了一层 .styler 和 .gr-group，默认浅灰 rgb(226,232,240) 也会盖住背景。*/
.svelte-1p9262q {
    background: rgba(255, 255, 255, 0.78) !important;
}
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
.page-title {padding: 8px 0 4px;}
.page-nav {display: flex; gap: 10px; align-items: center; margin: 0 0 14px;}
.nav-link {
    display: inline-block;
    padding: 8px 18px;
    border-radius: 8px;
    background: #2457f5;
    color: #ffffff !important;
    text-decoration: none;
    font-weight: 600;
    border: 1px solid #1d4ed8;
}
.nav-link:hover {background: #1d4ed8;}
.nav-link.secondary {background: #ffffff; color: #334155 !important; border-color: #cbd5e1;}
.nav-link.secondary:hover {background: #f1f5f9;}

#workbench-row {
    /* 扣掉的 170px = 页头标题 + 导航条。右栏底部是测评结果，
       留窄了会被挤出视口，HR 每次都得滚页面看结论。 */
    height: calc(100vh - 170px) !important;
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

/* ---- 弹窗 ---- */
/* Gradio 6 没有 Modal 组件，遮罩层只能用普通 Column + CSS 造。
   面板里的组件默认 min-width 很大，不压掉的话 flex 子项会被撑破。 */
.modal-overlay {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    right: 0 !important;
    bottom: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 900 !important;
    background: rgba(15, 23, 42, 0.45) !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: flex-start !important;
    padding: 56px 16px 32px !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    gap: 0 !important;
}
.modal-panel {
    background: #ffffff !important;
    border-radius: 16px !important;
    box-shadow: 0 24px 60px rgba(15, 23, 42, 0.35) !important;
    padding: 24px !important;
    width: 100% !important;
    max-width: 900px !important;
    min-width: 0 !important;
    flex: 0 0 auto !important;
    margin: 0 !important;
    gap: 14px !important;
}
.modal-panel .result-card {line-height: 1.8;}
.modal-title {font-size: 1.15em; font-weight: 700; margin: 0 0 4px;}
.modal-hint {color: #64748b; font-size: 13px; line-height: 1.7; margin: 0;}
.jd-preview {
    max-height: 260px;
    overflow-y: auto;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 12px;
    white-space: pre-wrap;
    font-size: 13px;
    line-height: 1.7;
    color: #334155;
}
.modal-danger {
    background: #fef2f2;
    color: #dc2626;
    border: 1px solid #fecaca;
    border-radius: 12px;
    padding: 14px 16px;
    line-height: 1.8;
}
"""


def _nav_link(href: str, label: str, *, secondary: bool = False) -> str:
    cls = "nav-link secondary" if secondary else "nav-link"
    return (
        f'<div class="page-nav"><a class="{cls}" href="{html.escape(href, quote=True)}">'
        f"{html.escape(label)}</a></div>"
    )


def _candidate_url(session_id: str, request: gr.Request | None = None) -> str:
    base_url = _resolve_base_url(request)
    if not base_url:
        # 没拿到 request 也没设 PUBLIC_BASE_URL（理论只发生在后台任务），
        # 兜底用魔搭公网服务地址
        base_url = "https://ywen666-hr-ai-assessment.ms.show"
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
    job_key: str,
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
    if path.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "仅支持 PDF、DOC、DOCX 或 TXT 格式的简历。</div>",
            gr.update(),
            gr.update(),
        )

    job_config = get_job_config_object(job_key)
    if job_config is None:
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "请选择有效岗位。</div>",
            gr.update(),
            gr.update(),
        )
    job_name = job_config.get("job_name", job_key)

    resume_text = await read_resume_text(str(path))
    # 经验标签按岗位封闭词表识别，必须把 job_config 传进去
    profile = parse_resume_text(resume_text, job_config)
    session_id = create_session(
        candidate_name=name,
        job_name=job_name,
        resume_json=profile,
        candidate_summary=resume_text[:1500],
        job_key=job_key,
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


def _job_choices() -> list[tuple[str, str]]:
    """岗位下拉选项：(岗位名, job_key)。

    下拉里显示 job_name，底层传 job_key —— 显示用中文标识，
    存储和检索一律用 key。两者混用是这套系统早期最大的坑。
    """
    return [
        (config.get("job_name", config["job_key"]), config["job_key"])
        for config in list_job_configs()
        if config.get("job_key")
    ]


def _empty_result() -> str:
    return '<div class="result-card result-empty"></div>'


def refresh_workbench(
    selected_session_id: str | None,
    selected_job_key: str | None,
    request: gr.Request,
) -> tuple[list[list[str]], dict, dict, str, str]:
    """刷新工作台：候选人列表、候选人下拉、岗位下拉、链接、结果区。"""
    sessions = list_sessions()
    session_ids = {session["session_id"] for session in sessions}
    selected_session_id = (
        selected_session_id if selected_session_id in session_ids else None
    )
    choices = _job_choices()
    keys = [key for _, key in choices]
    return (
        _sessions_table(sessions),
        gr.update(choices=_session_choices(sessions), value=selected_session_id),
        gr.update(
            choices=choices,
            value=selected_job_key if selected_job_key in keys
            else keys[0] if keys else None,
        ),
        show_candidate_link(selected_session_id, request),
        _empty_result(),
    )


# ===========================================================================
# 测评工作台页面
# ===========================================================================


def build_workbench_ui() -> gr.Blocks:
    """HR 高频页面：建测评链接、看候选人、查结果。岗位管理不在这里。"""
    try:
        job_choices = _job_choices()
    except Exception as exc:
        logger.exception("Failed to load job list on startup: %s", exc)
        job_choices = []

    with gr.Blocks(
        title="HR 简历评测",
        fill_height=True,
        fill_width=True,
    ) as demo:
        gr.Markdown("# 简历评测 · HR工作台", elem_classes=["page-title"])
        # 唯一的跨页入口。Gradio 没有页面跳转 API，Button 只能触发 Python 函数，
        # 所以这里直接塞一个 <a>，点了走浏览器导航。
        gr.HTML(_nav_link(JOB_MANAGER_PATH, "岗位管理"))

        with gr.Row(equal_height=True, elem_id="workbench-row"):
            with gr.Column(scale=5, elem_classes=["workbench-column"]):
                with gr.Group(elem_id="left-panel"):
                    gr.Markdown("### 创建候选人测评")
                    candidate_name = gr.Textbox(
                        label="候选人姓名",
                        placeholder="例如：张三",
                    )
                    job_picker = gr.Dropdown(
                        choices=job_choices,
                        value=job_choices[0][1] if job_choices else None,
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
            inputs=[candidate_name, job_picker, resume_file],
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
            inputs=[selected_session, job_picker],
            outputs=[sessions_table, selected_session, job_picker, selected_link, result_panel],
        )
        demo.load(
            refresh_workbench,
            inputs=[selected_session, job_picker],
            outputs=[sessions_table, selected_session, job_picker, selected_link, result_panel],
        )
    return demo


# ===========================================================================
# 岗位管理页面
# ===========================================================================
# 全程两段式：先「生成规则」拿到预览（不落库），HR 确认后再「保存」入库。
#
# 校验口径只有一档（strict，与入库同口径）。生成时就跑一遍并把结果摆在
# 弹窗里，保存时再跑一遍做拦截 —— 两处用的是同一个 validate_job_config，
# 所以不会出现「生成说通过、保存被拒」这种自相矛盾的情况。
# 软性坑（词表为空但规则用到了它）只提示不拦：那种坑跑起来不报错，
# 只会静默不命中，提示一句足够了。


def _job_table(
    configs: list[dict] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """岗位列表。返回 (表格数据, 行号→job_key)。

    两样东西必须一起返回、来自同一次 list_job_configs()：
    行号到 job_key 的映射一旦和表格行错位，点【编辑】改的就是别的岗位。
    分成两个函数各查一次，中间只要有人改了数据就会串行。

    表格里**不放 job_key** —— 那是后端内部标识，HR 用不上，
    看到一列看不懂的英文反而要多问一句。
    """
    rows: list[list[str]] = []
    keys: list[str] = []
    for config in (configs if configs is not None else list_job_configs()):
        key = config.get("job_key")
        if not key:
            continue
        keys.append(key)
        rows.append([
            config.get("job_name") or key,
            _SOURCE_LABELS.get(config.get("source_type"), config.get("source_type") or ""),
            config.get("create_time") or "",
            "编辑",
            "删除",
        ])
    return rows, keys


def refresh_job_table() -> tuple[list[list[str]], list[str]]:
    rows, keys = _job_table()
    return rows, keys


def _notice(message: str, *, tone: str = "error") -> str:
    """单行状态提示。需要逐条列出时用 _render_validation —— 这个函数会转义
    HTML，多行文字塞进来会被折叠成一行。"""
    palette = {
        "error": "#fef2f2;color:#dc2626",
        "ok": "#eef8f1;color:#0d7a4d",
    }
    return (
        f'<div class="result-card" style="background:{palette[tone]}">'
        f"{html.escape(message)}</div>"
    )


def _render_validation(errors: list[str], warnings: list[str]) -> str:
    """校验结果卡片。有 errors 就谈不上警告，errors 优先。"""
    if errors:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in errors)
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            f"<strong>校验不通过，共 {len(errors)} 项，保存将被拦截：</strong>"
            f"<ul>{items}</ul>"
            "<div>如需修正，请展开下方【高级：直接改配置 JSON】修改后再保存。</div>"
            "</div>"
        )
    if warnings:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in warnings)
        return (
            '<div class="result-card" style="background:#fff7e6;color:#9a6700">'
            f"<strong>校验通过，另有 {len(warnings)} 点提示：</strong><ul>{items}</ul></div>"
        )
    return (
        '<div class="result-card" style="background:#eef8f1;color:#0d7a4d">'
        "<strong>校验通过，可以保存。</strong></div>"
    )


def _parse_config_text(config_text: str) -> tuple[dict | None, str]:
    """解析编辑器里的 JSON。失败返回 (None, 给人看的错误说明)。"""
    text = (config_text or "").strip()
    if not text:
        return None, "配置为空，请先上传 JD 生成配置，或直接粘贴一份 JSON。"
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return None, f"配置不是合法 JSON：{exc}"
    if not isinstance(parsed, dict):
        return None, "配置的根节点必须是 JSON 对象。"
    return parsed, ""


def _validate_config_text(config_text: str) -> tuple[str, dict | None]:
    """跑一遍与入库完全一致的校验。返回 (状态卡片, 解析后的配置)。

    解析失败时配置为 None，状态卡片是错误提示 —— 调用方直接用它当
    「保存被拦截」的返回值，不用再自己拼一遍错误文案。
    """
    parsed, error = _parse_config_text(config_text)
    if parsed is None:
        return _notice(error), None
    normalized = normalize_job_config(parsed)
    errors, warnings = split_issues(validate_job_config(normalized))
    return _render_validation(errors, warnings + soft_gap_warnings(normalized)), normalized


async def _generate_from_jd(
    jd_path: str | None,
    *,
    job_key: str | None = None,
) -> tuple[str, str, str, dict]:
    """上传的 JD → 岗位配置预览。**不落库。**

    返回 (配置 JSON, 状态卡片, JD 原文, 清空文件框的 gr.update())。
    JD 原文一并回传是为了保存时存进 raw_jd 列，事后能追回这份配置
    来自哪份 JD。末尾清空文件框，省得 HR 连传第二份时还得手动取消。

    job_key 传了就锁死：生成器会自己按 JD 猜一个 key，编辑老岗位时
    猜出来的新 key 会把配置挂到另一个岗位名下，等于凭空多一个岗位、
    老岗位还是旧规则 —— 这正是需求里「job_key 不变」要防的事。
    """
    if not jd_path:
        raise gr.Error("请先上传岗位 JD 文件。")

    path = Path(jd_path)
    if path.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
        raise gr.Error("JD 文件仅支持 PDF、DOC、DOCX 或 TXT 格式。")

    try:
        jd_text = await read_resume_text(str(path))
    except Exception as exc:
        raise gr.Error(f"JD 文件读取失败：{exc}") from exc

    if not jd_text.strip():
        raise gr.Error("JD 文件里没有读到任何文字。")

    try:
        result = await JobConfigGenerator().generate(jd_text, job_key=job_key)
    except JobGenerationError as exc:
        raise gr.Error(f"岗位配置生成失败：{exc}") from exc

    config = result["config"]
    if job_key:
        # 兜底：generate 收下了 job_key 参数，但模型仍可能给回别的 key。
        config["job_key"] = job_key

    # 生成时就校验，结果直接摆在弹窗里。拦不拦是保存那一步的事，
    # 这里只是让 HR 立刻看见「这份配置存不进去」。
    status, _ = _validate_config_text(json.dumps(config, ensure_ascii=False))

    logger.info("JD 生成岗位配置：%s", config.get("job_key"))
    return (
        json.dumps(config, ensure_ascii=False, indent=2),
        status,
        jd_text,
        gr.update(value=None),
    )


async def generate_job_config(jd_path: str | None) -> tuple[str, str, str, dict]:
    """新增岗位：上传 JD → 生成配置预览。不落库。"""
    return await _generate_from_jd(jd_path)


async def regenerate_job_config(
    jd_path: str | None,
    job_key: str,
) -> tuple[str, str, str, dict]:
    """编辑岗位：上传新版 JD → 重新生成。job_key 锁死为原值。"""
    if not job_key:
        raise gr.Error("没有正在编辑的岗位，请重新从列表点【编辑】。")
    return await _generate_from_jd(jd_path, job_key=job_key)


def _persist_config(config_text: str, raw_jd: str | None) -> tuple[str, str]:
    """把编辑器里的配置写进库。返回 (状态卡片, 入库的 job_key)，失败时 key 为空串。

    校验不通过则完全不落库，且**不动编辑器** —— JSON 原样留在那儿，
    HR 照着弹窗里的提示改完直接重存。
    """
    parsed, error = _parse_config_text(config_text)
    if parsed is None:
        return _notice(error), ""

    job_key = parsed.get("job_key")
    if not job_key:
        return (
            _render_validation(["配置缺少 job_key，无法入库。"], []),
            "",
        )
    # 先查一次只是为了区分提示语是「已新增」还是「已更新」
    existed = get_job_config(job_key) is not None

    try:
        save_job_config(
            parsed,
            source_type="auto_llm",
            raw_jd=(raw_jd or "").strip() or None,
        )
    except JobConfigError as exc:
        # 用列表渲染而不是把 errors 拼成一段文字：_notice 会转义 HTML，
        # 拼出来的 \n 在页面上会被折叠成一行，几十条错误糊成一坨。
        return (
            _render_validation([f"保存失败，请修正后重新保存：{e}" for e in exc.errors], []),
            "",
        )

    verb = "已更新岗位" if existed else "已新增岗位"
    return _notice(f"{verb}：{job_key}", tone="ok"), job_key


def save_new_job(
    config_text: str,
    raw_jd: str | None,
) -> tuple[str, dict, list[list[str]], list[str]]:
    """新增岗位保存。返回 (状态, 弹窗可见性, 表格, 行→key)。

    存成功才关弹窗；失败保持打开，让 HR 接着改。
    """
    status, saved_key = _persist_config(config_text, raw_jd)
    rows, keys = _job_table()
    return status, gr.update(visible=not saved_key), rows, keys


def save_edited_job(
    config_text: str,
    raw_jd: str | None,
    job_key: str,
) -> tuple[str, dict, list[list[str]], list[str]]:
    """编辑岗位保存（覆盖）。

    job_key 在这里再钉一次：JSON 编辑器是敞开的，HR 改分值时手滑把
    job_key 改掉，就会在库里多出一个新岗位、老岗位纹丝不动。
    编辑的语义就是「覆盖这个岗位」，key 变了就不是编辑了。
    """
    parsed, error = _parse_config_text(config_text)
    if parsed is None:
        rows, keys = _job_table()
        return _notice(error), gr.update(visible=True), rows, keys

    pinned = dict(parsed)
    pinned["job_key"] = job_key
    status, saved_key = _persist_config(
        json.dumps(pinned, ensure_ascii=False), raw_jd
    )
    rows, keys = _job_table()
    return status, gr.update(visible=not saved_key), rows, keys


def delete_job_from_ui(
    job_key: str | None,
) -> tuple[str, dict, list[list[str]], list[str]]:
    """软删岗位（已在二次确认弹窗里点过确认）。"""
    if not job_key:
        raise gr.Error("没有待删除的岗位，请重新从列表点【删除】。")
    if not delete_job_config(job_key):
        raise gr.Error(f"岗位不存在或已删除：{job_key}")

    rows, keys = _job_table()
    return (
        _notice(f"已删除岗位：{job_key}（历史测评记录仍保留关联）", tone="ok"),
        gr.update(visible=False),
        rows,
        keys,
    )


# -- 弹窗开关 ---------------------------------------------------------------


def open_create_modal() -> tuple[dict, dict, dict, dict, str]:
    """打开【新增岗位】。每次都清空上一次残留的 JD 和 JSON。"""
    return (
        gr.update(visible=True),
        gr.update(value=None),
        gr.update(value=""),
        gr.update(value=""),
        "",
    )


def _close_modal() -> dict:
    return gr.update(visible=False)


def select_job_action(
    ev: gr.SelectData,
    keys: list[str],
) -> tuple[str, str, dict, str, str, str, dict, dict, dict]:
    """点岗位列表的【编辑】/【删除】列 → 打开对应弹窗。

    返回 (岗位 key, 原始 JD, 编辑弹窗可见性, 编辑区信息, 编辑区 JSON,
    编辑区状态, 编辑区文件框, 删除弹窗可见性, 删除区信息)。

    只认列号，不看单元格文字：文案以后改成「编辑 ✎」之类，
    按文字匹配就全废了。
    """
    hidden = gr.update(visible=False)
    index = list(getattr(ev, "index", None) or [])
    if len(index) < 2:
        return "", "", hidden, "", "", "", hidden, hidden, ""

    row, col = int(index[0]), int(index[1])
    if not (0 <= row < len(keys or [])):
        return "", "", hidden, "", "", "", hidden, hidden, ""

    job_key = keys[row]
    record = get_job_config(job_key, include_deleted=True) or {}
    config = get_job_config_object(job_key) or {}
    job_name = config.get("job_name") or job_key

    if col == COL_EDIT:
        raw_jd = record.get("raw_jd") or ""
        info = _edit_info_html(job_name, raw_jd)
        return (
            job_key,
            raw_jd,
            gr.update(visible=True),
            info,
            json.dumps(config, ensure_ascii=False, indent=2) if config else "",
            "",
            gr.update(value=None),
            hidden,
            "",
        )

    if col == COL_DELETE:
        return (
            job_key,
            "",
            hidden,
            "",
            "",
            "",
            hidden,
            gr.update(visible=True),
            _delete_info_html(job_name),
        )

    # 点到姓名/来源/时间列：不是操作，不弹窗
    return "", "", hidden, "", "", "", hidden, hidden, ""


def _edit_info_html(job_name: str, raw_jd: str) -> str:
    """编辑弹窗头部：岗位名 + 原有 JD 原文。

    早期 seed 进来的岗位 raw_jd 是空的（seed_from_files 不传这个字段），
    这时要明说「没有原始 JD」，不能让 HR 以为页面坏了。
    """
    if raw_jd.strip():
        jd_block = (
            '<div class="modal-hint">该岗位的原始 JD：</div>'
            f'<div class="jd-preview">{html.escape(raw_jd)}</div>'
        )
    else:
        jd_block = (
            '<div class="modal-hint">该岗位没有留存原始 JD（导入的历史岗位都没有）。'
            "请直接上传新版 JD，或展开下方【高级】直接改 JSON。</div>"
        )
    return (
        f'<div class="modal-title">编辑岗位：{html.escape(job_name)}</div>'
        f"{jd_block}"
        '<div class="modal-hint">保存后立即生效，<strong>历史测评报告的分数不会变</strong>'
        "（报告用的是测评当时的配置快照）；只有之后发起的新测评会用新规则。</div>"
    )


def _delete_info_html(job_name: str) -> str:
    return (
        f'<div class="modal-title">确认删除岗位：{html.escape(job_name)}</div>'
        '<div class="modal-danger">'
        "删除后该岗位不再出现在工作台的岗位下拉里，历史测评记录仍然保留。<br>"
        "<strong>这不是修改 JD 的方式</strong> —— 要改 JD 请用【编辑】。"
        "</div>"
    )


def build_job_manager_ui() -> gr.Blocks:
    """岗位管理子页面：上传 JD 建规则，增删改岗位。"""
    rows, keys = _job_table()

    with gr.Blocks(
        title="岗位管理",
        fill_height=True,
        fill_width=True,
    ) as demo:
        gr.Markdown("# 岗位管理", elem_classes=["page-title"])
        gr.HTML(_nav_link(WORKBENCH_PATH, "← 返回测评工作台", secondary=True))

        row_keys = gr.State(keys)
        active_job_key = gr.State("")
        active_raw_jd = gr.State("")

        with gr.Row():
            add_button = gr.Button("+ 新增岗位", variant="primary", scale=0)
            table_note = gr.Markdown(
                "岗位由 JD 自动生成规则。新增/编辑都走「上传 JD → 生成规则 → 保存」。",
                scale=1,
            )

        job_table = gr.Dataframe(
            headers=["岗位名称", "来源", "创建时间", "编辑", "删除"],
            datatype=["str"] * 5,
            value=rows,
            interactive=False,
            wrap=True,
            max_height=320,
            label="岗位列表（点【编辑】或【删除】所在的那一格）",
            elem_id="job-table",
        )

        # ---- 新增岗位弹窗 ----
        new_raw_jd = gr.State("")
        with gr.Column(
            visible=False, elem_id="create-modal", elem_classes=["modal-overlay"]
        ) as create_modal:
            with gr.Column(elem_classes=["modal-panel"]):
                gr.Markdown('<div class="modal-title">新增岗位</div>', elem_classes=["modal-title-wrap"])
                gr.Markdown(
                    '<div class="modal-hint">上传岗位 JD，系统会自动生成打分规则并做 schema 校验。'
                    "校验不通过会拦下不允许保存。</div>"
                )
                new_jd_file = gr.File(
                    label="上传岗位 JD（PDF / DOC / DOCX / TXT）",
                    file_types=SUPPORTED_FILE_TYPES,
                    type="filepath",
                )
                new_generate = gr.Button("根据 JD 生成规则", variant="primary")
                new_status = gr.HTML()
                with gr.Accordion("高级：直接改配置 JSON", open=False):
                    gr.Markdown(
                        "给开发/测试用：生成完想微调分值或阈值，直接改这里。"
                        "HR 日常不用展开。"
                    )
                    new_editor = gr.Code(
                        label="岗位配置 JSON",
                        language="json",
                        value="",
                        lines=20,
                        # 显式打开：Gradio 6 的 interactive 默认是 None，
                        # 实际能不能编辑要看前端回退。这个框存在的意义就是让人改，
                        # 不能赌默认值。
                        interactive=True,
                    )
                with gr.Row():
                    new_cancel = gr.Button("取消", scale=1)
                    new_save = gr.Button("保存入库", variant="primary", scale=1)

        # ---- 编辑岗位弹窗 ----
        with gr.Column(
            visible=False, elem_id="edit-modal", elem_classes=["modal-overlay"]
        ) as edit_modal:
            with gr.Column(elem_classes=["modal-panel"]):
                edit_info = gr.HTML()
                edit_jd_file = gr.File(
                    label="上传新版 JD（不传则保留现有规则）",
                    file_types=SUPPORTED_FILE_TYPES,
                    type="filepath",
                )
                edit_generate = gr.Button("根据新版 JD 重新生成规则")
                edit_status = gr.HTML()
                with gr.Accordion("高级：直接改配置 JSON", open=False):
                    gr.Markdown(
                        "改分值、阈值、勾选项都直接改这里，不用重新调模型。"
                    )
                    edit_editor = gr.Code(
                        label="岗位配置 JSON",
                        language="json",
                        value="",
                        lines=20,
                        interactive=True,
                    )
                with gr.Row():
                    edit_cancel = gr.Button("取消", scale=1)
                    edit_save = gr.Button("保存并覆盖", variant="primary", scale=1)

        # ---- 删除二次确认弹窗 ----
        with gr.Column(
            visible=False, elem_id="delete-modal", elem_classes=["modal-overlay"]
        ) as delete_modal:
            with gr.Column(elem_classes=["modal-panel"]):
                delete_info = gr.HTML()
                with gr.Row():
                    delete_cancel = gr.Button("取消", scale=1)
                    delete_confirm = gr.Button("确认删除", variant="stop", scale=1)

        # ---- 事件接线 ----
        add_button.click(
            open_create_modal,
            outputs=[create_modal, new_jd_file, new_editor, new_status, new_raw_jd],
        )
        new_cancel.click(_close_modal, outputs=create_modal)
        new_generate.click(
            generate_job_config,
            inputs=[new_jd_file],
            outputs=[new_editor, new_status, new_raw_jd, new_jd_file],
        )
        new_save.click(
            save_new_job,
            inputs=[new_editor, new_raw_jd],
            outputs=[new_status, create_modal, job_table, row_keys],
        )

        edit_cancel.click(_close_modal, outputs=edit_modal)
        edit_generate.click(
            regenerate_job_config,
            inputs=[edit_jd_file, active_job_key],
            outputs=[edit_editor, edit_status, active_raw_jd, edit_jd_file],
        )
        edit_save.click(
            save_edited_job,
            inputs=[edit_editor, active_raw_jd, active_job_key],
            outputs=[edit_status, edit_modal, job_table, row_keys],
        )

        delete_cancel.click(_close_modal, outputs=delete_modal)
        delete_confirm.click(
            delete_job_from_ui,
            inputs=[active_job_key],
            outputs=[delete_info, delete_modal, job_table, row_keys],
        )

        job_table.select(
            select_job_action,
            inputs=[row_keys],
            outputs=[
                active_job_key, active_raw_jd, edit_modal, edit_info,
                edit_editor, edit_status, edit_jd_file, delete_modal, delete_info,
            ],
        )

        demo.load(refresh_job_table, outputs=[job_table, row_keys])

    return demo