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
import re
from pathlib import Path

import gradio as gr

from resume_parser import parse_resume_text
from config import settings
from app.job_config_store import (
    JobConfigError,
    JobNameTakenError,
    delete_job_config,
    get_job_config,
    get_job_config_object,
    list_job_configs,
    save_job_config,
)
from app.job_generator import JobConfigGenerator, JobGenerationError, soft_gap_warnings
from app.job_schema import normalize_job_config, split_issues, validate_job_config
from app.rag_bypass import delete_rag_document, get_rag_document_content, save_rag_document
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
COL_NAME, COL_JD, COL_SOURCE, COL_CREATED, COL_EDIT, COL_DELETE, COL_KB = range(7)

WORKBENCH_PATH = "/hr"
# 尾斜杠不能省。Starlette 的 Mount 正则是 ^/hr/jobs/(?P<path>.*)$，
# 不带斜杠的 /hr/jobs 会被后注册的 /hr mount 完整匹配走（当成工作台的子路径），
# 结果是 404 —— 看起来像挂载顺序错了，其实是斜杠。
JOB_MANAGER_PATH = "/hr/jobs/"

# 品牌静态资源。四个文件都放在 <data_root>/static/dist/ 下，文件名是中文，
# 这里写死百分号编码后的 URL —— 浏览器和 StaticFiles 都能正常解析，
# 也不用在服务端再转义一遍。
#   高途大字.png   → 右上角的品牌角标
#   途小跃.jpg     → 工作台英雄条的背景（见 HR_WORKBENCH_CSS）
BRAND_MARK_URL = "/static/dist/%E9%AB%98%E9%80%94%E5%A4%A7%E5%AD%97.png"
MASCOT_URL = "/static/dist/%E9%80%94%E5%B0%8F%E8%B7%83.jpg"


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
/* =============================================================================
   HR 工作台 / 岗位管理 公共样式
   -----------------------------------------------------------------------------
   Gradio 6 会把注入进来的 CSS 复制成两份：一份原样，一份加上
   ".gradio-container.gradio-container-<版本> .contain " 前缀。
   于是每条规则同时以 (0,x,0) 和 (0,4,0) 两种权重出现，原样那份排在前面 ——
   也就是说 **在同一个样式表内部，后写的赢**。据此：
     1. 要压过 Gradio 主题的规则 → 加 !important（主题规则不带 important）；
     2. 要压过自己前面写的规则 → 写后面，或换更具体的选择器；
     3. html / body / gradio-app 在 .contain 外面，带前缀那份永远匹配不上，
        只有原样那份生效 —— 正好用来铺整页底色。
   ============================================================================= */

/* ------------------------------ 1. 设计变量 ------------------------------ */
:root {
    --hr-blue: #2457f5;
    --hr-blue-dark: #1d4ed8;
    --hr-blue-soft: #eef3ff;
    --hr-ink: #16213a;
    --hr-ink-2: #4d5b73;
    --hr-ink-3: #8695ad;
    --hr-line: #e4eaf5;
    --hr-line-2: #d3dcee;
    --hr-card: #ffffff;
    --hr-canvas: #f1f5ff;
    --hr-navy: #0b1440;
    --hr-navy-2: #16297a;
    --hr-r-card: 16px;
    --hr-r-ctl: 10px;
    --hr-r-lg: 20px;
    --hr-shadow-sm: 0 1px 2px rgba(15, 23, 42, .06), 0 2px 8px rgba(15, 23, 42, .05);
    --hr-shadow-md: 0 10px 26px rgba(15, 23, 42, .08), 0 2px 6px rgba(15, 23, 42, .04);
    --hr-shadow-lg: 0 24px 60px rgba(11, 20, 64, .22);
    --hr-font: "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
    /* Gradio 的布局块默认是白底，铺在彩色画布上会割裂 —— 统一透明。 */
    --block-background-fill: transparent !important;
    --body-background-fill: transparent !important;
}

/* --------------------- 2. 铺底色：打通 Gradio 的几层底色 --------------------- */
html {
    background-color: var(--hr-canvas) !important;
    background-image:
        radial-gradient(900px 520px at 10% -8%, rgba(36, 87, 245, .13), rgba(36, 87, 245, 0) 62%),
        radial-gradient(780px 460px at 94% 2%, rgba(124, 92, 255, .10), rgba(124, 92, 255, 0) 60%) !important;
    background-repeat: no-repeat !important;
    background-attachment: fixed !important;
}
body { background: transparent !important; }
gradio-app { background: transparent !important; }
html body .gradio-container,
html body .gradio-container .main.app,
html body .gradio-container .main.app > .wrap,
html body .gradio-container main.contain,
html body .gradio-container footer {
    background: transparent !important;
}
body, .gradio-container, input, textarea, select, button {
    font-family: var(--hr-font) !important;
}

/* ------------------------------- 3. 版心 ------------------------------- */
.gradio-container {
    width: 100% !important;
    max-width: none !important;
    min-height: 100vh !important;
}
.main.app {
    width: 100% !important;
    max-width: none !important;
    padding: 0 !important;
    box-sizing: border-box !important;
}
main.contain {
    width: 100% !important;
    max-width: 1400px !important;
    margin: 0 auto !important;
    padding: 20px 24px 28px !important;
    box-sizing: border-box !important;
    min-height: 100vh !important;
}
html body .gradio-container .prose { max-width: none !important; }

/* ---------------------------- 4. 页头英雄条 ---------------------------- */
.page-hero {
    position: relative;
    margin: 0 0 18px;
    padding: 20px 26px 18px;
    min-height: 116px;
    border-radius: var(--hr-r-lg);
    background: linear-gradient(135deg, var(--hr-navy-2) 0%, var(--hr-navy) 60%, #050a20 100%);
    box-shadow: var(--hr-shadow-lg);
    overflow: hidden;
}
.page-hero-title {
    margin: 0 !important;
    padding: 0 !important;
    font-size: 25px !important;
    font-weight: 800 !important;
    line-height: 1.35 !important;
    color: #ffffff !important;
    letter-spacing: .4px;
}
.page-hero-sub {
    margin: 6px 0 0 !important;
    padding: 0 !important;
    font-size: 13px !important;
    line-height: 1.6 !important;
    color: rgba(210, 224, 255, .78) !important;
}
.page-nav {
    position: relative;
    display: flex;
    gap: 10px;
    align-items: center;
    margin: 14px 0 0;
    padding-right: 60px;
}
/* 高途大字：钉在导航行的右上角 */
.brand-mark {
    position: absolute;
    top: -6px;
    right: 0;
    width: 46px;
    height: 46px;
    border-radius: 13px;
    overflow: hidden;
    background: #ffffff;
    border: 1px solid rgba(255, 255, 255, .22);
    box-shadow: 0 8px 18px rgba(3, 8, 30, .45);
}
.brand-mark img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
}

/* ------------------------------ 5. 导航按钮 ------------------------------ */
.nav-link {
    display: inline-block;
    padding: 8px 18px;
    border-radius: var(--hr-r-ctl);
    background: var(--hr-blue) !important;
    color: #ffffff !important;
    text-decoration: none;
    font-weight: 600;
    font-size: 14px;
    line-height: 1.4;
    border: 1px solid rgba(255, 255, 255, .18) !important;
    box-shadow: 0 6px 16px rgba(36, 87, 245, .38) !important;
    transition: background .15s ease, transform .15s ease, box-shadow .15s ease;
}
.nav-link:hover {
    background: var(--hr-blue-dark) !important;
    transform: translateY(-1px);
}
.nav-link.secondary {
    background: rgba(255, 255, 255, .13) !important;
    color: #e6edff !important;
    border-color: rgba(255, 255, 255, .30) !important;
    box-shadow: none !important;
}
.nav-link.secondary:hover { background: rgba(255, 255, 255, .22) !important; }
/* 登出是个 <form><button>，得把 Gradio 给 button 的默认样式压掉，看起来才像导航按钮 */
.nav-logout { margin: 0; }
button.nav-link {
    cursor: pointer;
    font-family: inherit;
    font-size: 14px;
    line-height: 1.4;
    min-width: 0;
    min-height: 0;
    padding: 8px 18px;
    border-radius: var(--hr-r-ctl);
}

/* ---------------------------- 6. 工作台面板 ---------------------------- */
#workbench-row {
    /* 扣掉的 200px = 页头英雄条 + 上下留白。右栏底部是测评结果，
       留窄了会被挤出视口，HR 每次都得滚页面看结论。 */
    height: calc(100vh - 200px) !important;
    min-height: 460px !important;
    align-items: stretch !important;
}
.workbench-column { height: 100% !important; min-height: 0 !important; }
#left-panel, #right-panel {
    height: 100% !important;
    min-height: 0 !important;
    box-sizing: border-box !important;
    background: var(--hr-card) !important;
    border: 1px solid var(--hr-line) !important;
    border-radius: var(--hr-r-card) !important;
    box-shadow: var(--hr-shadow-md) !important;
    padding: 18px 20px 20px !important;
}
.result-card {
    padding: 18px;
    border-radius: 12px;
    background: var(--hr-blue-soft);
    border: 1px solid #dbe4ff;
    line-height: 2;
}
.result-card a { color: var(--hr-blue) !important; word-break: break-all; }
.result-card pre {
    white-space: pre-wrap;
    font: inherit;
    line-height: 1.7;
    padding: 12px;
    border-radius: 8px;
    background: #f8fafc;
    margin-bottom: 0;
}
.result-empty { min-height: 220px; box-sizing: border-box; }
#result-panel .result-card { max-height: 60vh; overflow-y: auto; }
#sessions-table .table-wrap { max-height: 240px !important; overflow-y: auto !important; }
#candidate-picker { position: relative !important; }
#candidate-picker input { padding-right: 52px !important; }
#candidate-picker .icon-wrap { right: 52px !important; }
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
    border: 1px solid var(--hr-line-2) !important;
    background: white !important;
    color: var(--hr-ink-2) !important;
    font-size: 24px !important;
    box-shadow: var(--hr-shadow-sm) !important;
}

/* ------------------------------ 7. 表格 ------------------------------ */
/* Gradio 6 的 Dataframe：表头是一个 sticky 的 header-table，
   数据行是绝对定位的 .virtual-body > .virtual-row（.row-odd 是斑马行）。 */
#job-table {
    background: var(--hr-card) !important;
    border: 1px solid var(--hr-line) !important;
    border-radius: var(--hr-r-card) !important;
    box-shadow: var(--hr-shadow-md) !important;
    overflow: hidden;
}
#job-table table {
    table-layout: fixed !important;
    width: 100% !important;
}
#job-table th, #job-table td {
    text-align: center !important;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
#job-table th:nth-child(1), #job-table td:nth-child(1) { width: 14%; }
#job-table th:nth-child(2), #job-table td:nth-child(2) { width: 42%; }
#job-table th:nth-child(3), #job-table td:nth-child(3) { width: 8%; }
#job-table th:nth-child(4), #job-table td:nth-child(4) { width: 18%; }
#job-table th:nth-child(5), #job-table td:nth-child(5) { width: 6%; }
#job-table th:nth-child(6), #job-table td:nth-child(6) { width: 6%; }
#job-table th:nth-child(7), #job-table td:nth-child(7) { width: 6%; }
#job-table thead th button, #job-table thead [class*="menu"] { display: none !important; }
#job-table .header-table th,
#sessions-table .header-table th {
    background: #f2f6ff !important;
    color: var(--hr-ink) !important;
    font-weight: 700 !important;
    font-size: 13px !important;
    padding: 9px 8px !important;
    border-bottom: 1px solid var(--hr-line-2) !important;
}
#job-table .virtual-body .virtual-row,
#sessions-table .virtual-body .virtual-row {
    background: #ffffff !important;
    border-bottom: 1px solid #eef2f9;
    font-size: 13px;
}
#job-table .virtual-body .virtual-row.row-odd,
#sessions-table .virtual-body .virtual-row.row-odd {
    background: #f7f9ff !important;
}
#job-table .virtual-body .virtual-row:hover { background: #eaf0ff !important; }

/* ------------------------------ 8. 弹窗 ------------------------------ */
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
    background: rgba(11, 20, 64, .55) !important;
    backdrop-filter: blur(3px);
    -webkit-backdrop-filter: blur(3px);
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
    border-radius: var(--hr-r-lg) !important;
    box-shadow: 0 28px 70px rgba(6, 12, 40, .45) !important;
    padding: 26px !important;
    width: 100% !important;
    max-width: 900px !important;
    min-width: 0 !important;
    flex: 0 0 auto !important;
    margin: 0 !important;
    gap: 14px !important;
}
.modal-panel .result-card { line-height: 1.8; }
.modal-title { font-size: 1.15em; font-weight: 700; margin: 0 0 4px; }
.modal-hint { color: var(--hr-ink-3); font-size: 13px; line-height: 1.7; margin: 0; }
.modal-danger {
    background: #fef2f2;
    color: #dc2626;
    border: 1px solid #fecaca;
    border-radius: 12px;
    padding: 14px 16px;
    line-height: 1.8;
}
"""


HR_WORKBENCH_CSS = """
/* =============================================================================
   HR 工作台专属：深蓝英雄条 + 途小跃当页头背景。
   途小跃.jpg 的四角是纯黑，screen 混合之后黑会整个化成英雄条的深蓝，
   不会留下一块方形的图片边界。
   ============================================================================= */
.page-hero {
    min-height: 178px;
    background-image:
        url('/static/dist/%E9%80%94%E5%B0%8F%E8%B7%83.jpg'),
        linear-gradient(135deg, var(--hr-navy-2) 0%, var(--hr-navy) 58%, #050a20 100%);
    background-size: auto 172px, cover;
    background-position: right 92px center, center center;
    background-repeat: no-repeat, no-repeat;
    background-blend-mode: screen, normal;
}
.page-hero-title { font-size: 27px !important; }
"""


HR_JOB_MANAGER_CSS = """
/* =============================================================================
   岗位管理专属：跟工作台同一条英雄条，但不放吉祥物，
   右上角只留一个高途大字。
   ============================================================================= */
.page-hero { min-height: 116px; }
.page-hero-title { font-size: 25px !important; }
"""


def _nav_link(href: str, label: str, *, secondary: bool = False) -> str:
    """导航行里的一枚链接（不含外层容器）。"""
    cls = "nav-link secondary" if secondary else "nav-link"
    return (
        f'<a class="{cls}" href="{html.escape(href, quote=True)}">'
        f"{html.escape(label)}</a>"
    )


def _logout_form() -> str:
    """导航行里的登出表单（POST /api/logout）。

    页面卸载时的自动登出改成「宽限期」模式后（刷新 / 站内跳转不再被踢回登录页），
    需要一个 HR 能主动点的登出入口。
    """
    return (
        '<form class="nav-logout" method="post" action="/api/logout">'
        '<button type="submit" class="nav-link secondary">退出登录</button>'
        "</form>"
    )


def _brand_mark() -> str:
    """导航行右上角的高途大字。

    静态资源放在 <data_root>/static/dist/ 下，文件名是中文，
    这里直接写百分号编码后的 URL，省得服务端再转义一次。
    """
    return (
        '<span class="brand-mark">'
        f'<img src="{BRAND_MARK_URL}" alt="高途" />'
        "</span>"
    )


def _nav_row(*items: str) -> str:
    """一行导航，右上角挂高途大字。"""
    return f'<nav class="page-nav">{"".join(items)}{_brand_mark()}</nav>'


def _page_header(title: str, subtitle: str = "", nav: str = "") -> str:
    """整块页头：标题 + 副标题 + 导航行。

    以前标题是 gr.Markdown、导航是 gr.HTML，两个独立的 block，
    底色会在中间断一道缝。合成一个 header 之后才能画成一条完整的英雄条。
    """
    sub = f'<p class="page-hero-sub">{html.escape(subtitle)}</p>' if subtitle else ""
    return (
        '<header class="page-hero">'
        f'<h1 class="page-hero-title">{html.escape(title)}</h1>'
        f"{sub}{nav}"
        "</header>"
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

    if not job_key:
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "请先选择岗位。</div>",
            gr.update(),
            gr.update(),
        )

    job_config = get_job_config_object(job_key)
    if job_config is None:
        # 走到这里说明下拉里那个岗位在库里已经没了 —— 多半是另一个标签页
        # 的岗位管理里把它删了。这边下拉还没来得及刷新，说清楚比「请选择
        # 有效岗位」有用：HR 知道要重新选一次，而不是以为自己操作错了。
        return (
            '<div class="result-card" style="background:#fef2f2;color:#dc2626">'
            "所选岗位已不存在，可能刚被删除。下拉框会自动刷新，请重新选择后再生成链接。"
            "</div>",
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


def toggle_assessment_result(
    session_id: str | None, shown: bool
) -> tuple[str, bool]:
    """切换测评结果区的展开/收起。

    - 当前已展开（shown=True）：收起，返回空结果
    - 当前未展开（shown=False）：展开，返回测评结果

    按钮文字保持"查看测评结果"不变，靠状态切换内容。
    """
    if shown:
        return _empty_result(), False
    return show_assessment_result(session_id), True


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


def _job_choices_signature(choices: list[tuple[str, str]]) -> str:
    """岗位下拉选项的「指纹」。

    回答一个问题：下拉里现在这份选项，和上一次比有没有变？
    变了才需要往下推更新，没变就别动 —— 每隔几秒把同样的选项再推一遍，
    会把 HR 正在点的下拉菜单重绘掉，正在选的人会踩空。

    指纹只认 (岗位名, job_key)：这两项变了，HR 看到的下拉就变了。
    """
    return json.dumps(choices, ensure_ascii=False)


def _empty_result() -> str:
    return '<div class="result-card result-empty"></div>'


def refresh_workbench(
    selected_session_id: str | None,
    selected_job_key: str | None,
    request: gr.Request,
) -> tuple[list[list[str]], dict, dict, str, str, str]:
    """刷新工作台：候选人列表、候选人下拉、岗位下拉、链接、结果区、岗位指纹。

    最后一个返回值是岗位下拉的指纹（_job_choices_signature），
    供定时刷新比对用 —— 详见 refresh_job_picker。
    """
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
        _job_choices_signature(choices),
    )


def refresh_job_picker(
    selected_job_key: str | None,
    known_signature: str | None,
) -> tuple[dict, str]:
    """只刷新岗位下拉，不碰工作台的其他部件。

    为什么要轮询：岗位管理和测评工作台是两个各自挂载的 Gradio 应用
    （见模块开头），页面之间没有推送机制。HR 在另一个标签页新增、改名、
    删掉岗位，这边根本收不到通知，下拉框就一直停在旧快照上 ——
    最坑的是「岗位已经被删了，这边还能拿它生成测评链接」。

    所以工作台每隔几秒自己探一次：指纹没变就 gr.skip() 什么都不推，
    变了才把新的选项推下去，并保证当前选中项要么还在、要么退回第一项。

    返回 (岗位下拉更新, 新指纹)。
    """
    try:
        choices = _job_choices()
    except Exception:  # noqa: BLE001 - 探一次失败不该把整个工作台带崩
        logger.exception("刷新岗位下拉失败，本次跳过")
        return gr.skip(), known_signature

    signature = _job_choices_signature(choices)
    if signature == known_signature:
        return gr.skip(), signature

    keys = [key for _, key in choices]
    value = (
        selected_job_key if selected_job_key in keys
        else keys[0] if keys else None
    )
    return gr.update(choices=choices, value=value), signature


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
        # 页头。标题和导航合成一个 HTML 块，才能画成一条不断的英雄条。
        # 导航是唯一的跨页入口：Gradio 没有页面跳转 API，Button 只能触发 Python 函数，
        # 所以这里直接塞一个 <a>，点了走浏览器导航。
        gr.HTML(
            _page_header(
                "简历评测 · HR工作台",
                "上传简历，生成候选人测评链接，随时查看测评进度与结果。",
                _nav_row(_nav_link(JOB_MANAGER_PATH, "岗位管理"), _logout_form()),
            )
        )

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
                    # 岗位下拉的当前快照指纹，只给定时刷新比对用。
                    # 看不见的 State，HR 不用管它。
                    job_signature = gr.State("")
                    resume_file = gr.File(
                        label="上传简历（PDF / DOC / DOCX / TXT）",
                        file_types=[".pdf", ".doc", ".docx", ".txt"],
                        type="filepath",
                    )
                    create_button = gr.Button("生成候选人链接", variant="primary")
                    created_link = gr.HTML(label="候选人测评链接")
            with gr.Column(scale=7, elem_classes=["workbench-column"]):
                with gr.Group(elem_id="right-panel"):
                    gr.Markdown("### 测评进度")
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
                    result_panel = gr.HTML(value=_empty_result(), elem_id="result-panel")
                    result_shown = gr.State(False)

        create_button.click(
            create_candidate,
            inputs=[candidate_name, job_picker, resume_file],
            outputs=[created_link, sessions_table, selected_session],
        )
        selected_session.change(
            select_candidate,
            inputs=selected_session,
            outputs=[selected_link, result_panel],
        ).then(lambda: False, outputs=result_shown)
        result_button.click(
            toggle_assessment_result,
            inputs=[selected_session, result_shown],
            outputs=[result_panel, result_shown],
        )
        refresh_button.click(
            refresh_workbench,
            inputs=[selected_session, job_picker],
            outputs=[
                sessions_table,
                selected_session,
                job_picker,
                selected_link,
                result_panel,
                job_signature,
            ],
        ).then(lambda: False, outputs=result_shown)
        demo.load(
            refresh_workbench,
            inputs=[selected_session, job_picker],
            outputs=[
                sessions_table,
                selected_session,
                job_picker,
                selected_link,
                result_panel,
                job_signature,
            ],
        ).then(lambda: False, outputs=result_shown)

        # 岗位下拉定时刷新：岗位管理在另一个标签页改了岗位，这边收不到通知。
        # 详见 refresh_job_picker 的说明。定时器只是探针，列表没变就跳过。
        # Gradio 6 的 Timer 用 value 表示间隔秒数（没有 interval 参数）。
        job_timer = gr.Timer(value=5)
        job_timer.tick(
            refresh_job_picker,
            inputs=[job_picker, job_signature],
            outputs=[job_picker, job_signature],
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


def _jd_preview(raw_jd: str | None, *, limit: int = 36) -> str:
    """列表里那一格 JD 的摘要。

    只给开头 —— 全量 JD 塞进表格会把版面撑爆，想看全文得点【JD】那格，
    弹窗里的文本框是完整原文。

    一个 JD 都没有的岗位画一个破折号，HR 一眼能看出「这岗位还没录 JD」，
    而不是被一段空白骗过去。
    """
    text = (raw_jd or "").strip()
    if not text:
        return "—"
    flat = re.sub(r"\s+", " ", text)
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return f"{flat}（{len(text)} 字）"


def _job_table(
    configs: list[dict] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """岗位列表。返回 (表格数据, 行号→job_key)。

    两样东西必须一起返回、来自同一次 list_job_configs()：
    行号到 job_key 的映射一旦和表格行错位，点【编辑】改的就是别的岗位。
    分成两个函数各查一次，中间只要有人改了数据就会串行。

    表格里**不放 job_key** —— 那是后端内部标识，HR 用不上，
    看到一列看不懂的英文反而要多问一句。

    JD 列放摘要：HR 在列表上就能看出「这个岗位有没有录 JD、录了大概什么」，
    想看全文点那一格。
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
            _jd_preview(config.get("raw_jd")),
            _SOURCE_LABELS.get(config.get("source_type"), config.get("source_type") or ""),
            config.get("create_time") or "",
            "编辑",
            "删除",
            "知识库",
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
        "warn": "#fff7e6;color:#9a6700",
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
            "<div>如需修正，请调整 JD 后重新生成规则，或联系开发。</div>"
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


async def _read_jd_source(
    jd_path: str | None,
    jd_text: str = "",
    *,
    empty_hint: str = "请先提供岗位 JD：上传 JD 文件，或在文本框里粘贴 JD 内容。",
) -> str:
    """把「上传的文件」和「粘贴/编辑的文本」合成一份 JD 原文。

    JD 有两个来源：上传的文件和直接粘贴/编辑的文本。传了文件就用文件
    （HR 特意传了文件就是要用这一份），没传文件就用文本框里的内容。
    两样都没有 → 报错，并把 empty_hint 原样抛出去。
    """
    source_jd = jd_text or ""

    if jd_path:
        path = Path(jd_path)
        if path.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
            raise gr.Error("JD 文件仅支持 PDF、DOC、DOCX 或 TXT 格式。")

        try:
            file_jd = await read_resume_text(str(path))
        except Exception as exc:
            raise gr.Error(f"JD 文件读取失败：{exc}") from exc

        if not file_jd.strip():
            raise gr.Error("JD 文件里没有读到任何文字。")

        source_jd = file_jd

    if not source_jd.strip():
        raise gr.Error(empty_hint)

    return source_jd


async def _generate_from_jd(
    jd_path: str | None,
    jd_text: str = "",
    *,
    job_key: str | None = None,
    key_points: str = "",
    empty_hint: str = "请先提供岗位 JD：上传 JD 文件，或在文本框里粘贴 JD 内容。",
) -> tuple[str, str, str, dict]:
    """JD → 岗位配置预览。**不落库。**

    返回 (配置 JSON, 状态卡片, JD 原文, 清空文件框的 gr.update())。
    JD 原文一并回传是为了保存时存进 raw_jd 列，事后能追回这份配置
    来自哪份 JD。末尾清空文件框，省得 HR 连传第二份时还得手动取消。

    job_key 传了就锁死：生成器会自己按 JD 猜一个 key，编辑老岗位时
    猜出来的新 key 会把配置挂到另一个岗位名下，等于凭空多一个岗位、
    老岗位还是旧规则 —— 这正是需求里「job_key 不变」要防的事。
    """
    source_jd = await _read_jd_source(jd_path, jd_text, empty_hint=empty_hint)

    try:
        result = await JobConfigGenerator().generate(
            source_jd, job_key=job_key, key_points=key_points
        )
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
        source_jd,
        gr.update(value=None),
    )


async def generate_job_config(
    jd_path: str | None,
    jd_text: str = "",
    key_points: str = "",
) -> tuple[str, str, str, dict, str]:
    """新增岗位：JD（上传文件或直接粘贴）→ 生成配置预览。不落库。

    返回 (配置 JSON, 状态卡片, 用到的 JD 原文, 清空文件框, 写回文本框的 JD)。
    """
    config_json, status, used_jd, file_update = await _generate_from_jd(
        jd_path, jd_text, key_points=key_points,
        empty_hint="请先上传岗位 JD 文件，或在文本框里粘贴 JD 内容。"
    )
    return config_json, status, used_jd, file_update, used_jd


async def regenerate_job_config(
    jd_path: str | None,
    job_key: str = "",
    jd_text: str = "",
    key_points: str = "",
) -> tuple[str, str, str, dict, str]:
    """编辑岗位：按 JD 重新生成规则。job_key 锁死为原值。

    JD 来源：上传的文件优先，其次是弹窗里那个可以直接改的文本框。
    用文件生成时，文件内容会写回文本框 —— 文本框必须始终是
    「这个岗位现在的 JD」的真相，不能文件在框外、框里还留着旧的。

    返回 (配置 JSON, 状态卡片, 用到的 JD 原文, 清空文件框, 写回文本框的 JD)。
    """
    if not job_key:
        raise gr.Error("没有正在编辑的岗位，请重新从列表点【编辑】。")
    config_json, status, used_jd, file_update = await _generate_from_jd(
        jd_path, jd_text, job_key=job_key, key_points=key_points
    )
    return config_json, status, used_jd, file_update, used_jd


def _persist_config(config_text: str, raw_jd: str | None) -> tuple[str, str]:
    """把编辑器里的配置写进库。返回 (状态卡片, 入库的 job_key)，失败时 key 为空串。

    校验不通过则完全不落库，且**不动编辑器** —— JSON 原样留在那儿，
    HR 照着弹窗里的提示改完直接重存。
    """
    parsed, error = _parse_config_text(config_text)
    if parsed is None:
        return _notice(error), ""

    # 模型生成的配置会带 _gen_source 标记（auto_llm / key_points），
    # 落库前剥掉并据它设 source_type。没有这个标记 = HR 手写 JSON → manual。
    gen_source = parsed.pop("_gen_source", None)
    if gen_source in ("auto_llm", "key_points"):
        source_type = gen_source
    else:
        source_type = "manual"

    job_key = parsed.get("job_key")
    if not job_key:
        return (
            _render_validation(["配置缺少 job_key，无法入库。"], []),
            "",
        )
    # 先查一次只是为了区分提示语是「已新增」还是「已更新」
    existed = get_job_config(job_key) is not None

    try:
        # raw_jd 的三态必须原样传下去，不能在这里压成 None：
        #   None → 「这次不动 JD 原文」，沿用库里存着的那份
        #   ""   → 「JD 原文改成空的」（HR 亲手清空了 JD）
        #   文本 → 「JD 原文换成这份」
        # 之前写成 (raw_jd or "").strip() or None，于是「不动」和「清空」
        # 变成同一件事：清空 JD 会被当成「没改」，库里的旧 JD 一直留着。
        save_job_config(
            parsed,
            source_type=source_type,
            raw_jd=raw_jd.strip() if isinstance(raw_jd, str) else None,
        )
    except JobNameTakenError as exc:
        # 重名：这是「岗位名被人占了」，不是配置写坏了 ——
        # 提示得说人话，别让 HR 去改 JSON。返回空 key，弹窗保持打开，
        # HR 换个岗位名就能直接重存。
        return (
            _notice(
                f"当前岗位已存在：「{exc.job_name}」这个名字已经有岗位在用了，换一个岗位名再保存。",
                tone="error",
            ),
            "",
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


def _jd_drift_note(jd_text: str | None, baseline_jd: str | None) -> str:
    """JD 原文改了但规则没跟着重新生成时的提醒。返回 "" 表示两边一致。

    只提醒、不拦。拦下来的话，改个错别字也要等模型跑完 150 秒；
    模型挂了更是岗位都存不进去。规则是给人改的，JD 只是溯源材料，
    两者对不上要说清楚，但不该由程序替人做主。
    """
    current = (jd_text or "").strip()
    baseline = (baseline_jd or "").strip()
    if current == baseline:
        return ""
    if not baseline:
        return (
            '<div class="result-card" style="background:#fff7e6;color:#9a6700">'
            "<strong>JD 原文与当前规则不是一回事：</strong>"
            "这个岗位此前没有留存 JD，而当前规则不是按你刚填的 JD 生成的。"
            "建议先点【根据 JD 重新生成规则】，再保存。"
            "</div>"
        )
    return (
        '<div class="result-card" style="background:#fff7e6;color:#9a6700">'
        "<strong>JD 原文已改动，但规则仍是按旧 JD 生成的：</strong>"
        "本次保存会把新 JD 原文和旧规则一起存下来，两者对不上。"
        "建议先点【根据 JD 重新生成规则】再保存。"
        "</div>"
    )


def _jd_sync_hint(jd_text: str | None, baseline_jd: str | None) -> str:
    """JD 文本框下方的实时提示：现在这份 JD 和规则对不对得上。"""
    current = (jd_text or "").strip()
    baseline = (baseline_jd or "").strip()
    if not current and not baseline:
        return (
            '<div class="modal-hint">这个岗位没有留存原始 JD。'
            "可以在上面直接粘贴或输入 JD 内容，再点【根据 JD 重新生成规则】。</div>"
        )
    if current == baseline:
        return '<div class="modal-hint">JD 与当前规则所依据的版本一致。</div>'
    return (
        '<div class="modal-hint" style="color:#9a6700;">'
        "JD 已经改动，但下面的规则还是按旧 JD 生成的。"
        "改完请点【根据 JD 重新生成规则】，或直接保存（保存后 JD 与规则会不一致）。"
        "</div>"
    )


def save_new_job(
    config_text: str,
    jd_text: str | None,
    baseline_jd: str | None = None,
) -> tuple[str, str, dict, list[list[str]], list[str]]:
    """新增岗位保存。返回 (弹窗状态, 页面状态, 弹窗可见性, 表格, 行→key)。

    存成功才关弹窗；失败保持打开，让 HR 接着改。

    页面状态是给弹窗关掉之后看的 —— 成功提示写在弹窗里等于没写，
    弹窗一关就没了，HR 只觉得「点了没反应」。
    """
    status, saved_key = _persist_config(config_text, jd_text)
    rows, keys = _job_table()
    if not saved_key:
        return status, "", gr.update(visible=True), rows, keys
    page_status = status + _jd_drift_note(jd_text, baseline_jd)
    return status, page_status, gr.update(visible=False), rows, keys


def save_edited_job(
    config_text: str,
    jd_text: str | None,
    job_key: str,
    baseline_jd: str | None = None,
) -> tuple[str, str, dict, list[list[str]], list[str]]:
    """编辑岗位保存（覆盖）。返回 (弹窗状态, 页面状态, 弹窗可见性, 表格, 行→key)。

    job_key 在这里再钉一次：JSON 编辑器是敞开的，HR 改分值时手滑把
    job_key 改掉，就会在库里多出一个新岗位、老岗位纹丝不动。
    编辑的语义就是「覆盖这个岗位」，key 变了就不是编辑了。

    jd_text 是文本框里现在的 JD 原文 —— 原样存进 raw_jd 列。
    baseline_jd 是当前规则所依据的那版 JD，只用来判断「改过了但没重新生成规则」。
    """
    parsed, error = _parse_config_text(config_text)
    if parsed is None:
        rows, keys = _job_table()
        return _notice(error), "", gr.update(visible=True), rows, keys

    pinned = dict(parsed)
    pinned["job_key"] = job_key
    status, saved_key = _persist_config(
        json.dumps(pinned, ensure_ascii=False), jd_text
    )
    rows, keys = _job_table()
    if not saved_key:
        return status, "", gr.update(visible=True), rows, keys
    page_status = status + _jd_drift_note(jd_text, baseline_jd)
    return status, page_status, gr.update(visible=False), rows, keys


def delete_job_from_ui(
    job_key: str | None,
) -> tuple[str, str, dict, list[list[str]], list[str]]:
    """软删岗位（已在二次确认弹窗里点过确认）。

    返回 (弹窗状态, 页面状态, 弹窗可见性, 表格, 行→key)。
    删除弹窗本身会被关掉，所以结果必须再写一份到页面上。
    """
    if not job_key:
        raise gr.Error("没有待删除的岗位，请重新从列表点【删除】。")
    if not delete_job_config(job_key):
        raise gr.Error(f"岗位不存在或已删除：{job_key}")

    rows, keys = _job_table()
    status = _notice(f"已删除岗位：{job_key}（历史测评记录仍保留关联）", tone="ok")
    return status, status, gr.update(visible=False), rows, keys


def save_knowledge(
    job_key: str | None,
    content: str,
) -> tuple[str, dict]:
    """保存岗位知识库：整份覆盖写入 rag_docs.db。

    候选人深度问答（排班、KPI、提成等）检索的就是这份内容。
    保存后立即生效，不需要重启。
    """
    if not job_key:
        raise gr.Error("请先从列表选择一个岗位。")
    content = (content or "").strip()
    if not content:
        raise gr.Error("知识库内容不能为空。如果要清空，请点【清空】按钮。")
    save_rag_document(rag_doc_id=job_key, job_key=job_key, content=content, title=job_key)
    status = _notice("知识库已保存，候选人提问时立即生效。", tone="ok")
    return status, gr.update(visible=False)


def clear_knowledge(job_key: str | None) -> tuple[str, dict, str]:
    """清空岗位知识库。"""
    if not job_key:
        raise gr.Error("请先从列表选择一个岗位。")
    delete_rag_document(job_key)
    status = _notice("知识库已清空。", tone="ok")
    return status, gr.update(visible=False), ""


# -- 弹窗开关 ---------------------------------------------------------------


def open_create_modal() -> tuple[dict, dict, dict, dict, dict, str, dict]:
    """打开【新增岗位】。每次都清空上一次残留的 JD、JSON、打分关键点。

    返回 (弹窗可见性, 文件框, JD 文本框, JSON 编辑器, 状态卡片, 基准 JD, 打分关键点)。
    """
    return (
        gr.update(visible=True),
        gr.update(value=None),
        gr.update(value=""),
        gr.update(value=""),
        gr.update(value=""),
        "",
        gr.update(value=""),
    )


def _close_modal() -> dict:
    return gr.update(visible=False)


def select_job_action(
    ev: gr.SelectData,
    keys: list[str],
) -> tuple:
    """点岗位列表的【编辑】/【删除】/【知识库】列 → 打开对应弹窗。

    返回 (岗位 key, 原始 JD(基准), 编辑弹窗可见性, 编辑区信息,
    JD 文本框, 编辑区 JSON, 编辑区状态, 编辑区文件框, JD 同步提示,
    打分关键点,
    删除弹窗可见性, 删除区信息,
    知识库弹窗可见性, 知识库文本框, 知识库头部信息)。

    打分关键点每次都清空 —— 那是上一个岗位的东西，
    留着的话 HR 会对着一棵不相关的树做决定。

    只认列号，不看单元格文字：文案以后改成「编辑 ✎」之类，
    按文字匹配就全废了。
    """
    hidden = gr.update(visible=False)
    kb_hidden = gr.update(visible=False)
    blank = gr.update(value="")
    index = list(getattr(ev, "index", None) or [])
    if len(index) < 2:
        return "", "", hidden, "", "", "", "", hidden, "", blank, hidden, "", kb_hidden, "", ""

    row, col = int(index[0]), int(index[1])
    if not (0 <= row < len(keys or [])):
        return "", "", hidden, "", "", "", "", hidden, "", blank, hidden, "", kb_hidden, "", ""

    job_key = keys[row]
    record = get_job_config(job_key, include_deleted=True) or {}
    config = get_job_config_object(job_key) or {}
    job_name = config.get("job_name") or job_key

    # 点【知识库】→ 打开知识库维护弹窗
    if col == COL_KB:
        kb_content = get_rag_document_content(job_key)
        kb_info = (
            f'<div class="modal-hint">岗位：<strong>{html.escape(job_name)}</strong>'
            f" —— 这里维护候选人深度问答的补充知识（排班、KPI、提成等）。"
            f"保存后整份覆盖，立即生效。</div>"
        )
        return (
            "", "", hidden, "", "", "", "", hidden, "",
            blank,
            hidden, "",
            gr.update(visible=True), kb_content, kb_info,
        )

    # 点【JD】那格和点【编辑】是同一件事：都是打开编辑弹窗看/改这个岗位。
    if col in (COL_JD, COL_EDIT):
        raw_jd = record.get("raw_jd") or ""
        info = _edit_info_html(job_name, raw_jd)
        return (
            job_key,
            raw_jd,
            gr.update(visible=True),
            info,
            raw_jd,                                   # JD 文本框（可直接改）
            json.dumps(config, ensure_ascii=False, indent=2) if config else "",
            "",
            gr.update(value=None),                     # 清空文件框
            _jd_sync_hint(raw_jd, raw_jd),
            blank,                                      # 打分关键点
            hidden,
            "",
            kb_hidden, "", "",
        )

    if col == COL_DELETE:
        return (
            job_key,
            "",
            hidden,
            "",
            "",
            "",
            "",
            hidden,
            "",
            blank,
            gr.update(visible=True),
            _delete_info_html(job_name),
            kb_hidden, "", "",
        )

    # 点到姓名/来源/时间列：不是操作，不弹窗
    return "", "", hidden, "", "", "", "", hidden, "", blank, hidden, "", kb_hidden, "", ""


def _edit_info_html(job_name: str, raw_jd: str) -> str:
    """编辑弹窗头部：岗位名 + 说明。

    JD 正文不再画在这里 —— 画成静态 HTML 就没法编辑（打不了字、删不掉、
    粘不进去）。正文放到下面的 JD 文本框里，那里是真正的输入框。

    早期 seed 进来的岗位 raw_jd 是空的（seed_from_files 不传这个字段），
    这时要明说「没有原始 JD」，不能让 HR 以为页面坏了。
    """
    if raw_jd.strip():
        jd_block = (
            '<div class="modal-hint">下面的文本框里是这个岗位现在的 JD 原文，'
            "可以直接改、粘贴或清空。</div>"
        )
    else:
        jd_block = (
            '<div class="modal-hint">该岗位没有留存原始 JD（导入的历史岗位都没有）。'
            "请直接上传新版 JD，或在下方文本框里直接输入、粘贴 JD 内容。</div>"
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
        gr.HTML(
            _page_header(
                "岗位管理",
                "维护岗位 JD 与打分规则；改动会立刻影响新建的候选人测评。",
                _nav_row(
                    _nav_link(WORKBENCH_PATH, "← 返回HR工作台", secondary=True),
                    _logout_form(),
                ),
            )
        )

        row_keys = gr.State(keys)
        active_job_key = gr.State("")
        active_raw_jd = gr.State("")

        with gr.Row():
            add_button = gr.Button("+ 新增岗位", variant="primary", scale=0)
            kb_button = gr.Button("知识库更新", scale=0)

        job_table = gr.Dataframe(
            headers=["岗位名称", "岗位JD", "来源", "创建时间", "编辑", "删除", "知识库"],
            datatype=["str"] * 7,
            value=rows,
            interactive=False,
            wrap=False,
            max_height=320,
            label="岗位列表（点【编辑】【删除】或【知识库】所在的那一格）",
            elem_id="job-table",
        )

        # 弹窗里的状态卡片会随弹窗一起关掉，结果必须在这里另留一份，
        # 否则保存成功后 HR 眼前什么都没有，只会觉得「点了没反应」。
        job_status = gr.HTML()

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
                new_jd_text = gr.Textbox(
                    label="岗位 JD 原文（也可以直接粘贴到这里）",
                    placeholder="上传文件也可以；不传文件就直接把 JD 内容粘进来。",
                    lines=10,
                    max_lines=20,
                    interactive=True,
                )
                new_key_points = gr.Textbox(
                    label="岗位筛选打分关键点（选填）",
                    placeholder=(
                        "想人工指定怎么筛，就写在这里，一行一条。\n"
                        "例如：\n"
                        "本科及以上，学信网可查\n"
                        "同专业优先，机械/自动化优先\n"
                        "近三年有 3D 建模或 CAM 软件经验加分\n"
                        "接受长期出差"
                    ),
                    lines=5,
                    max_lines=12,
                    interactive=True,
                )
                new_generate = gr.Button("根据 JD 生成规则", variant="primary")
                new_status = gr.HTML()
                with gr.Accordion("查看岗位 JSON", open=False):
                    new_editor = gr.Code(
                        label="岗位配置 JSON",
                        language="json",
                        value="",
                        lines=20,
                        interactive=False,
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
                    label="上传新版 JD（不传则按下面的文本框生成）",
                    file_types=SUPPORTED_FILE_TYPES,
                    type="filepath",
                )
                edit_jd_text = gr.Textbox(
                    label="岗位 JD 原文（可直接改、粘贴、删除）",
                    placeholder="这里就是当前岗位的 JD 原文，可以直接改。",
                    lines=12,
                    max_lines=24,
                    interactive=True,
                )
                edit_sync_hint = gr.HTML()
                edit_key_points = gr.Textbox(
                    label="岗位筛选打分关键点（选填）",
                    placeholder=(
                        "想人工指定怎么筛，就写在这里，一行一条。留空就完全按 JD 拆。\n"
                        "例如：本科及以上；同专业优先；接受长期出差"
                    ),
                    lines=4,
                    max_lines=12,
                    interactive=True,
                )
                edit_generate = gr.Button("根据 JD 重新生成规则")
                edit_status = gr.HTML()
                with gr.Accordion("查看岗位 JSON", open=False):
                    edit_editor = gr.Code(
                        label="岗位配置 JSON",
                        language="json",
                        value="",
                        lines=20,
                        interactive=False,
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

        # ---- 知识库维护弹窗 ----
        with gr.Column(
            visible=False, elem_id="kb-modal", elem_classes=["modal-overlay"]
        ) as knowledge_modal:
            with gr.Column(elem_classes=["modal-panel"]):
                kb_info = gr.HTML()
                knowledge_text = gr.Textbox(
                    label="补充知识库 Q&A",
                    placeholder="Q：有没有排班制？\nA：大小周，工作日早9晚6。\n\nQ：提成怎么算？\nA：阶梯式 8%-20%。",
                    lines=15,
                    max_lines=30,
                    interactive=True,
                )
                with gr.Row():
                    kb_cancel = gr.Button("取消", scale=1)
                    kb_clear = gr.Button("清空", scale=1)
                    kb_save = gr.Button("保存", variant="primary", scale=1)

        # ---- 事件接线 ----
        add_button.click(
            open_create_modal,
            outputs=[
                create_modal, new_jd_file, new_jd_text, new_editor, new_status,
                new_raw_jd, new_key_points,
            ],
        )
        kb_button.click(
            lambda: _notice(
                "请在岗位列表中点击某行的「知识库」列，维护该岗位的补充问答（排班、KPI、提成等）。",
                tone="warn",
            ),
            outputs=[job_status],
        )
        new_cancel.click(_close_modal, outputs=create_modal)
        new_generate.click(
            generate_job_config,
            inputs=[new_jd_file, new_jd_text, new_key_points],
            outputs=[new_editor, new_status, new_raw_jd, new_jd_file, new_jd_text],
        )
        new_save.click(
            save_new_job,
            inputs=[new_editor, new_jd_text, new_raw_jd],
            outputs=[new_status, job_status, create_modal, job_table, row_keys],
        )

        edit_cancel.click(_close_modal, outputs=edit_modal)
        edit_generate.click(
            regenerate_job_config,
            inputs=[edit_jd_file, active_job_key, edit_jd_text, edit_key_points],
            outputs=[edit_editor, edit_status, active_raw_jd, edit_jd_file, edit_jd_text],
        )
        edit_save.click(
            save_edited_job,
            inputs=[edit_editor, edit_jd_text, active_job_key, active_raw_jd],
            outputs=[edit_status, job_status, edit_modal, job_table, row_keys],
        )
        # 用 .input() 而不是 .change()：.change() 在后端把值写回文本框时也会触发，
        # 那时读到的 active_raw_jd 可能还是旧基准，提示会闪一下错的。
        # .input() 只在用户真的敲了/粘了字时触发。
        edit_jd_text.input(
            _jd_sync_hint,
            inputs=[edit_jd_text, active_raw_jd],
            outputs=[edit_sync_hint],
        )

        delete_cancel.click(_close_modal, outputs=delete_modal)
        delete_confirm.click(
            delete_job_from_ui,
            inputs=[active_job_key],
            outputs=[delete_info, job_status, delete_modal, job_table, row_keys],
        )

        # 知识库弹窗接线
        kb_cancel.click(_close_modal, outputs=knowledge_modal)
        kb_save.click(
            save_knowledge,
            inputs=[active_job_key, knowledge_text],
            outputs=[job_status, knowledge_modal],
        )
        kb_clear.click(
            clear_knowledge,
            inputs=[active_job_key],
            outputs=[job_status, knowledge_modal, knowledge_text],
        )

        job_table.select(
            select_job_action,
            inputs=[row_keys],
            outputs=[
                active_job_key, active_raw_jd, edit_modal, edit_info,
                edit_jd_text, edit_editor, edit_status, edit_jd_file,
                edit_sync_hint, edit_key_points,
                delete_modal, delete_info,
                knowledge_modal, knowledge_text, kb_info,
            ],
        )

        demo.load(refresh_job_table, outputs=[job_table, row_keys])

    return demo
