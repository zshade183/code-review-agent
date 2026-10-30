"""LLM Provider 抽象。

统一收敛模型调用的单一 Provider：

- 统一的 ``chat`` 接口，支持 OpenAI 兼容的 function calling
- 指数退避重试（区分 429 / 5xx / 超时 / 4xx 不可重试）
- SSE 流式解析（``chat_stream``）
- 异步并发入口（``achat``，用线程池卸载阻塞 IO，不引入额外依赖）
- 模型不支持 tools 时自动降级，供上层切换为文本 ReAct 协议

只依赖已安装的 ``requests``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

import requests

from agent import settings
from agent.schema import TokenUsage, ToolCall

logger = logging.getLogger(__name__)

Message = Dict[str, Any]

#: 不可重试的 HTTP 状态码（请求本身有问题，重试无意义）
NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}


class LLMError(RuntimeError):
    """LLM 调用失败。"""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ToolsUnsupported(LLMError):
    """当前模型 / 网关不支持原生 function calling。"""


@dataclass
class StreamEvent:
    """流式输出事件。"""

    type: str  # delta | usage | done | error
    text: str = ""
    usage: Optional[TokenUsage] = None
    error: str = ""


@dataclass
class LLMResponse:
    """一次 LLM 调用的完整结果。"""

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    finish_reason: str = ""
    latency_ms: float = 0.0
    usage_estimated: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _estimate_tokens(text: str) -> int:
    """在网关不返回 usage 时给出粗略估算。

    中日韩字符按 1 token ≈ 1.5 字符、其余按 1 token ≈ 4 字符估算。
    该值仅用于预算控制与趋势观察，不作为计费依据。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text))
    other = max(0, len(text) - cjk)
    return int(cjk / 1.5 + other / 4.0) + 1


def estimate_usage(messages: Sequence[Message], content: str) -> TokenUsage:
    prompt_chars = 0
    for msg in messages:
        prompt_chars += len(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            prompt_chars += len(json.dumps(call, ensure_ascii=False))
    prompt_tokens = _estimate_tokens("x" * prompt_chars)
    completion_tokens = _estimate_tokens(content)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _parse_arguments(raw: str) -> tuple[Dict[str, Any], str]:
    """解析工具调用参数，返回 (参数字典, 错误信息)。"""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        # 常见退化：模型输出被 markdown 包裹或带前导文本，尝试提取第一个 JSON 对象
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                parsed, _ = decoder.raw_decode(text[match.start():])
                break
            except json.JSONDecodeError:
                continue
        else:
            return {}, f"工具参数不是合法 JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "工具参数必须是 JSON 对象"
    return parsed, ""


class LLMClient:
    """OpenAI 兼容协议的 LLM 客户端。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 3,
        mock: Optional[bool] = None,
    ) -> None:
        self.api_key = settings.API_KEY if api_key is None else api_key
        self.api_url = settings.API_URL if api_url is None else api_url
        self.model = settings.MODEL_NAME if model is None else model
        self.timeout = timeout
        self.max_retries = max_retries
        self.mock = settings.MOCK_MODE if mock is None else mock
        self._tools_supported: Optional[bool] = None
        # ReviewPipeline 会在线程池中并行调用多个领域 Reviewer。requests.Session
        # 不保证跨线程安全，因此每个工作线程各自持有连接池；同一线程内仍能复用连接。
        self._session_local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._session_local, "value", None)
        if session is None:
            session = requests.Session()
            self._session_local.value = session
        return session

    # ---------- 能力探测 ----------

    @property
    def tools_supported(self) -> Optional[bool]:
        """None 表示尚未探测出结论。"""
        return self._tools_supported

    def mark_tools_unsupported(self) -> None:
        self._tools_supported = False
        logger.warning("模型 %s 不支持原生 function calling，将降级为文本 ReAct 协议", self.model)

    # ---------- 请求构造 ----------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]],
        temperature: float,
        stream: bool,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": float(temperature),
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if stream:
            payload["stream"] = True
            # 部分网关（如火山方舟）需要显式声明以在流末尾返回 usage
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.status_code < 400:
            return
        body = ""
        try:
            body = response.text[:500]
        except Exception:
            body = ""
        retryable = response.status_code not in NON_RETRYABLE_STATUS
        raise LLMError(
            f"HTTP {response.status_code}: {body}",
            status=response.status_code,
            retryable=retryable,
        )

    def _extract_response(self, payload: Dict[str, Any], latency_ms: float) -> LLMResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"响应中缺少 choices 字段: {json.dumps(payload, ensure_ascii=False)[:400]}")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        finish_reason = str(choices[0].get("finish_reason") or "")

        tool_calls: List[ToolCall] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            name = str(function.get("name") or "").strip()
            raw_args = str(function.get("arguments") or "")
            arguments, parse_error = _parse_arguments(raw_args)
            if not name:
                continue
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{index}"),
                    name=name,
                    arguments=arguments,
                    raw_arguments=raw_args,
                    parse_error=parse_error,
                )
            )

        raw_usage = payload.get("usage") or {}
        if raw_usage:
            usage = TokenUsage(
                prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                total_tokens=int(raw_usage.get("total_tokens") or 0),
            )
            if usage.total_tokens == 0:
                usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
            estimated = False
        else:
            usage = estimate_usage([], content)
            estimated = True

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=str(payload.get("model") or self.model),
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            usage_estimated=estimated,
            raw=payload,
        )

    # ---------- 同步调用 ----------

    def chat(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        """同步对话（带指数退避重试）。"""
        if self.mock:
            from agent.mock import mock_chat

            return mock_chat(messages, tools)

        if not self.model:
            raise LLMError(
                "未配置 MODEL_NAME：请在 .env 中填写模型 endpoint ID，"
                "或设置 MOCK_MODE=true 使用离线模拟后端。"
            )
        if not self.api_url:
            raise LLMError("未配置 API_URL：请在 .env 中填写模型接口地址。")

        temperature = settings.TEMPERATURE if temperature is None else temperature
        use_tools = bool(tools) and self._tools_supported is not False
        payload = self._payload(messages, tools if use_tools else None, temperature, False, tool_choice)

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            started = time.time()
            try:
                response = self._session().post(
                    self.api_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                self._raise_for_status(response)
                result = self._extract_response(response.json(), (time.time() - started) * 1000.0)
                if use_tools:
                    self._tools_supported = True
                return result
            except LLMError as exc:
                last_error = exc
                if use_tools and exc.status in NON_RETRYABLE_STATUS:
                    # 网关很可能不支持 tools 参数，降级后立刻重试一次
                    self.mark_tools_unsupported()
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    use_tools = False
                    continue
                if not exc.retryable:
                    break
            except requests.exceptions.Timeout as exc:
                last_error = exc
            except requests.exceptions.RequestException as exc:
                last_error = exc
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc

            if attempt < self.max_retries - 1:
                backoff = min(2.0 ** attempt, 8.0)
                logger.warning("LLM 调用失败（第 %d 次），%.1fs 后重试：%s", attempt + 1, backoff, last_error)
                time.sleep(backoff)

        raise LLMError(f"LLM 调用在 {self.max_retries} 次尝试后仍失败: {last_error}")

    async def achat(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        """异步入口：把阻塞的 HTTP 调用卸载到线程池，从而获得真实并发。

        这样在不引入 httpx 的前提下，也能对批量样本实现并发（见
        ``experiments/agent_batch.py``）。
        """
        return await asyncio.to_thread(self.chat, messages, tools, temperature, tool_choice)

    # ---------- 流式调用 ----------

    def chat_stream(
        self,
        messages: Sequence[Message],
        temperature: Optional[float] = None,
    ) -> Iterator[StreamEvent]:
        """流式对话，逐块产出文本增量（用于前端实时展示 Agent 思考过程）。"""
        if self.mock:
            from agent.mock import mock_stream

            yield from mock_stream(messages)
            return

        temperature = settings.TEMPERATURE if temperature is None else temperature
        payload = self._payload(messages, None, temperature, True)

        try:
            with self._session().post(
                self.api_url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
                stream=True,
            ) as response:
                self._raise_for_status(response)
                usage: Optional[TokenUsage] = None
                buffer = ""
                for raw_line in response.iter_lines(decode_unicode=False):
                    if raw_line is None:
                        continue
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    raw_usage = data.get("usage")
                    if raw_usage:
                        usage = TokenUsage(
                            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                            total_tokens=int(raw_usage.get("total_tokens") or 0),
                        )
                    for choice in data.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            buffer += str(delta)
                            yield StreamEvent(type="delta", text=str(delta))
                if usage is None:
                    usage = TokenUsage()
                    usage.completion_tokens = _estimate_tokens(buffer)
                    usage.total_tokens = usage.completion_tokens
                yield StreamEvent(type="usage", usage=usage)
                yield StreamEvent(type="done")
        except LLMError as exc:
            yield StreamEvent(type="error", error=str(exc))
        except requests.exceptions.RequestException as exc:
            yield StreamEvent(type="error", error=f"流式请求失败: {exc}")
