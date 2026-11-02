"""Review Agent 的角色提示词。"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from agent.agents.roles import AgentRoleConfig
from agent.langgraph.prompts import CODE_MARKER, NODE_MARKER


def _numbered(code: str) -> str:
    return "\n".join(f"{index}| {line}" for index, line in enumerate(code.splitlines(), 1))


def build_review_prompt(config: AgentRoleConfig, evidence: Dict[str, Any], code: str,
                        extra_context: str = "") -> Tuple[str, str]:
    system = f"""{NODE_MARKER} {config.node_name}
你是独立的 Review Agent。职责：{config.responsibility}。
你看不到其他 Agent 的结论。先阅读公共证据；只有证据不足时才调用工具补充取证，
最多调用 {config.max_tool_calls} 次。所有工具均为只读能力。
最终只输出 JSON：{{"issues":[...]}}。每条问题包含 category、severity、subtype、
line_start、line_end、risk_description、fix_suggestion、confidence、evidence、evidence_refs。
工具证据必须填写实际 tool 和 Observation 中的 evidence_id；源码证据使用 code_snippet
并填写原始 snippet。没有明确问题时输出 {{"issues":[]}}，不要输出 Markdown。

【公共证据】
{json.dumps(evidence, ensure_ascii=False, default=str)}
"""
    user = f"{CODE_MARKER}\n```python\n{_numbered(code)}\n```"
    if extra_context:
        user += f"\n\n补充上下文：{extra_context}"
    return system, user


__all__ = ["build_review_prompt"]
