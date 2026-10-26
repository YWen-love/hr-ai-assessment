# HR 简历评测智能体（hr-ai-assessment）

基于大模型的简历解析 + 一对一意向沟通测评系统，面向 HR 场景的候选人初筛与岗位意向确认。

**一句话核心：模型负责对话，代码负责判定。** 分数、分级、备选岗推荐全部由确定性规则引擎计算，模型只生成对话文本，输出再过红线清洗后才下发给候选人。

---

## 核心特性

- **对话式意向测评**：AI 与候选人多轮沟通，自动确认岗位、工时、薪资接受度，结束后自动生成总结与报告
- **三层数据隔离**：HR 看分数 + 备选岗；候选人只看结语，物理上拿不到内部信息
- **岗位配置驱动**：每个岗位一份 `job_config`（打分维度、红线、备选岗映射），支持从 JD 自动生成
- **旁路 RAG 答疑**：候选人反问的非意向类问题走独立 RAG 通道，不污染主线对话上下文
- **多模型切换**：MiniMax 主线 / 火山方舟备用，改 `LLM_PROVIDER` 一项即可切换
- **HR 工作台**：Gradio 岗位管理 + 候选人列表，FastAPI 候选人测评页

---

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 配置环境变量（密钥放在项目外层目录的 .env，仓库内只保留 .env.example）
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY 等

# 3. 启动（FastAPI + 内嵌 Gradio）
uvicorn main:app --host 0.0.0.0 --port 8011
# 本地浏览器： http://127.0.0.1:8011/hr  （HR 工作台）
#             http://127.0.0.1:8011/login （登录页）
```

首次启动会自动：建表（3 个 SQLite 库）、从 `job_configs/*.json` 种子化岗位配置、从 `job_intro.md`/`screen_rules.md` 构建 RAG 知识库。

---

## 目录结构

```
hr-ai-assessment/
├── main.py                    # FastAPI 入口：路由 + 鉴权中间件 + Gradio 挂载
├── app.py                     # Gradio 启动器（魔搭模式用）
├── config.py                  # 配置：多模型切换、HR 账号、Session
├── prompts.py                 # 5 个主线 Prompt 模板（对话/测评/候选人结语/JD生成/备选匹配）
├── resume_parser.py           # 简历解析 + 经验标签（岗位驱动的封闭词表）
├── rule_engine.py             # ★ 规则引擎：打分的唯一实现点
│
├── job_intro.md               # 岗位介绍（===岗位名=== 分隔）
├── screen_rules.md            # 岗位筛选规则（同上）
├── job_configs/               # ★ 岗位配置种子（*.json，启动时导入 SQLite）
│
├── app/
│   ├── llm_client.py          # LLM 客户端 + 多提供方 + JSON 重试
│   ├── assessment_agent.py    # 主线对话 Agent（生成追问 / 收尾）
│   ├── assessment_service.py  # 测评编排：取配置 → 起会话 → 每轮 → 收尾
│   ├── traits.py              # 意向抽取（对话 → 结构化意向结论）
│   ├── redline.py             # ★ 红线清洗：协议标记剥离 + 禁词拦截
│   ├── rag_bypass.py          # 旁路 RAG：候选人反问答疑，不进主线上下文
│   ├── rule_engine.py         # 分数重算（job_config 驱动）
│   ├── text_match.py          # 结构化字段标准化（薪资/工时/教育）
│   ├── job_schema.py          # ★ 岗位配置 Schema：字段定义 + 校验
│   ├── job_generator.py       # JD → job_config 自动生成
│   ├── job_config_store.py    # 岗位配置 CRUD（SQLite）
│   ├── knowledge_base.py      # RAG 知识库（岗位介绍 + 筛选规则）
│   ├── resume_reader.py       # PDF / DOCX / TXT 文本抽取
│   ├── session_store.py       # 会话持久化（3 层数据 + 视图隔离）
│   ├── hr_auth.py             # HR 账号（bcrypt + SQLite）
│   ├── login_page.py          # 登录/注册页渲染
│   ├── gradio_ui.py           # HR 工作台（岗位管理 + 候选人列表）
│   ├── paths.py               # 路径解析（本地 / 魔搭 / 打包）
│   └── schemas.py             # Pydantic 数据模型
│
└── tests/                     # 测试
```

### 依赖方向

```
main.py → { gradio_ui, hr_auth, assessment_service, job_config_store, ... }
assessment_service → { assessment_agent, traits, rule_engine, rag_bypass, redline, session_store, resume_parser }

rule_engine 不 import 任何 LLM —— 分数必须能在没网的环境里复现
redline 在 assessment_agent 和 session_store 之间，是下发给候选人前的最后一道关
```

---

## 核心机制

### 1. 提示词架构：三层 + 多模板

三级提示词工程，编排主体是代码而非模型：

| 层 | 内容 | 实现 |
|---|---|---|
| L1 静态角色模板 | 8 个独立模板，各自定义角色、禁止项、输出格式 | `prompts.py`（5）+ `traits.py` / `resume_parser.py` / `rag_bypass.py`（各 1） |
| L2 运行时注入 | 用 `.replace("{{...}}")` 把 job_config、简历标签、对话历史、封闭词表填入 | `llm_client.py` 合并成一条 system 消息 |
| L3 单次任务指令 | user message，通常一句 + 大块载荷（简历原文、JD、transcript） | 各 service 调用处 |

8 个模板按场景物理隔离：简历解析、主线对话、意向抽取、JD 生成、备选岗匹配、RAG 旁路等，互不串扰。

### 2. 数据流物理隔离（不依赖提示词约束）

关键约束不靠 prompt 叮嘱，靠数据流上根本没有那个字段：

- **主线对话 Agent 不注入 `alternate_role_mapping`** —— 模型不知道备选岗存在，就不可能说漏
- **候选人侧结语由独立一次调用生成** —— 上下文里连 `job_config` 都没有
- **候选人 API 只返回 `candidate_msg`** —— 分数、备选岗、HR 总结物理上不存在于响应体

[redline.py](app/redline.py) 的四道防线（结构层 / 词法层 / 协议层 / 收敛层）是对这个隔离的纵深防御，而不是主要依赖。

### 3. 规则引擎：分数不来自模型

`rule_engine.py` 是打分的唯一实现点。模型可以自评一个 `model_score`，但那只作为对照留档——**入库的分数永远是后端按 `job_config` 重算的结果**。

每次测评保存一份 `job_config_snapshot`，保证历史报告可还原（即使后来岗位配置改了，旧报告的分数也对得上当时的规则）。

### 4. 会话协议：`[DONE]` / `[NEED_RAG]`

模型用协议标记驱动流程，不靠关键词猜：

| 标记 | 含义 | 处理 |
|---|---|---|
| `[DONE]` | 面试结束 | 触发收尾：意向抽取 → 规则打分 → 生成 HR 总结 + 候选人结语 |
| `[NEED_RAG]问句` | 候选人反问需要查知识库 | 走旁路 RAG 通道，**不进主线对话历史** |
| 无标记 | 正常追问 | 直接下发给候选人 |

`MAX_TURNS=20` 作为兜底，防止模型永远不收尾。

### 5. 旁路 RAG：候选人反问不污染主线

候选人问「这个岗位晋升空间大吗」，答案不该出现在主线对话里（否则会干扰后续意向判断）。`rag_bypass.py` 独立处理：

1. 解析 `[NEED_RAG]` 标记，剥离后只把正文下发给候选人
2. 用岗位知识库（`screening_rules`）做 TF-IDF 检索
3. 调独立 LLM 生成答疑
4. 答疑再过 `rag_answer_is_offline` 红线检查（不含备选岗名、不含评分、不含转岗建议）
5. 直接返回给候选人，**不写入 `chat_history`**

### 6. 红线清洗（redline.py）

下发给候选人前的最后一道关，四道防线：

1. **结构层**：剥离 `[DONE]` / `[NEED_RAG]` 协议标记
2. **词法层**：禁词表只收备选岗名与 `backup_job_key`，主岗名绝不进禁词表
3. **协议层**：含 `[NEED_RAG]` 的内容绝不下发
4. **收敛层**：命中红线时返回兜底话术「好的，我这边先记录下来…」，宁可兜底不给残句

### 7. 岗位配置 Schema（job_schema.py）

`job_config` 是整个测评的配置真源，结构：

```
job_key / job_name
├── experience_requirements   # 经验要求 + 封闭词表
├── education_requirements    # 学历要求
├── score_rules[]             # 打分维度（权重 + 各档分值）
├── redline_conditions[]      # 一票否决条件
├── alternate_role_mapping[]  # 备选岗映射（不向候选人暴露）
└── rag_doc_id                # 一岗一库，RAG 隔离
```

`validate_job_config` 区分「必须修」和「建议确认」两类问题——带 `[警告]` 前缀的是建议项，不拦截保存，否则 HR 只能手改 JSON 才能把岗位建起来。

### 8. JD → 配置自动生成

`job_generator.py` 把 JD 文本丢给 LLM，配合 `JD_TO_CONFIG` 模板输出 job_config 草稿。关键设计：

- **字段表 / 操作符表从 `FIELD_TYPES` 常量渲染进 prompt**，不手写第二份——杜绝「Prompt 说 A、引擎只认 B」的静默漂移
- 生成结果只做预览，**不落库**；HR 改完预览里的分值/阈值，再调 `/api/job/save` 落库
- 落库默认 `pending`，还要再调 `/api/job/{key}/review` 标记复核——「AI 生成的配置」和「HR 确认过的配置」必须分得开

### 9. 多层 LLM 调用

不同场景用不同模型（在 `llm_client.py` 按用途配置）：

- 简历解析：快速模型
- 主线对话：对话模型
- 意向抽取：reasoning 模型（结构化输出要求高）
- 报告生成：对话模型

`llm_client.py` 内建 JSON 解析失败重试：追加更严格的格式指令后重试，最多 2 次。

### 10. 数据库（3 个 SQLite，一业务一库）

| 库 | 表 | 用途 |
|---|---|---|
| `resume_sessions.db` | `sessions` | 候选人会话：简历标签 / 对话历史 / 分数 / 意向 / HR总结 / 候选人结语（视图隔离） |
| `rag_knowledge.db` | `jobs` / `knowledge_documents` | 岗位介绍 + 筛选规则（RAG 检索源） |
| `job_config_table`（同 sessions 库） | `job_config_table` | 岗位配置 |
| `hr_users.db` | `hr_users` | HR 账号（bcrypt） |

`session_store` 用 `_ensure_columns` 幂等补列 + 改名迁移，不引入迁移框架。

---

## 关键配置（.env）

密钥只放项目**外层目录**的 `.env`（`hr-ai-assessment/` 的父目录），仓库内只保留 `.env.example`。线上由平台注入环境变量，不依赖 `.env`。

| 变量 | 说明 |
|---|---|
| `LLM_PROVIDER` | `minimax`（默认）或 `volcengine`，改这一项切换模型 |
| `MINIMAX_API_KEY` | MiniMax API Key |
| `MINIMAX_BASE_URL` | 默认 `https://api.minimax.chat/v1` |
| `MINIMAX_MODEL` | MiniMax 模型 |
| `DEEPSEEK_API_KEY` | 火山方舟 API Key（备用） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_MODEL` | 火山方舟配置 |
| `HR_ADMIN_USERNAME` | HR 管理员账号（默认 `admin`） |
| `HR_ADMIN_PASSWORD` | HR 管理员密码（留空则启动时随机生成并打印日志） |
| `PUBLIC_BASE_URL` | 候选人链接前缀（默认空，自动取请求头 `X-Forwarded-Host`） |
| `SESSION_SECRET` | Session 签名密钥（不配则每次启动随机，重启后旧 cookie 失效） |
| `SESSION_MAX_AGE` | cookie 有效期秒数（默认空 = 浏览器关闭即失效） |
| `APP_DATA_DIR` | 自定义数据目录 |
| `BACKGROUND_IMAGE_NAME` | 背景图文件名（默认 `bg.jpg`） |

---

## API 概览

### HR 侧（需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` / `/api/logout` | 登录 / 登出 |
| POST | `/hr/parse-link` | 上传简历 → 生成候选人测评链接 |
| GET | `/hr/sessions` | 候选人列表 |
| GET | `/hr/result/{session_id}` | 测评结果（分数 + 总结） |
| GET | `/hr/report/{session_id}` | 完整报告（静态分 + 意向 + 备选岗） |
| GET/POST | `/api/job/list` `/api/job/{key}` | 岗位配置 CRUD |
| POST | `/api/job/generate` | JD → 配置预览（不落库） |
| POST | `/api/job/save` | 保存配置 |
| POST | `/api/job/{key}/review` | 标记复核 |
| POST | `/api/job/{key}/copy` | 复制岗位（复制品必换 `rag_doc_id`） |
| DELETE | `/api/job/{key}` | 软删岗位 |

### 候选人侧（公开，session_id 即令牌）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/candidate/{session_id}` | 测评页面 |
| POST | `/assessment/start` | 开始测评（上传简历） |
| POST | `/assessment/next` | 候选人回答一句话（自动判断是否收尾 / 走 RAG） |
| GET | `/candidate/result/{session_id}` | 候选人侧结果（只含结语，不含分数） |

---

## HR 工作台登录

- 登录页：`/login`，注册页：`/register`
- 密码存储：bcrypt（rounds=12）
- 默认管理员：启动时按 `HR_ADMIN_USERNAME` / `HR_ADMIN_PASSWORD` 同步到库；密码留空则随机生成并 WARNING 打印
- 自注册：用户名 3-32 字符字母开头，密码至少 6 位；重名靠 `username UNIQUE` 约束兜底
- 关闭标签页自动登出（`pagehide` + `sendBeacon`）

## 背景图

三页（登录 / HR 工作台 / 候选人）共用一张背景图，放 `<data_root>/static/bg.jpg`。图片缺失时渐变 overlay 仍生效，页面正常显示。

## 运行数据

- 候选人会话：`<data_root>/resume_sessions.db`
- RAG 知识库：`<data_root>/rag_knowledge.db`
- HR 账号：`<data_root>/hr_users.db`
- 上传简历：`<data_root>/uploads/`

> `data_root()`：魔搭容器用 `/mnt/workspace/hr-ai-assessment/`，本地用 `app/data/`，可被 `APP_DATA_DIR` 覆盖。

## 测试

```bash
pytest tests/ -q
```

覆盖岗位配置 Schema 校验、JD 生成的分值规则、规则引擎打分、红线清洗、简历解析、意向抽取、报告组装、E2E 测评流程、HR 工作台 UI 等。

## 部署到魔搭（ModelScope）

1. Space SDK 选 **python**（不是 gradio）—— 本项目是 FastAPI + Gradio 混合架构
2. 入口文件：`app.py`，端口 7860
3. 环境变量设 `LLM_PROVIDER` + 对应 API Key / Base URL / Model
4. 数据自动持久化到 `/mnt/workspace/hr-ai-assessment/`（容器重启不丢）
5. 候选人链接域名自动从 `X-Forwarded-Proto` / `X-Forwarded-Host` 取，无需硬编码
