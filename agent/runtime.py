"""手写轻量 Agent Runtime（ReAct）。

为什么不用框架：Agent 的全部工程难点都在这个循环里——终止条件、预算控制、重复动作
检测、工具参数纠错、结构化输出校验与重试、上下文膨胀控制。自己实现一遍才能把这些
细节讲清楚，也便于按需替换每一部分。

循环骨架::

    组装消息 -> 调用 LLM
        ├─ 返回 tool_calls  -> 执行工具 -> 观察结果回填 -> 继续循环
        └─ 返回文本         -> 交给 finalizer 校验
                                ├─ 校验通过 -> 结束（stop_reason=final）
                                └─ 校验失败 -> 把错误回填 -> 继续循环（结构化重试）

停止条件（任一命中即停）：
``final`` / ``max_steps`` / ``max_tool_calls`` / ``token_budget`` / ``timeout`` /
``no_progress`` / ``llm_error`` / ``finalizer_failed``

对不支持原生 function calling 的模型，自动降级为文本 ReAct 协议
（``Thought / Action / Action Input / Final Answer``），解析逻辑见
``_parse_text_action``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent import settings
from agent.llm import LLMClient, LLMError, Message
from agent.schema import AgentRun, TokenUsage, ToolCall
from agent.tools.base import ToolRegistry
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)

#: finalizer：接收模型最终文本，返回 (解析结果, 错误信息)。错误信息非空则触发重试。
Finalizer = Callable[[str], Tuple[Any, str]]

TEXT_ACTION_RE = re.compile(
    r"Action\s*[:：]\s*([A-Za-z_][A-Za-z0-9_]*)\s*\n\s*Action\s*Input\s*[:：]\s*(.*)",
    re.S | re.I,
)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


def _scan_json_object(text: str) -> Tuple[str, str]:
    """从文本中扫描出第一个完整的 JSON 对象（括号配对，能正确处理嵌套）。

    相比非贪婪正则，这里不会在嵌套对象的内层 ``}`` 处提前截断。
    """
    start = text.find("{")
    if start == -1:
        return "", "未找到 JSON 对象（缺少 '{'）"
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
                return text[start : index + 1], ""
    return text[start:], "JSON 对象括号不配对（可能被输出截断）"


@dataclass
class AgentConfig:
    """单次运行的预算与策略。"""

    max_steps: int = field(default_factory=lambda: settings.MAX_STEPS)
    max_tool_calls: int = field(default_factory=lambda: settings.MAX_TOOL_CALLS)
    max_total_tokens: int = field(default_factory=lambda: settings.MAX_PROMPT_TOKENS)
    max_wall_clock_s: float = field(default_factory=lambda: settings.MAX_WALL_CLOCK_S)
    repeat_action_limit: int = field(default_factory=lambda: settings.REPEAT_ACTION_LIMIT)
    structured_retry_limit: int = field(default_factory=lambda: settings.STRUCTURED_RETRY_LIMIT)
    temperature: float = field(default_factory=lambda: settings.TEMPERATURE)
    #: 是否优先使用原生 function calling；关闭则强制走文本 ReAct
    native_tools: bool = True
    text_react_fallback: bool = field(default_factory=lambda: settings.TEXT_REACT_FALLBACK)
    #: 单个工具结果回填给模型时的最大字符数（控制上下文膨胀）
    observation_max_chars: int = 4000
    tool_choice: Optional[str] = None


@dataclass
class RunOutcome:
    """runtime.run 的返回值封装。"""

    run: AgentRun
    value: Any = None
    text: str = ""


class AgentRuntime:
    """通用 ReAct Agent 运行时。"""

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        config: Optional[AgentConfig] = None,
        trace: Optional[TraceRecorder] = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.config = config or AgentConfig()
        self.trace = trace or TraceRecorder()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(
        self,
        user_prompt: str,
        system_prompt: str = "你是一个严谨的代码审查 Agent。",
        finalizer: Optional[Finalizer] = None,
        task: str = "review",
    ) -> RunOutcome:
        self.trace.task = task
        started = time.time()
        total_usage = TokenUsage()
        tool_call_count = 0
        action_history: Dict[str, int] = {}
        repeat_strikes = 0
        finalizer_attempts = 0
        last_text = ""
        value: Any = None

        use_native = self.config.native_tools
        messages: List[Message] = [
            {"role": "system", "content": self._build_system_prompt(system_prompt, use_native)},
            {"role": "user", "content": user_prompt},
        ]

        stop_reason = "incomplete"
        for _step in range(1, self.config.max_steps + 1):
            # ---- 预算检查 ----
            if time.time() - started > self.config.max_wall_clock_s:
                stop_reason = "timeout"
                break
            if total_usage.total_tokens > self.config.max_total_tokens:
                stop_reason = "token_budget"
                break

            # ---- 调用 LLM ----
            tools_schema = self.registry.schemas() if use_native else None
            call_started = time.time()
            try:
                response = self.client.chat(
                    messages,
                    tools=tools_schema,
                    temperature=self.config.temperature,
                    tool_choice=self.config.tool_choice if tools_schema else None,
                )
            except LLMError as exc:
                self.trace.mark_error(str(exc))
                self.trace.add_step("error", content=f"LLM 调用失败：{exc}")
                stop_reason = "llm_error"
                break

            total_usage.accumulate(response.usage)
            latency_ms = (time.time() - call_started) * 1000.0
            last_text = response.content or ""

            # 原生模式失败时降级为文本协议
            if use_native and self.client.tools_supported is False and self.config.text_react_fallback:
                use_native = False
                messages[0] = {
                    "role": "system",
                    "content": self._build_system_prompt(system_prompt, use_native),
                }
                self.trace.add_step(
                    "think",
                    content="检测到该模型不支持原生 function calling，已切换到文本 ReAct 协议。",
                )

            calls = response.tool_calls
            if not calls and not use_native:
                call = self._parse_text_action(response.content)
                if call is not None:
                    calls = [call]

            # ---- 分支一：无工具调用 -> 视为最终答案 ----
            if not calls:
                if finalizer is None:
                    self.trace.add_step(
                        "final", content=last_text, usage=response.usage, latency_ms=latency_ms
                    )
                    stop_reason = "final"
                    break

                parsed, error = self._safe_finalize(finalizer, last_text)
                if not error:
                    value = parsed
                    self.trace.add_step(
                        "final", content=last_text, usage=response.usage, latency_ms=latency_ms
                    )
                    stop_reason = "final"
                    break

                finalizer_attempts += 1
                self.trace.add_step(
                    "think",
                    content=f"结构化输出校验未通过（第 {finalizer_attempts} 次）：{error}",
                    usage=response.usage,
                    latency_ms=latency_ms,
                )
                if finalizer_attempts > self.config.structured_retry_limit:
                    stop_reason = "finalizer_failed"
                    break
                messages.append({"role": "assistant", "content": last_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你上一次的输出未通过结构化校验，错误如下：\n"
                            f"{error}\n\n"
                            "请修正后重新输出**完整且合法**的 JSON，不要包含任何解释文字、不要使用 markdown 代码块。"
                        ),
                    }
                )
                continue

            # ---- 分支二：执行工具 ----
            if response.content:
                self.trace.add_step(
                    "think", content=response.content, usage=response.usage, latency_ms=latency_ms
                )
            else:
                self.trace.add_step("think", content="(模型直接发起工具调用)", usage=response.usage, latency_ms=latency_ms)

            if tool_call_count + len(calls) > self.config.max_tool_calls:
                stop_reason = "max_tool_calls"
                self.trace.add_step(
                    "error", content=f"工具调用次数将达到上限（{self.config.max_tool_calls}），停止。"
                )
                break

            messages.append(self._assistant_tool_message(response.content, calls, use_native))

            for call in calls:
                tool_call_count += 1
                signature = call.signature()
                seen = action_history.get(signature, 0)
                action_history[signature] = seen + 1

                if seen >= self.config.repeat_action_limit:
                    repeat_strikes += 1
                    reminder = (
                        f"[重复调用拦截] 你已经用相同参数调用过 `{call.name}` {seen} 次，"
                        "结果不会发生变化。请基于已有观察给出结论，或换一个参数/换一个工具。"
                    )
                    self.trace.add_step("observe", content=reminder)
                    messages.append(self._observation_message(call, reminder, use_native))
                    if repeat_strikes > 2:
                        stop_reason = "no_progress"
                        break
                    continue

                tool = self.registry.get(call.name)
                if tool is None:
                    observation = (
                        f"[未知工具] 不存在名为 `{call.name}` 的工具。可用工具：{', '.join(self.registry.names())}"
                    )
                    self.trace.add_step("observe", content=observation)
                    messages.append(self._observation_message(call, observation, use_native))
                    continue

                if call.parse_error:
                    observation = (
                        f"[参数解析失败] {call.parse_error}。原始参数：{call.raw_arguments!r}。"
                        "请重新以合法 JSON 对象形式给出参数。"
                    )
                    self.trace.add_step("observe", content=observation)
                    messages.append(self._observation_message(call, observation, use_native))
                    continue

                result = tool(**call.arguments)
                self.trace.add_step("act", content=tool.description, tool_call=call, tool_result=result)
                observation = result.to_observation(self.config.observation_max_chars)
                messages.append(self._observation_message(call, observation, use_native))

            if stop_reason == "no_progress":
                break

            # 上下文膨胀控制：保留 system + 初始 user + 最近若干轮
            messages = self._trim_messages(messages)
        else:
            stop_reason = "max_steps"

        self.trace.mark_stop(stop_reason)
        report = value if hasattr(value, "issues") else None
        run = self.trace.build(report=report, output=None if report else {"text": last_text})
        run.usage = total_usage
        return RunOutcome(run=run, value=value, text=last_text)

    # ------------------------------------------------------------------
    # 提示词与消息组装
    # ------------------------------------------------------------------

    def _build_system_prompt(self, base: str, use_native: bool) -> str:
        if use_native:
            return base
        return (
            f"{base}\n\n"
            "【工具使用协议】\n"
            "当前环境通过文本协议调用工具。你每次**只能输出一种**格式：\n\n"
            "要么调用工具：\n"
            "Thought: <你的推理>\n"
            "Action: <工具名>\n"
            "Action Input: <JSON 对象>\n\n"
            "要么给出最终答案：\n"
            "Thought: <你的推理>\n"
            "Final Answer: <最终结果>\n\n"
            "可用工具：\n"
            f"{self.registry.text_spec()}\n\n"
            "严格约束：\n"
            "1. Action Input 必须是单行合法 JSON 对象，键名必须来自上面声明的参数名\n"
            "2. 一次只输出一个 Action，等待系统返回 Observation 后再继续\n"
            "3. 不要臆造 Observation，不要重复调用相同参数的工具"
        )

    def _assistant_tool_message(self, content: str, calls: Sequence[ToolCall], use_native: bool) -> Message:
        if not use_native:
            text = content or ""
            for call in calls:
                text += (
                    f"\nAction: {call.name}\nAction Input: "
                    f"{json.dumps(call.arguments, ensure_ascii=False)}"
                )
            return {"role": "assistant", "content": text}

        return {
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {
                    "id": call.id or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for index, call in enumerate(calls)
            ],
        }

    def _observation_message(self, call: ToolCall, observation: str, use_native: bool) -> Message:
        if use_native:
            return {
                "role": "tool",
                "tool_call_id": call.id or "",
                "content": observation,
            }
        return {"role": "user", "content": f"Observation ({call.name}):\n{observation}"}

    def _trim_messages(self, messages: List[Message], keep_recent: int = 10) -> List[Message]:
        """控制上下文长度：保留 system 与首条 user，其余只留最近若干条。"""
        if len(messages) <= keep_recent + 2:
            return messages
        head = [messages[0]]
        if messages[1].get("role") == "user":
            head.append(messages[1])
        # 保证裁剪后不会从 tool 消息开始（否则部分网关会报错）
        trimmed = messages[-keep_recent:]
        while trimmed and trimmed[0].get("role") == "tool":
            trimmed = trimmed[1:]
        return head + trimmed

    # ------------------------------------------------------------------
    # 文本 ReAct 解析
    # ------------------------------------------------------------------

    def _parse_text_action(self, content: str) -> Optional[ToolCall]:
        """从文本输出中解析 Action / Action Input。"""
        if not content:
            return None
        match = TEXT_ACTION_RE.search(content)
        if not match:
            return None
        name = match.group(1).strip()
        raw_tail = match.group(2)
        raw_input, scan_error = _scan_json_object(raw_tail)

        arguments: Dict[str, Any] = {}
        parse_error = scan_error
        if not parse_error:
            try:
                parsed = json.loads(raw_input)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    parse_error = "Action Input 必须是 JSON 对象"
            except json.JSONDecodeError as exc:
                parse_error = f"Action Input 不是合法 JSON：{exc.msg}"
        return ToolCall(
            id=f"text_{hashlib.md5((name + raw_input).encode()).hexdigest()[:8]}",
            name=name,
            arguments=arguments,
            raw_arguments=raw_input[:400],
            parse_error=parse_error,
        )

    # ------------------------------------------------------------------
    # 结构化输出
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_finalize(finalizer: Finalizer, text: str) -> Tuple[Any, str]:
        try:
            return finalizer(text)
        except Exception as exc:  # noqa: BLE001 - 校验器异常不应中断 Agent
            return None, f"结果解析异常：{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------

    def stream_text(self, prompt: str, system_prompt: str = "") -> List[str]:
        """流式获取文本（用于前端实时展示），返回增量列表。"""
        messages: List[Message] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        chunks: List[str] = []
        for event in self.client.chat_stream(messages):
            if event.type == "delta":
                chunks.append(event.text)
            elif event.type == "error":
                chunks.append(f"\n[流式错误] {event.error}")
        return chunks


def extract_json_block(text: str) -> str:
    """从模型输出中稳健地取出 JSON 主体。

    优先使用 ```json 围栏，其次取首尾花括号之间的内容；两者都失败则原样返回，
    交由 Pydantic 报出具体错误（而不是像原来那样静默返回空结果）。
    """
    if not text:
        return ""
    fenced = JSON_BLOCK_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped
