import os
import secrets
from typing import Optional
from dotenv import load_dotenv

# 本地开发读取.env；魔搭容器无.env，自动跳过，读取平台环境变量
load_dotenv()


def _resolve_session_secret() -> str:
    """SessionMiddleware 签名密钥。env 显式设置最稳；为空则随机生成（重启后旧 cookie 会失效）。"""
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return configured
    # 每次启动随机；不写入文件，避免影响部署幂等性
    return secrets.token_urlsafe(48)


class Settings:
    # 大模型火山方舟配置
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "")
    DEEPSEEK_REASONING_MODEL: str = os.getenv("DEEPSEEK_REASONING_MODEL", "")

    # 项目基础信息
    APP_NAME: str = os.getenv("APP_NAME", "resume_assessment_agent")
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"

    # HR管理员登录（兼容旧 .env：ADMIN_USERNAME / ADMIN_PASSWORD 仍可用）
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

    # 新版 HR 账号配置（推荐显式设置）
    HR_ADMIN_USERNAME: str = os.getenv("HR_ADMIN_USERNAME", os.getenv("ADMIN_USERNAME", "admin"))
    HR_ADMIN_PASSWORD: str = os.getenv("HR_ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", ""))

    # 公网根地址，生成候选人测评链接使用
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")

    # 会话/cookie 配置
    SESSION_SECRET: str = _resolve_session_secret()
    SESSION_COOKIE_NAME: str = os.getenv("SESSION_COOKIE_NAME", "hr_session")
    # 默认 None = 会话级 cookie（浏览器关闭即失效，下次访问要重新登录）。
    # 想要持久化（例如 24 小时免登录），在 .env 设 SESSION_MAX_AGE=86400
    _env_max_age = os.getenv("SESSION_MAX_AGE", "").strip()
    SESSION_MAX_AGE: Optional[int] = int(_env_max_age) if _env_max_age else None

    # 静态资源目录中的背景图文件名（启动时检测存在性）
    BACKGROUND_IMAGE_NAME: str = os.getenv("BACKGROUND_IMAGE_NAME", "bg.jpg")


settings = Settings()