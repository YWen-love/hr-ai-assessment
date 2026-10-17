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

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 等真实值

# 3. 启动 FastAPI 后端（含 Gradio HR 工作台挂载在 /hr）
uvicorn main:app --host 0.0.0.0 --port 8011
# 浏览器打开 http://127.0.0.1:8011/hr

# 4. 或仅启动 Gradio（魔搭部署模式用 7860）
python app.py
# 浏览器打开 http://127.0.0.1:7860
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
- 本地 `http://127.0.0.1:7860/hr` 操作 → 链接变成 `http://127.0.0.1:7860/candidate/xxx`
- 魔搭 `https://ywen666-hr-ai-assessment.ms.show/hr` 操作 → 链接变成 `https://ywen666-hr-ai-assessment.ms.show/candidate/xxx`（自动读 `X-Forwarded-Proto` / `X-Forwarded-Host`）

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