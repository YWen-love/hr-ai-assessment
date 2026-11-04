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
    """所有提供方都失败后抛出。

    tools_unsupported=True 表示「提供方不认 tools 参数」——
    调用方可以据此改用 JSON 信封模式（让模型把工具调用写成一段 JSON）重试。
    """

    def __init__(self, message: str, *, tools_unsupported: bool = False):
        super().__init__(message)
        self.tools_unsupported = tools_unsupported


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
        text, _calls, _raw = await self.chat_messages(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        return text

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

    async def chat_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, List[Dict[str, Any]], List[Any]]:
        """多轮对话。返回 (正文, 工具调用, 原始工具调用)。

        工具调用归一化成 {"id", "name", "arguments", "arguments_error"}；
        原始 tool_calls 一并返回 —— 继续对话时要把它们原样塞回 assistant 消息，
        网关按 id 认这次调用。

        提供方不认 tools 参数时不会静默吞掉：抛 LLMError(tools_unsupported=True)，
        由调用方改用 JSON 信封模式重试。
        """
        last_error: Optional[Exception] = None
        saw_tools_unsupported = False

        for entry in settings.llm_providers().values():
            if not (entry["api_key"] and entry["base_url"] and entry["model"]):
                logger.debug("跳过未配置的提供方：%s", entry["label"])
                continue
            try:
                return await self._request_messages(
                    entry, messages,
                    temperature if temperature is not None else self.temperature,
                    max_tokens, tools, tool_choice,
                )
            except _ProviderError as exc:
                last_error = exc
                if exc.tools_unsupported:
                    saw_tools_unsupported = True
                    logger.warning(
                        "提供方 %s 不支持原生函数调用：%s", entry["label"], exc
                    )
                else:
                    logger.warning(
                        "提供方 %s 调用失败，降级到下一个：%s", entry["label"], exc
                    )

        if saw_tools_unsupported:
            raise LLMError(
                f"所有 LLM 提供方均不支持原生函数调用，最后一个错误：{last_error}",
                tools_unsupported=True,
            ) from last_error

        raise LLMError(
            f"所有 LLM 提供方均不可用，最后一个错误：{last_error}"
        ) from last_error

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tools: List[Dict[str, Any]],
        tool_choice: Optional[Any] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, List[Dict[str, Any]], List[Any]]:
        """发一次带工具定义的请求。返回 (正文, 工具调用, 原始工具调用)。

        注意：提供方不支持原生函数调用时这里直接抛 LLMError(tools_unsupported=True)，
        不会自动改成信封模式 —— 提示词里【怎么调】那一段也要跟着换，
        所以这个决定得由拿着提示词的调用方做。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.chat_messages(
            messages, tools=tools, tool_choice=tool_choice,
            temperature=temperature, max_tokens=max_tokens,
        )

    async def _request_messages(
        self,
        provider: Dict[str, str],
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> Tuple[str, List[Dict[str, Any]], List[Any]]:
        payload: Dict[str, Any] = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        body = await self._post(provider, payload)

        if not tools:
            return _extract_content(body), [], []

        text, calls, raw_calls = _extract_tool_calls(body)
        if not text and not calls:
            raise _ProviderError(
                "响应里既没有正文也没有工具调用；"
                f"message 字段={sorted((body.get('choices') or [{}])[0].get('message') or {})}"
            )
        return text, calls, raw_calls

    async def _post(
        self,
        provider: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """发一次请求，返回解析后的 JSON body。错误统一抛 _ProviderError。"""
        url = f"{provider['base_url'].rstrip('/')}/chat/completions"

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
                tools_unsupported=_looks_like_tools_unsupported(
                    response.status_code, response.text
                ),
            )

        try:
            body = response.json()
        except Exception as exc:
            raise _ProviderError(
                f"响应结构异常：{exc}；原文：{response.text[:300]}"
            ) from exc

        if not isinstance(body, dict):
            raise _ProviderError(f"响应结构异常：顶层不是 JSON 对象（{type(body).__name__}）")

        return body


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
    """单个提供方的失败。retryable=False 表示换谁都一样，直接终止。

    tools_unsupported=True 表示「这个网关不认 tools 参数」——
    不是配置问题，是能力缺失，换提供方也许能解决，所以和普通失败分开标记。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        tools_unsupported: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.tools_unsupported = tools_unsupported


# ---------------------------------------------------------------------------
# 工具调用解析
# ---------------------------------------------------------------------------


def _extract_text_lenient(message: Dict[str, Any], choice: Dict[str, Any]) -> str:
    """取正文，取不到就返回空串（不抛错）。

    走工具调用时正文可能是空的 —— 模型把整轮回答都放在 tool_calls 里，
    这时候空串是正常状态，不能当错误。
    """
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

    text = choice.get("text")
    if isinstance(text, str) and text.strip():
        return strip_reasoning(text)

    return ""


def _coerce_arguments(args: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """把工具参数归一成 dict。

    OpenAI 标准里 arguments 是 JSON 字符串，部分网关直接给对象；
    两种都认。解析不了就返回 ({}, 错误说明)，让调用方把错误回灌给模型重发。
    """
    if isinstance(args, dict):
        return args, None
    if isinstance(args, str) and args.strip():
        text = strip_reasoning(args).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = extract_json_object(text)
            except Exception:
                return {}, f"工具参数不是合法 JSON：{text[:200]}"
        if isinstance(parsed, dict):
            return parsed, None
        return {}, f"工具参数不是 JSON 对象：{text[:200]}"
    return {}, "工具参数为空"


def _extract_tool_calls(
    payload: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]], List[Any]]:
    """从 chat/completions 响应里取出正文和工具调用。

    返回 (正文, 归一化后的调用, 原始调用列表)。
    原始调用要原样回填进 assistant 消息 —— 继续对话时网关按 id 认这次调用。
    """
    choices = payload.get("choices") or []
    if not choices:
        raise _ProviderError(f"响应缺少 choices 字段：{str(payload)[:200]}")

    choice = choices[0]
    message = choice.get("message") or {}
    text = _extract_text_lenient(message, choice)

    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raw_calls = []

    calls: List[Dict[str, Any]] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = function.get("name") or raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments, error = _coerce_arguments(
            function.get("arguments", raw.get("arguments"))
        )
        calls.append(
            {
                "id": raw.get("id"),
                "name": name.strip(),
                "arguments": arguments,
                "arguments_error": error,
                "type": raw.get("type") or "function",
            }
        )

    return text, calls, list(raw_calls)


def _looks_like_tools_unsupported(status_code: int, text: str) -> bool:
    """判断这个 4xx 是不是「不认 tools 参数」。

    只在 400/404/415/422 这类「请求形状不对」的状态码上看，
    并且要求报错正文里提到了 tool 或 function —— 否则那多半是别的参数写错了，
    换个提供方也没用。
    """
    if status_code not in (400, 404, 415, 422):
        return False
    low = (text or "").lower()
    return "tool" in low or "function" in low


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