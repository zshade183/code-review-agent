"""旧 Runtime API 的兼容门面。

正式执行内核统一为 :class:`agent.executors.ToolCallingExecutor`。本模块只保留历史导入、
配置 DTO 和 JSON 提取辅助函数，不再维护第二套 ReAct 循环。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent import settings
from agent.agents.roles import AgentRoleConfig
from agent.executors import ExecutionLimits, ToolCallingExecutor
from agent.llm import LLMClient, Message
from agent.schema import AgentRun
from agent.tools.base import ToolRegistry
from agent.trace import TraceRecorder

Finalizer = Callable[[str], Tuple[Any, str]]
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


@dataclass
class AgentConfig:
    """兼容配置；执行时转换为 ``ExecutionLimits``。"""

    max_steps: int = field(default_factory=lambda: settings.MAX_STEPS)
    max_tool_calls: int = field(default_factory=lambda: settings.MAX_TOOL_CALLS)
    max_total_tokens: int = field(default_factory=lambda: settings.MAX_PROMPT_TOKENS)
    max_wall_clock_s: float = field(default_factory=lambda: settings.MAX_WALL_CLOCK_S)
    repeat_action_limit: int = field(default_factory=lambda: settings.REPEAT_ACTION_LIMIT)
    structured_retry_limit: int = field(default_factory=lambda: settings.STRUCTURED_RETRY_LIMIT)
    temperature: float = field(default_factory=lambda: settings.TEMPERATURE)
    native_tools: bool = True
    text_react_fallback: bool = field(default_factory=lambda: settings.TEXT_REACT_FALLBACK)
    observation_max_chars: int = 4000
    tool_choice: Optional[str] = None

    def to_limits(self) -> ExecutionLimits:
        return ExecutionLimits(
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_total_tokens=self.max_total_tokens,
            max_wall_clock_s=self.max_wall_clock_s,
            repeat_action_limit=self.repeat_action_limit,
            finalizer_retry_limit=self.structured_retry_limit,
            temperature=self.temperature,
            observation_max_chars=self.observation_max_chars,
            tool_choice=self.tool_choice,
        )


@dataclass
class RunOutcome:
    run: AgentRun
    value: Any = None
    text: str = ""


class AgentRuntime:
    """兼容旧调用；内部委托统一执行器。"""

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

    def run(
        self,
        user_prompt: str,
        system_prompt: str = "你是一个严谨的代码审查 Agent。",
        finalizer: Optional[Finalizer] = None,
        task: str = "review",
    ) -> RunOutcome:
        self.trace.task = task
        role = AgentRoleConfig(
            name=task,
            node_name=f"{task}_agent",
            responsibility=task,
            tool_names=tuple(self.registry.names()),
            max_steps=self.config.max_steps,
            max_tool_calls=self.config.max_tool_calls,
        )
        execution = ToolCallingExecutor(
            self.client,
            self.registry,
            role,
            limits=self.config.to_limits(),
            trace=self.trace,
            bind_code=False,
        ).run("", system_prompt, user_prompt, finalizer=finalizer)
        run = self.trace.build(output={"text": execution.content})
        run.usage = execution.usage
        return RunOutcome(run=run, value=execution.value, text=execution.content)

    def stream_text(self, prompt: str, system_prompt: str = "") -> List[str]:
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
    """从模型输出中提取 JSON 主体。"""
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


__all__ = ["AgentConfig", "AgentRuntime", "Finalizer", "RunOutcome", "extract_json_block"]
