import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)


class DeepSeekClient:
    def __init__(self):
        self.api_key = settings.DEEPSEEK_API_KEY
        self.base_url = settings.DEEPSEEK_BASE_URL.rstrip("/")
        self.model = settings.DEEPSEEK_MODEL

    def config_status(self) -> dict:
        """返回当前 AI 配置状态（不暴露完整 key），用于 /debug/ai-status 端点。"""
        key = self.api_key or ""
        return {
            "api_key": {
                "is_set": bool(key),
                "length": len(key),
                "prefix": (key[:6] + "..." + key[-3:]) if len(key) >= 9 else (key if key else ""),
            },
            "base_url": self.base_url or "",
            "model": self.model or "",
            "config_issues": self._check_config(),
        }

    def _check_config(self) -> list[str]:
        issues: list[str] = []
        if not self.api_key:
            issues.append("DEEPSEEK_API_KEY 未配置（应填在 .env 或魔搭密文变量里）")
        if not self.base_url:
            issues.append("DEEPSEEK_BASE_URL 未配置（示例：https://ark.cn-beijing.volces.com/api/v1）")
        if not self.model:
            issues.append("DEEPSEEK_MODEL 未配置（应填模型端点 ID，如 ep-xxxxx）")
        return issues

    async def test_call(self, timeout: float = 10) -> dict:
        """最小化测试：发一句"hi"给 AI，返回真实错误。用于诊断。"""
        if not self.api_key:
            return {"ok": False, "error": "DEEPSEEK_API_KEY 未配置"}
        if not self.base_url or not self.model:
            return {"ok": False, "error": "DEEPSEEK_BASE_URL 或 DEEPSEEK_MODEL 未配置"}
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "temperature": 0,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as e:
            return {"ok": False, "error": f"无法连接 {url}（DNS/网络问题）: {e}"}
        except httpx.TimeoutException as e:
            return {"ok": False, "error": f"连接 {url} 超时（> {timeout}s）: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code == 200:
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                return {"ok": True, "status": 200, "content": content}
            except Exception as e:
                return {"ok": False, "status": 200, "error": f"返回 JSON 结构异常: {e}", "raw": resp.text[:300]}
        # 错误响应：截取关键部分
        body = resp.text[:500]
        reason = {
            401: "API key 无效或过期（HTTP 401）",
            403: "权限不足 / key 未开通此模型（HTTP 403）",
            404: "模型端点不存在 / base_url 错（HTTP 404）",
            429: "请求限流（HTTP 429）",
        }.get(resp.status_code, f"HTTP {resp.status_code}")
        return {"ok": False, "status": resp.status_code, "error": reason, "body": body}

    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY 未配置。检查：.env 里的 DEEPSEEK_API_KEY 是否填了；或魔搭 Space 的密文变量里是否设置了 DEEPSEEK_API_KEY。")
        if not self.base_url:
            raise ValueError("DEEPSEEK_BASE_URL 未配置。示例：https://ark.cn-beijing.volces.com/api/v1")
        if not self.model:
            raise ValueError("DEEPSEEK_MODEL 未配置。应填模型端点 ID（ep-xxxxx 格式）")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        }

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as e:
            raise RuntimeError(f"无法连接 {url}（容器内可能没有外网，或 DNS 失败）: {e}") from e
        except httpx.TimeoutException as e:
            raise RuntimeError(f"调用 {url} 超时（>60s）: {e}") from e

        if response.status_code != 200:
            reason = {
                401: "API key 无效或过期",
                403: "key 未开通此模型 / 权限不足",
                404: "模型端点不存在 / base_url 错",
                429: "请求限流",
            }.get(response.status_code, f"HTTP {response.status_code}")
            raise RuntimeError(f"AI 调用失败：{reason} | 响应: {response.text[:500]}")

        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, ValueError, IndexError) as e:
            raise RuntimeError(f"AI 响应结构异常: {e} | 原文: {response.text[:300]}") from e
