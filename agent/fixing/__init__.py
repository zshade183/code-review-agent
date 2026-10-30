"""修复层：Fix Agent 与三级验证。

审查与修复是**两条独立管线**：

- 领域 Reviewer / Critic / Verifier 始终保持只读
- 只有 Fix Agent 能获得补丁工具、代码执行工具与测试工具
"""

from agent.fixing.agent import (
    OPTIMIZER_SYSTEM_PROMPT,
    OptimizerAgent,
    build_optimizer_prompt,
    extract_code,
)
from agent.fixing.verification import verify_optimization

__all__ = [
    "OPTIMIZER_SYSTEM_PROMPT",
    "OptimizerAgent",
    "build_optimizer_prompt",
    "extract_code",
    "verify_optimization",
]
