"""Review 与 Verification 角色的权限和预算定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from agent import settings


@dataclass(frozen=True)
class AgentRoleConfig:
    name: str
    node_name: str
    responsibility: str
    tool_names: Tuple[str, ...]
    max_steps: int
    max_tool_calls: int


_REVIEW_DISCOVERY_TOOLS = (
    "run_static_analysis", "get_ast_outline", "find_symbol",
    "search_codebase", "read_project_file",
)

_VERIFICATION_CONTEXT_TOOLS = (
    "check_syntax", "get_ast_outline", "find_symbol",
    "search_codebase", "read_project_file", "find_symbol_in_project",
)

REVIEW_AGENT_CONFIGS: Dict[str, AgentRoleConfig] = {
    "fast": AgentRoleConfig(
        name="review", node_name="review_agent",
        responsibility="综合检查安全、缺陷、质量和性能问题，只报告证据明确的候选",
        tool_names=_REVIEW_DISCOVERY_TOOLS,
        max_steps=settings.REVIEW_FAST_MAX_STEPS,
        max_tool_calls=settings.REVIEW_FAST_MAX_TOOL_CALLS,
    ),
    "auto": AgentRoleConfig(
        name="review", node_name="review_agent",
        responsibility="综合检查安全、缺陷、质量和性能问题，并按需补充项目上下文",
        tool_names=_REVIEW_DISCOVERY_TOOLS,
        max_steps=settings.REVIEW_AUTO_MAX_STEPS,
        max_tool_calls=settings.REVIEW_AUTO_MAX_TOOL_CALLS,
    ),
    "full": AgentRoleConfig(
        name="review", node_name="review_agent",
        responsibility="完整检查安全、缺陷、质量和性能问题，优先补齐高风险候选的证据",
        tool_names=_REVIEW_DISCOVERY_TOOLS,
        max_steps=settings.REVIEW_FULL_MAX_STEPS,
        max_tool_calls=settings.REVIEW_FULL_MAX_TOOL_CALLS,
    ),
}

VERIFICATION_AGENT_CONFIG = AgentRoleConfig(
    name="verification", node_name="verification_agent",
    responsibility="逐条检查候选成立条件、主动寻找反证并指出缺失上下文",
    tool_names=_VERIFICATION_CONTEXT_TOOLS,
    max_steps=settings.VERIFICATION_MAX_STEPS,
    max_tool_calls=settings.VERIFICATION_MAX_TOOL_CALLS,
)

FIX_AGENT_CONFIG = AgentRoleConfig(
    name="fix", node_name="fix_agent",
    responsibility="仅修复用户选中的候选，并输出完整的最小修改代码",
    tool_names=(
        "get_ast_outline", "find_symbol", "check_syntax", "check_signature",
        "execute_python_code", "run_tests", "scan_dangerous_code",
        "search_codebase", "read_project_file", "find_symbol_in_project",
    ),
    max_steps=settings.MAX_STEPS,
    max_tool_calls=settings.MAX_TOOL_CALLS,
)

# 兼容旧名称。
DOMAIN_AGENT_CONFIGS = REVIEW_AGENT_CONFIGS
DomainAgentConfig = AgentRoleConfig

__all__ = [
    "AgentRoleConfig", "DomainAgentConfig", "REVIEW_AGENT_CONFIGS",
    "VERIFICATION_AGENT_CONFIG", "FIX_AGENT_CONFIG", "DOMAIN_AGENT_CONFIGS",
]
