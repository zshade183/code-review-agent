"""修复层：Fix Agent 与三级验证。

审查与修复是**两条独立管线**：

- 四个领域审查助手始终保持只读
- Fix Agent 只能生成候选代码；Harness 独占最终补丁、验证与失败回退
"""

from agent.fixing.agent import (
    OPTIMIZER_SYSTEM_PROMPT,
    OptimizerAgent,
    build_optimizer_prompt,
    extract_code,
)
from agent.fixing.verification import verify_optimization
from agent.fixing.harness import FixHarness, HarnessDecision

__all__ = [
    "OPTIMIZER_SYSTEM_PROMPT",
    "OptimizerAgent",
    "build_optimizer_prompt",
    "extract_code",
    "verify_optimization",
    "FixHarness",
    "HarnessDecision",
]
