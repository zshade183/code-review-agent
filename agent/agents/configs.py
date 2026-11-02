"""兼容旧导入；角色定义已迁移到 :mod:`agent.agents.roles`。"""

from agent.agents.roles import (
    AgentRoleConfig,
    DOMAIN_AGENT_CONFIGS,
    FIX_AGENT_CONFIG,
    REVIEW_AGENT_CONFIGS,
    VERIFICATION_AGENT_CONFIG,
    DomainAgentConfig,
)

__all__ = [
    "AgentRoleConfig", "DomainAgentConfig", "REVIEW_AGENT_CONFIGS",
    "VERIFICATION_AGENT_CONFIG", "FIX_AGENT_CONFIG", "DOMAIN_AGENT_CONFIGS",
]
