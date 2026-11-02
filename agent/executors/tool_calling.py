"""统一的模型/工具执行循环。

Review、Verification 与 Fix 共用本执行器。角色只提供提示词、工具白名单和预算；
执行器负责模型协议适配、工具调用、finalizer 重试、预算与轨迹。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent import settings
from agent.agents.roles import AgentRoleConfig
from agent.evidence_service import EvidenceService
from agent.schema import TokenUsage, ToolCall
from agent.tools.base import ToolRegistry
from agent.trace import TraceRecorder

Finalizer = Callable[[str], Tuple[Any, str]]


@dataclass(frozen=True)
class ExecutionLimits:
    max_steps: int
    max_tool_calls: int
    max_total_tokens: int = field(default_factory=lambda: settings.MAX_PROMPT_TOKENS)
    max_wall_clock_s: float = field(default_factory=lambda: settings.MAX_WALL_CLOCK_S)
    repeat_action_limit: int = field(default_factory=lambda: settings.REPEAT_ACTION_LIMIT)
    finalizer_retry_limit: int = field(default_factory=lambda: settings.STRUCTURED_RETRY_LIMIT)
    temperature: float = field(default_factory=lambda: settings.TEMPERATURE)
    observation_max_chars: int = 4000
    tool_choice: str | None = None

    @classmethod
    def from_role(cls, role: AgentRoleConfig) -> "ExecutionLimits":
        return cls(max_steps=role.max_steps, max_tool_calls=role.max_tool_calls)


@dataclass
class AgentExecutionResult:
    content: str
    steps: int
    stop_reason: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_evidence: List[Dict[str, Any]] = field(default_factory=list)
    value: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""


class ToolCallingExecutor:
    """执行一个已配置角色的模型/工具循环。"""

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        role: AgentRoleConfig,
        evidence_service: EvidenceService | None = None,
        limits: ExecutionLimits | None = None,
        trace: TraceRecorder | None = None,
        bind_code: bool = True,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.role = role
        self.evidence_service = evidence_service
        self.limits = limits or ExecutionLimits.from_role(role)
        self.trace = trace
        self.bind_code = bind_code

    def _bound_model(self) -> Any:
        if not len(self.registry) or not hasattr(self.llm, "bind_tools"):
            return self.llm
        try:
            return self.llm.bind_tools(
                self.registry.schemas(), tool_choice=self.limits.tool_choice or "auto"
            )
        except (AttributeError, NotImplementedError, TypeError, ValueError):
            return self.llm

    @staticmethod
    def _message_dict(message: BaseMessage) -> Dict[str, Any]:
        if isinstance(message, SystemMessage):
            return {"role": "system", "content": str(message.content)}
        if isinstance(message, HumanMessage):
            return {"role": "user", "content": str(message.content)}
        if isinstance(message, ToolMessage):
            return {
                "role": "tool", "content": str(message.content),
                "tool_call_id": message.tool_call_id,
            }
        calls = []
        for item in getattr(message, "tool_calls", None) or []:
            if not isinstance(item, dict):
                continue
            calls.append({
                "id": str(item.get("id") or ""), "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": json.dumps(item.get("args") or {}, ensure_ascii=False),
                },
            })
        payload: Dict[str, Any] = {"role": "assistant", "content": str(message.content or "")}
        if calls:
            payload["tool_calls"] = calls
        return payload

    def _invoke(self, model: Any, messages: List[BaseMessage], *, with_tools: bool = True) -> tuple[BaseMessage, TokenUsage, float]:
        started = time.perf_counter()
        if hasattr(model, "invoke"):
            response = model.invoke(messages)
            usage = TokenUsage()
            raw_usage = getattr(response, "usage_metadata", None) or {}
            if isinstance(raw_usage, dict):
                usage = TokenUsage(
                    prompt_tokens=int(raw_usage.get("input_tokens") or raw_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(raw_usage.get("output_tokens") or raw_usage.get("completion_tokens") or 0),
                    total_tokens=int(raw_usage.get("total_tokens") or 0),
                )
            return response, usage, (time.perf_counter() - started) * 1000

        if not hasattr(self.llm, "chat"):
            raise TypeError(f"模型 {type(self.llm).__name__} 既不支持 invoke 也不支持 chat")
        response = self.llm.chat(
            [self._message_dict(item) for item in messages],
            tools=self.registry.schemas() if with_tools and len(self.registry) else None,
            temperature=self.limits.temperature,
            tool_choice=self.limits.tool_choice,
        )
        calls = [
            {"id": item.id, "name": item.name, "args": item.arguments}
            for item in response.tool_calls
        ]
        message = AIMessage(content=response.content or "", tool_calls=calls)
        return message, response.usage, float(response.latency_ms or (time.perf_counter() - started) * 1000)

    @staticmethod
    def _calls(message: BaseMessage) -> Sequence[Dict[str, Any]]:
        calls = getattr(message, "tool_calls", None) or []
        return [item for item in calls if isinstance(item, dict)]

    def _effective_args(self, name: str, args: Dict[str, Any], code: str) -> Dict[str, Any]:
        """源码参数由运行时注入，模型不能替换实际任务目标。"""
        effective = dict(args)
        tool = self.registry.get(name)
        properties = (tool.parameters.get("properties") or {}) if tool is not None else {}
        if self.bind_code and "code" in properties:
            effective["code"] = code
        return effective

    def run(
        self,
        code: str,
        system: str,
        user: str,
        finalizer: Finalizer | None = None,
    ) -> AgentExecutionResult:
        model = self._bound_model()
        messages: List[BaseMessage] = [SystemMessage(content=system), HumanMessage(content=user)]
        records: List[Dict[str, Any]] = []
        observations: List[Dict[str, Any]] = []
        usage = TokenUsage()
        calls_used = 0
        finalizer_attempts = 0
        action_counts: Dict[str, int] = {}
        started = time.perf_counter()
        last_content = ""
        value: Any = None
        error = ""

        def finish(reason: str, step: int) -> AgentExecutionResult:
            if self.trace is not None:
                self.trace.mark_stop(reason)
                if error:
                    self.trace.mark_error(error)
            return AgentExecutionResult(
                last_content, step, reason, records, observations, value, usage, error
            )

        for step in range(1, self.limits.max_steps + 1):
            if time.perf_counter() - started > self.limits.max_wall_clock_s:
                return finish("timeout", step - 1)
            if usage.total_tokens > self.limits.max_total_tokens:
                return finish("token_budget", step - 1)
            try:
                response, step_usage, latency_ms = self._invoke(model, messages)
            except Exception as exc:  # provider failures are surfaced as an execution result
                error = f"{type(exc).__name__}: {exc}"
                if self.trace is not None:
                    self.trace.add_step("error", content=f"LLM 调用失败：{error}")
                return finish("llm_error", step)

            usage.accumulate(step_usage)
            last_content = str(getattr(response, "content", "") or "")
            calls = list(self._calls(response))
            if not calls:
                if finalizer is None:
                    if self.trace is not None:
                        self.trace.add_step("final", content=last_content, usage=step_usage, latency_ms=latency_ms)
                    return finish("final", step)
                try:
                    parsed, validation_error = finalizer(last_content)
                except Exception as exc:  # finalizer failures must not crash the service
                    parsed, validation_error = None, f"结果校验异常：{type(exc).__name__}: {exc}"
                if not validation_error:
                    value = parsed
                    if self.trace is not None:
                        self.trace.add_step("final", content=last_content, usage=step_usage, latency_ms=latency_ms)
                    return finish("final", step)
                finalizer_attempts += 1
                if self.trace is not None:
                    self.trace.add_step("think", content=f"输出校验未通过：{validation_error}",
                                        usage=step_usage, latency_ms=latency_ms)
                if finalizer_attempts > self.limits.finalizer_retry_limit:
                    error = validation_error
                    return finish("finalizer_failed", step)
                messages.extend([
                    response if isinstance(response, AIMessage) else AIMessage(content=last_content),
                    HumanMessage(content=(
                        "你上一次的输出未通过校验，错误如下：\n"
                        f"{validation_error}\n\n请按原任务要求重新输出完整结果。"
                    )),
                ])
                continue

            if self.trace is not None:
                self.trace.add_step("think", content=last_content or "(模型发起工具调用)",
                                    usage=step_usage, latency_ms=latency_ms)
            messages.append(response if isinstance(response, AIMessage) else AIMessage(content=last_content))
            budget_hit = False
            no_progress = False
            for raw in calls:
                name = str(raw.get("name") or "")
                args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
                call_id = str(raw.get("id") or f"{self.role.name}_{step}_{calls_used}")
                if calls_used >= self.limits.max_tool_calls:
                    budget_hit = True
                    messages.append(ToolMessage(content="工具调用预算已用完，请直接给出最终结果。",
                                                tool_call_id=call_id))
                    continue
                calls_used += 1
                args = self._effective_args(name, args, code)
                signature = json.dumps([name, args], ensure_ascii=False, sort_keys=True, default=str)
                action_counts[signature] = action_counts.get(signature, 0) + 1
                if action_counts[signature] > self.limits.repeat_action_limit:
                    no_progress = True
                    messages.append(ToolMessage(content="相同工具调用重复过多，停止执行。", tool_call_id=call_id))
                    continue

                tool = self.registry.get(name)
                result = None
                record = None
                tool_call = ToolCall(id=call_id, name=name, arguments=args)
                if tool is None:
                    observation = f"未知或无权使用工具 {name}；允许工具：{', '.join(self.registry.names())}"
                    ok = False
                else:
                    if self.evidence_service is not None:
                        result, record = self.evidence_service.execute(name, args, self.role.name)
                    else:
                        result = tool(**args)
                    observation = result.to_observation(self.limits.observation_max_chars)
                    if record is not None:
                        observation = f"[evidence_id={record.evidence_id}]\n{observation}"
                    ok = bool(result.ok)
                    observations.append({
                        "agent": self.role.name, "tool": name,
                        "evidence_id": record.evidence_id if record is not None else "",
                        "ok": ok, "arguments": args,
                        "content": result.content[: self.limits.observation_max_chars],
                    })
                records.append({
                    "step": step, "tool": name, "arguments": args, "ok": ok,
                    "error_type": result.error_type if result else "unknown_tool",
                    "attempts": result.attempts if result else 0,
                    "from_cache": result.from_cache if result else False,
                    "duration_ms": result.duration_ms if result else 0,
                })
                if self.trace is not None:
                    self.trace.add_step("act", content=f"调用工具 {name}", tool_call=tool_call)
                    self.trace.add_step("observe", content=observation, tool_result=result)
                messages.append(ToolMessage(content=observation, tool_call_id=call_id))
            if no_progress:
                return finish("no_progress", step)
            if budget_hit:
                # 保留一次无工具总结机会。
                try:
                    final_response, final_usage, latency_ms = self._invoke(self.llm, [
                        *messages,
                        HumanMessage(content="工具预算已用完，请立即基于已有观察给出最终结果，不要再调用工具。"),
                    ], with_tools=False)
                    usage.accumulate(final_usage)
                    last_content = str(getattr(final_response, "content", "") or "")
                    if not self._calls(final_response):
                        if finalizer is None:
                            return finish("budget_final", step + 1)
                        parsed, validation_error = finalizer(last_content)
                        if not validation_error:
                            value = parsed
                            return finish("budget_final", step + 1)
                        error = validation_error
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                return finish("max_tool_calls", step + 1)

        return finish("max_steps", self.limits.max_steps)


__all__ = ["AgentExecutionResult", "ExecutionLimits", "Finalizer", "ToolCallingExecutor"]
