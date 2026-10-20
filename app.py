"""魔搭默认启动入口（FastAPI + Gradio 统一服务）。

ModelScope / 魔搭 部署时自动以本文件作为入口，启动服务。
本地调试：`uvicorn main:app --port 8011`（独立 FastAPI 入口）。

架构：
- main.py 已包含 FastAPI 全套接口（含 /candidate/{session_id} 候选人对话、HR 接口等）
- Gradio HR 工作台在 main.py 里挂载在 /hr 路径
- 本文件复用 main.py 的 app，用 uvicorn 在魔搭期望端口（7860）统一启动

为什么不用 demo.launch()？
    Gradio 的 demo.launch() 只能服务 Gradio UI，不会暴露 FastAPI 路由。
    候选人打开 https://...ms.show/candidate/xxx 时会 404。
    改用 uvicorn.run(app) 后，FastAPI + Gradio 共存于 7860 端口。
"""
import logging
import os

import uvicorn

from main import app

logger = logging.getLogger(__name__)

# ModelScope 期望顶层 demo 变量；FastAPI app 也满足要求
demo = app


if __name__ == "__main__":
    # 端口优先级：
    #   1. GRADIO_SERVER_PORT 环境变量（魔搭自动注入，通常是 7860）
    #   2. 默认 7860（Gradio 默认端口，也是魔搭健康检查期望的端口）
    server_port = int(os.getenv("GRADIO_SERVER_PORT", "7860"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=server_port,
        log_level="info",
    )