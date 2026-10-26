"""统一 LLM 客户端：MiniMax 主线，火山方舟备用，全局共用。

设计要点：
- 提供方由 settings.LLM_PROVIDER 一处切换，URL/Key/模型随之切换，代码零改动
- 主线调用失败（网络/鉴权/限流/5xx）自动降级到备用提供方，业务侧无感
- chat_json() 对模型输出做容错解析（代码围栏、前后废话、尾逗号），
  这是 JD 生成和简历解析能稳定跑的前提
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import settings

logger = logging.getLogger(__name__)

# 触发降级的 HTTP 状态码：鉴权/限流/服务端故障都该换备用，
# 400 这类请求本身有问题的重试没意义，直接抛出。
_FALLBACK_STATUS = {401, 403, 404, 408, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """所有提供方都失败后抛出。"""


class LLMClient:
    def __init__(self, *, temperature: float = 0.3, timeout: float = 60.0):
        self.temperature = temperature
        self.timeout = timeout

    # -- 诊断 ---------------------------------------------------------------

    def config_status(self) -> dict:
        """给 /debug/ai-status 用。不暴露完整 key。"""
        entries = settings.llm_providers()
        active = settings.active_llm()
        return {
            "current_provider": active["label"],
            "providers": [
                {
                    "label": entry["label"],
                    "is_active": entry["label"] == active["label"],
                    "base_url": entry["base_url"],
                    "model": entry["model"],
                    "api_key_set": bool(entry["api_key"]),
                    "api_key_prefix": (
                        entry["api_key"][:6] + "..." if len(entry["api_key"]) >= 9
                        else ("(too short)" if entry["api_key"] else "")
                    ),
                }
                for entry in entries.values()
            ],
            "config_issues": self.config_issues(),
        }

    def config_issues(self) -> List[str]:
        issues: List[str] = []
        if not settings.MINIMAX_API_KEY:
            issues.append("MINIMAX_API_KEY 未配置（主线提供方）")
        if not settings.MINIMAX_MODEL:
            issues.append("MINIMAX_MODEL 未配置（主线提供方）")
        if not settings.DEEPSEEK_API_KEY:
            issues.append("DEEPSEEK_API_KEY 未配置（备用提供方，缺失则无降级能力）")
        return issues

    # -- 调用 ---------------------------------------------------------------

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """按提供方优先级依次尝试，首个成功的结果即返回。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Optional[Exception] = None

        for entry in settings.llm_providers().values():
            if not (entry["api_key"] and entry["base_url"] and entry["model"]):
                logger.debug("跳过未配置的提供方：%s", entry["label"])
                continue
            try:
                return await self._request(
                    entry, messages,
                    temperature if temperature is not None else self.temperature,
                    max_tokens,
                )
            except _ProviderError as exc:
                last_error = exc
                logger.warning(
                    "提供方 %s 调用失败，降级到下一个：%s", entry["label"], exc
                )

        raise LLMError(
            f"所有 LLM 提供方均不可用，最后一个错误：{last_error}"
        ) from last_error

    async def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: Optional[float] = None,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """要求模型返回 JSON 对象。解析失败会重试，重试仍失败则抛 LLMError。

        重试时会在 system 末尾追加更严格的格式指令 —— 实测比原样重试有效得多。
        """
        strict_suffix = (
            "\n\n【输出格式硬性要求】只输出一个 JSON 对象，不要输出任何解释文字、"
            "不要使用代码围栏、不要输出 JSON 数组或多个对象。"
        )
        current_system = system_prompt
        last_error: Optional[Exception] = None

        for attempt in range(retries + 1):
            try:
                raw = await self.chat(
                    current_system, user_prompt, temperature=temperature
                )
                return extract_json_object(raw)
            except (LLMError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning("JSON 解析失败（第 %d 次）：%s", attempt + 1, exc)
                current_system = system_prompt + strict_suffix

        raise LLMError(f"模型未返回可解析的 JSON：{last_error}") from last_error

    async def _request(
        self,
        provider: Dict[str, str],
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
    ) -> str:
        url = f"{provider['base_url'].rstrip('/')}/chat/completions"
        payload: Dict[str, Any] = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as exc:
            raise _ProviderError(f"无法连接 {url}（DNS/网络）：{exc}") from exc
        except httpx.TimeoutException as exc:
            raise _ProviderError(f"调用 {url} 超时（>{self.timeout}s）") from exc
        except httpx.HTTPError as exc:
            raise _ProviderError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            # 4xx 里只有"可能换个提供方就好了"的才降级；其余直接算最终失败
            retryable = response.status_code in _FALLBACK_STATUS
            raise _ProviderError(
                f"HTTP {response.status_code}（{provider['label']}）：{response.text[:300]}",
                retryable=retryable,
            )

        try:
            return _extract_content(response.json())
        except _ProviderError:
            raise
        except Exception as exc:
            raise _ProviderError(
                f"响应结构异常：{exc}；原文：{response.text[:300]}"
            ) from exc


# ---------------------------------------------------------------------------
# 响应解析：跨提供方 / 跨模型的形状差异都在这里收敛
# ---------------------------------------------------------------------------

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """剥离思维链。

    MiniMax-M3 等推理模型会把 <think>…</think> 直接塞进 content，
    而这些内容里往往含花括号和中文顿号，会污染后续的 JSON 平衡括号扫描。
    关闭推理的模型返回空串，此处直接透传。
    """
    if not text or "<think>" not in text:
        return text or ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # 模型被 max_tokens 截断时可能留下未闭合的 think 块
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.strip()


def _extract_content(payload: Dict[str, Any]) -> str:
    """从 chat/completions 响应里取出正文。

    不同提供方/模型的 message 字段形状不一致，实测踩到过三种：
      1. {"content": "..."}                      —— 最常见
      2. {"content": "..."} 缺失，思考内容在 reasoning_content
      3. {"content": ["..."]}                   —— 部分网关返回片段数组
    这里逐个降级尝试，实在取不到才抛错。
    """
    choices = payload.get("choices") or []
    if not choices:
        raise _ProviderError(f"响应缺少 choices 字段：{str(payload)[:200]}")

    message = choices[0].get("message") or {}

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return strip_reasoning(content)
    if isinstance(content, list):  # 片段数组
        joined = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
        if joined.strip():
            return strip_reasoning(joined)

    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return strip_reasoning(reasoning)

    # 有些网关把正文放在顶层 choices[0].text
    text = choices[0].get("text")
    if isinstance(text, str) and text.strip():
        return strip_reasoning(text)

    raise _ProviderError(
        f"响应中没有可用正文（finish_reason={choices[0].get('finish_reason')!r}）；"
        f"message 字段={sorted(message)}"
    )


class _ProviderError(RuntimeError):
    """单个提供方的失败。retryable=False 表示换谁都一样，直接终止。"""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# JSON 容错解析
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json_object(raw: str) -> Dict[str, Any]:
    """从模型输出里抠出 JSON 对象。"""
    # 先剥思维链：推理内容里的花括号会让后面的括号扫描抓错对象
    text = strip_reasoning(raw or "").strip()
    if not text:
        raise json.JSONDecodeError("模型返回空内容", text, 0)

    for candidate in _json_candidates(text):
        for payload in (candidate, _strip_trailing_commas(candidate)):
            try:
                return _as_object(json.loads(payload))
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError(f"未能从模型输出中解析出 JSON 对象：{text[:300]}", text, 0)


def _json_candidates(text: str) -> List[str]:
    """按"最可能正确"排序的候选片段。

    顺序很关键：
    1. 整体文本 —— 模型直接输出纯 JSON 时命中
    2. 代码围栏内容 —— 有明确边界时命中
    3. 配平括号片段，【末尾优先】—— 模型惯例是先解释后给结论，靠后的那个
       才是答案；末尾优先顺带规避思维链残留花括号造成的误抓
    """
    candidates = [text]

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    # 只保留最外层片段：嵌套结构里的内层 {...} 会被外层包含，
    # 否则 {"a":{"b":1}} 的内层会被误当成独立答案
    fragments: List[str] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("{", cursor)
        if start == -1:
            break
        fragment, end = _scan_balanced(text, start)
        if fragment is None:
            break
        fragments.append(fragment)
        cursor = end  # 从本片段结尾之后继续找下一个，跳过嵌套内容

    for fragment in reversed(fragments):
        if fragment not in candidates:
            candidates.append(fragment)

    return candidates


def _strip_trailing_commas(text: str) -> str:
    """只处理字符串字面量之外的逗号，避免破坏值里本来就有的 '}' 或 ','。"""
    out: List[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
            continue
        if char == ",":
            nxt = next((c for c in text[index + 1 :] if not c.isspace()), "")
            if nxt in ("}", "]"):
                continue  # 丢掉这个尾逗号
        out.append(char)
    return "".join(out)


def _as_object(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise json.JSONDecodeError(f"期望 JSON 对象，实际是 {type(value).__name__}", str(value), 0)
    return value


def _scan_balanced(text: str, start: int) -> Tuple[Optional[str], int]:
    """从 text[start] 处的 '{' 起扫描配平括号，跳过字符串字面量里的括号。"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1
    return None, len(text)