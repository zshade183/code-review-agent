"""兼容旧导入；通用工具循环已迁移到 :mod:`agent.executors`。"""

from __future__ import annotations

import json
from typing import Any, Dict

from agent.agents.roles import AgentRoleConfig
from agent.evidence_service import EvidenceService
from agent.executors import AgentExecutionResult, ToolCallingExecutor
from agent.langgraph.prompts import CODE_MARKER, NODE_MARKER
from agent.tools.base import ToolRegistry


def _numbered(code: str) -> str:
    return "\n".join(f"{index}| {line}" for index, line in enumerate(code.splitlines(), 1))


class DomainAgent:
    """已弃用的角色包装器；新代码应直接使用 ``ToolCallingExecutor``。"""

    def __init__(self, llm: Any, registry: ToolRegistry, config: AgentRoleConfig,
                 evidence_service: EvidenceService | None = None) -> None:
        self.config = config
        self.executor = ToolCallingExecutor(llm, registry, config, evidence_service)

    def _system_prompt(self, evidence: Dict[str, Any]) -> str:
        return f"""{NODE_MARKER} {self.config.node_name}
你是独立的 {self.config.name} 代码审查 Agent。
职责：{self.config.responsibility}。
你看不到其他 Agent 的结论。先阅读公共证据；只有证据不足时才调用工具补充取证，
最多调用 {self.config.max_tool_calls} 次。
所有工具均为只读能力。不得声称调用了未实际调用的工具。
最终只输出 JSON：{{"issues":[...]}}。每条问题包含 category、severity、subtype、
line_start、line_end、risk_description、fix_suggestion、confidence、evidence、evidence_refs。
evidence 必须是对象数组，工具证据必须引用 Observation 中的 evidence_id；
源码证据使用 code_snippet 并填写 snippet。没有明确问题时输出 {{"issues":[]}}。

【公共证据】
{json.dumps(evidence, ensure_ascii=False, default=str)}
"""

    def run_with_prompt(self, code: str, system: str, user: str) -> AgentExecutionResult:
        return self.executor.run(code, system, user)

    def run(self, code: str, evidence: Dict[str, Any], extra_context: str = "") -> AgentExecutionResult:
        user = f"{CODE_MARKER}\n```python\n{_numbered(code)}\n```"
        if extra_context:
            user += f"\n\n补充上下文：{extra_context}"
        return self.executor.run(code, self._system_prompt(evidence), user)


DomainAgentResult = AgentExecutionResult

__all__ = ["DomainAgent", "DomainAgentResult"]
