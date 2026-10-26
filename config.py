import os
import secrets
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# .env 加载规范（定稿要求）：
#   1. 真实密钥只放项目【外层目录】的 .env，仓库内只保留 .env.example
#   2. 代码固定读这个绝对路径，不做逐级搜索 —— 搜索行为在不同启动目录下结果不可预测
#   3. 线上（魔搭/服务器/GitHub）由平台注入系统环境变量，不依赖 .env
#   4. 优先级：平台环境变量 > 本地外层 .env
#      靠 override=False 实现：已有环境变量不会被 .env 覆盖
# ---------------------------------------------------------------------------

def _local_env_path() -> Path:
    """项目外层目录的 .env，即 hr-ai-assessment 的父目录。"""
    project_dir = Path(__file__).resolve().parent
    return project_dir.parent / ".env"


_ENV_FILE = _local_env_path()
if _ENV_FILE.is_file():
    load_dotenv(dotenv_path=_ENV_FILE, override=False)


def _resolve_session_secret() -> str:
    """SessionMiddleware 签名密钥。env 显式设置最稳；为空则随机生成（重启后旧 cookie 会失效）。"""
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return configured
    # 每次启动随机；不写入文件，避免影响部署幂等性
    return secrets.token_urlsafe(48)


class Settings:
    # ------------------------------------------------------------------
    # 大模型配置：主线 MiniMax（付费），备用火山方舟
    # 切换只需改 LLM_PROVIDER 一项，无需改代码。
    # ------------------------------------------------------------------
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "minimax").lower()

    # MiniMax 主线。BASE_URL / API_URL 两种命名都接受，避免配置方对不上。
    MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")
    MINIMAX_BASE_URL: str = (
        os.getenv("MINIMAX_BASE_URL")
        or os.getenv("MINIMAX_API_URL")
        or "https://api.minimax.chat/v1"
    )
    MINIMAX_MODEL: str = os.getenv("MINIMAX_MODEL", "")

    # 火山方舟备用
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

    # ------------------------------------------------------------------
    # LLM 提供方解析：一处切换，URL/Key/模型全部随之切换
    # ------------------------------------------------------------------
    def llm_providers(self) -> dict[str, dict[str, str]]:
        """按 LLM_PROVIDER 排好序的候选提供方列表，第一个为当前生效的。"""
        providers = {
            "minimax": {
                "label": "MiniMax",
                "api_key": self.MINIMAX_API_KEY,
                "base_url": self.MINIMAX_BASE_URL,
                "model": self.MINIMAX_MODEL,
            },
            "volcengine": {
                "label": "火山方舟",
                "api_key": self.DEEPSEEK_API_KEY,
                "base_url": self.DEEPSEEK_BASE_URL,
                "model": self.DEEPSEEK_MODEL,
            },
        }
        preferred = self.LLM_PROVIDER if self.LLM_PROVIDER in providers else "minimax"
        ordered = [preferred] + [k for k in providers if k != preferred]
        return {key: providers[key] for key in ordered}

    def active_llm(self) -> dict[str, str]:
        """当前生效的提供方配置（含已解析好的完整 base_url 与 model）。"""
        for entry in self.llm_providers().values():
            if entry["api_key"] and entry["base_url"] and entry["model"]:
                return entry
        return self.llm_providers()[self.LLM_PROVIDER] if self.LLM_PROVIDER in (
            "minimax", "volcengine"
        ) else self.llm_providers()["minimax"]


settings = Settings()