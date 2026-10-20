# HR 简历评测智能体（hr-ai-assessment）

基于大模型的简历解析 + 一对一意向沟通测评 Demo，面向 HR 场景的候选人初筛。

## 📂 目录结构

```
hr-ai-assessment/
├── .env.example              # 环境变量模板（拷贝为 .env 后填写真实值）
├── README.md                 # 本文档
├── requirements.txt          # Python 依赖清单
│
├── app.py                    # 魔搭默认启动入口（Gradio 主页面）
├── main.py                   # FastAPI 后端（提供 REST 接口 + 内嵌 Gradio）
│
├── config.py                 # 应用配置（API Key、Base URL 等）
├── prompts.py                # 测评 Agent 的 prompt 模板
├── resume_parser.py          # 简历文本解析（含 CandidateProfile 模型）
├── rule_engine.py            # 候选人筛选规则引擎
│
├── job_intro.md               # 各岗位介绍（===岗位名=== 分隔，供 RAG 检索）
├── screen_rules.md           # 各岗位筛选规则（同上）
│
└── app/                      # 后端子包（支持模块）
    ├── __init__.py
    ├── paths.py              # 路径解析（兼容源码 / PyInstaller 打包）
    ├── schemas.py            # Pydantic 数据模型
    ├── gradio_ui.py          # Gradio HR 工作台（被 app.py 调用）
    ├── assessment_agent.py   # 测评 Agent（生成问题 / 总结）
    ├── assessment_service.py # 测评流程编排
    ├── deepseek_client.py    # DeepSeek / 火山方舟 API 客户端
    ├── job_reader.py         # 岗位知识库读取
    ├── knowledge_base.py     # SQLite RAG 知识库（岗位介绍 + 筛选规则）
    ├── resume_reader.py      # PDF / DOCX / TXT 文本抽取
    └── session_store.py      # 候选人测评会话持久化（SQLite）
```

## 🔓 公网演示账号

> ⚠️ **这是公开在 GitHub 仓库的演示账号，禁止在演示项目里上传真实候选人简历。**

部署到魔搭的实例使用以下共享账号供 HR 团队 / 访客访问：

| 字段 | 值 |
|---|---|
| HR 工作台地址 | `https://<your-space>.ms.show/hr` |
| 用户名 | `HR01` |
| 密码 | `HR01-123456` |

打开上方的 HR 工作台地址，按提示输入用户名 / 密码即可进入。

**重要说明**

- 该账号是公开 demo 凭证（已写入本仓库），**任何拿到魔搭链接的人都能登录**。
- 请只在演示项目里**放合成测试数据**，不要上传真实简历。
- 若需正式部署（接入真实候选人数据）：删除本节，并在魔搭控制台 → 环境变量里**重设** `HR_ADMIN_USERNAME` / `HR_ADMIN_PASSWORD`，然后**私下把新账号密码发给你信任的人**。

---

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 等真实值

# 3. 启动 FastAPI 后端（含 Gradio HR 工作台挂载在 /hr，本地调试用）
uvicorn main:app --host 0.0.0.0 --port 8011
# 本地浏览器： http://127.0.0.1:8011/hr
# 部署到魔搭后直接访问公网入口（见下文"部署到魔搭"）

# 4. 或仅启动 Gradio（魔搭部署模式用 7860）
python app.py
# 本地浏览器： http://127.0.0.1:7860
# 部署到魔搭后直接访问公网入口（见下文"部署到魔搭"）
```

## 🌐 部署到魔搭（ModelScope Space）

### 1. 创建 Space 时选对 SDK

**SDK 必须选 `python`（或 `None` / 自定义），不能选 `gradio`。**

本项目是 FastAPI + Gradio 混合架构（候选人页走 FastAPI，HR 工作台走 Gradio），不是纯 Gradio app。如果选 `sdk=gradio`，魔搭 launcher 会按纯 Gradio 模式调 `POST /invoke` 健康检查，必然 404 启动失败。

- 选 sdk=python → 魔搭直接 `python app.py`，由我们自己的 uvicorn 启动 FastAPI+Gradio
- 入口文件：`app.py`
- 端口：7860（魔搭自动注入 `GRADIO_SERVER_PORT=7860` 环境变量）

### 2. 数据持久化（关键）

魔搭容器默认是**临时存储**，容器重启 SQLite 会被清空，导致所有候选人 session 丢失。

代码已经自动处理：检测到 `/data` 目录可写时，SQLite 和上传文件会写到 `/data/hr-ai-assessment/`（持久化）。本地开发无 `/data`，回退到 `app/data/`。

```python
# app/paths.py 自动选择
def data_root():
    if APP_DATA_DIR 环境变量: 用环境变量
    elif /data 可写（魔搭容器）: 用 /data/hr-ai-assessment/  ← 持久化
    else: 用 app/data/  ← 本地
```

### 3. 公网链接（base_url 自动跟随）

候选人测评链接的域名不需要硬编码，代码会自动从请求头里取：

- **公网入口**：魔搭部署后，HR 在 `https://ywen666-hr-ai-assessment.ms.show/hr` 操作 → 给候选人发的链接变成 `https://ywen666-hr-ai-assessment.ms.show/candidate/xxx`（自动读 `X-Forwarded-Proto` / `X-Forwarded-Host`）
- **项目展示页**（Studio 元信息、说明文档）：`https://www.modelscope.cn/studios/YWen666/hr-ai-assessment`
- **本地开发**（裸跑 uvicorn，无反代）：在 `.env` 设 `PUBLIC_BASE_URL=http://127.0.0.1:7860` 后，链接变成 `http://127.0.0.1:7860/candidate/xxx`

不用再设 `PUBLIC_BASE_URL` 环境变量。

### 4. 部署流程

1. 魔搭控制台 → 新建 Space → SDK 选 **python**（不是 gradio）
2. 把整个 `hr-ai-assessment` 目录推上去
3. 在 Space 的"环境变量"里设（必填，AI 测评要）：
   ```
   DEEPSEEK_API_KEY=你的key
   DEEPSEEK_BASE_URL=https://ark.cn-beijing.volces.com/api/v1
   DEEPSEEK_MODEL=ep-xxxxx
   ```
4. 等启动成功（看日志：应看到 `Uvicorn running on http://0.0.0.0:7860`）
5. 访问 `https://你的空间名.ms.show/hr` → HR 工作台
6. 访问 `https://你的空间名.ms.show/` → 简化版 HTML 上传页（备选）
7. 候选人测评页 = `https://你的空间名.ms.show/candidate/{session_id}`（由 HR 工作台生成）

### 5. 关于 Gradio 自带的 query 参数

候选人测评链接可能带 Gradio client 自动注入的 query 参数：
```
/candidate/02e49844-.../?__theme=light&backend_url=%2F&t=...&web_env=other
```

这些参数（`__theme`、`backend_url`、`t`、`web_env`）是 Gradio 维护客户端状态用的，**对后端无影响**——FastAPI 路由只匹配 path 部分（`/candidate/{session_id}`），自动忽略 query。候选人页面是普通 HTML+JS，**不依赖** Gradio client，所以即使带这些参数也能正常打开。

## 🧠 岗位知识库

- 岗位介绍维护在 `job_intro.md`，筛选规则维护在 `screen_rules.md`
- 用 `===岗位名称===` 分隔不同岗位
- 首次启动会自动构建 SQLite RAG 知识库到 `app/data/rag_knowledge.db`
- 新增 / 修改岗位后，**重启服务**即可同步

## 📊 运行数据

- 候选人会话库：`app/data/resume_sessions.db`
- 岗位 RAG 库：`app/data/rag_knowledge.db`
- 上传的简历：`app/data/uploads/`

> 这些目录和数据库由程序自动创建，无需手动初始化。

## ⚙️ 关键配置（.env）

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 火山方舟 / DeepSeek API Key（**必填**，否则 AI 测评不可用） |
| `DEEPSEEK_BASE_URL` | 默认 `https://ark.cn-beijing.volces.com/api/v1` |
| `DEEPSEEK_MODEL` | 模型端点 ID |
| `PUBLIC_BASE_URL` | 候选人访问链接前缀（默认空，会自动取当前请求地址；魔搭部署时填魔搭公网地址） |
| `APP_DATA_DIR` | 自定义数据目录（不填则用 `app/data/`） |
| `SESSION_SECRET` | Session 签名密钥（**首次部署必填**，否则每次重启所有 HR 都会被迫重新登录） |
| `HR_ADMIN_USERNAME` | HR 管理员账号（默认 `admin`） |
| `HR_ADMIN_PASSWORD` | HR 管理员密码（**首次部署强烈建议设置**；留空则启动时随机生成并打印到日志） |

## 🔐 HR 工作台登录

HR 工作台（`/hr`）强制登录后才能访问，未登录会被 302 到 `/login`。

### 默认账号

- 首次启动时，`hr_users` 表为空 → 程序自动创建默认账号：
  - 用户名：`HR_ADMIN_USERNAME`（默认 `admin`）
  - 密码：若 `HR_ADMIN_PASSWORD` 已设 → 用该密码；否则随机生成，**WARNING 级别打印到日志**（仅启动时一次）
- 后续重启 → 表里若已有用户，会按 .env 同步默认 admin（改名 / 改密 / 跳过，详见 `seed_default_admin`）
- 数据库位置：`<data_root>/hr_users.db`（与候选人会话库分开）
- 密码存储：bcrypt 散列（不是明文）

### 自注册

- 注册页：部署到魔搭后访问 `https://ywen666-hr-ai-assessment.ms.show/register`
- 登录页底部有「还没有账号？立即注册」入口
- 表单：账号 / 密码 / 确认密码
- 验证规则：
  - 用户名：3-32 字符，字母开头，可含字母、数字、下划线、连字符
  - 密码：至少 6 位
  - 两次密码必须一致
- 注册成功 → **自动登录** → 跳回原请求页
- 重名 → 400 + "该用户名已被注册"
- 依赖 `username UNIQUE` 约束兜底，避开先 SELECT 后 INSERT 的竞态

### 登录/登出

- 登录页：部署到魔搭后访问 `https://ywen666-hr-ai-assessment.ms.show/login`
- 登录后跳回原请求页（如 `/hr`、`/hr/sessions`）
- 登出：`POST /api/logout`（当前 Gradio 工作台未挂登出按钮，可手动调接口或清 cookie）
- Session cookie 名：`hr_session`，有效期 `SESSION_MAX_AGE` 秒（默认 86400 = 24 小时）

### 公开路径（无需登录）

- `/`（根，简化版 HTML 上传页）
- `/login`、`/register`
- `/api/login`、`/api/register`、`/api/logout`
- `/static/*`（背景图等静态资源）
- `/candidate/{session_id}`（候选人测评页，session_id 本身即访问令牌）

## 🖼 三页统一背景图

登录页、HR 工作台、候选人测评页**共用一张背景图**。

### 放置位置

把图片放到 `<data_root>/static/bg.jpg`：

| 部署环境 | 实际路径 |
|---|---|
| 本地开发 | `<本地仓库根目录>/app/data/static/bg.jpg` |
| 魔搭部署 | `/data/hr-ai-assessment/static/bg.jpg` |

> 目录不存在也没关系，程序启动时会自动 `mkdir -p`。
> `data_root()` 的解析逻辑见 `app/paths.py`（魔搭用 `/data/hr-ai-assessment/`，本地用 `app/data/`）。

### 支持的格式

默认引用 `/static/bg.jpg`。如换 PNG/WebP/SVG：

1. 把图片放到 `<data_root>/static/bg.png`（或 `bg.webp`、`bg.svg`）
2. 改两处代码里的扩展名：
   - `app/login_page.py` 第 12 行：`BG_URL = "/static/bg.<ext>"`
   - `app/gradio_ui.py` 第 48 行：`url('/static/bg.<ext>')`
   - `main.py` 的 `render_candidate_page()` `<style>` 中：`url('/static/bg.<ext>')`

### 图片缺失的降级

图片不存在时 `url('/static/bg.jpg')` 会 404，但**渐变 overlay 仍生效**，三页都能正常显示（只是没有照片做底）。建议至少放一张浅色图保证视觉效果。

## 📁 文件清单（按本版本）

`app/hr_auth.py` — HR 账号数据库（SQLite + bcrypt）  
`app/login_page.py` — 登录页 HTML 渲染  
`main.py` — FastAPI 后端（含登录中间件 + `/static` 静态资源）  
`app/gradio_ui.py` — Gradio HR 工作台（`HR_UI_CSS` 顶部含背景图 CSS）