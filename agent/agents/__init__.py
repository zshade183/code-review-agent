"""审查主线中的自治 Agent。"""

from agent.agents.roles import (
    AgentRoleConfig, DOMAIN_AGENT_CONFIGS, REVIEW_AGENT_CONFIGS,
    VERIFICATION_AGENT_CONFIG, FIX_AGENT_CONFIG, DomainAgentConfig,
)
from agent.agents.domain import DomainAgent, DomainAgentResult
from agent.agents.review import build_review_prompt
from agent.agents.verification import build_verification_prompt

__all__ = [
    "AgentRoleConfig", "DOMAIN_AGENT_CONFIGS", "REVIEW_AGENT_CONFIGS",
    "VERIFICATION_AGENT_CONFIG", "FIX_AGENT_CONFIG", "DomainAgentConfig", "DomainAgent", "DomainAgentResult",
    "build_review_prompt", "build_verification_prompt",
]
