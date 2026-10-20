# 魔搭（ModelScope Studio）hr-ai-assessment 部署镜像
#
# 基础镜像：python:3.10-slim（精简 Debian + Python 3.10）
# 第一次测用这个；要是构建后运行出错，再换你截图里的镜像。

FROM python:3.10-slim

# 系统依赖：lxml（python-docx 的依赖）在 slim 上得编译，
# 还要 gcc / libxml2 / libxslt。下面这一行同时把 bcrypt 等也要的 gcc 一起装。
# 不用 --no-install-recommends 的不行，但保留让 pip 编译能跑。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements.txt，构建缓存命中（依赖不变不会重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip cache purge

# 复制其余代码
COPY . .

# 魔搭约定的端口（健康检查默认探测 7860）
EXPOSE 7860

# 入口：app.py 顶层调 uvicorn.run(app)，监听 0.0.0.0:7860
CMD ["python", "app.py"]
