import os
from dotenv import load_dotenv

# 本地开发读取.env；魔搭容器无.env，自动跳过，读取平台环境变量
load_dotenv()

class Settings:
    # 大模型火山方舟配置
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "")
    DEEPSEEK_REASONING_MODEL: str = os.getenv("DEEPSEEK_REASONING_MODEL", "")

    # 项目基础信息
    APP_NAME: str = os.getenv("APP_NAME", "resume_assessment_agent")
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"

    # HR管理员登录（去掉默认账号密码，线上必须手动填，避免安全漏洞）
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

    # 公网根地址，生成候选人测评链接使用
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")


settings = Settings()
