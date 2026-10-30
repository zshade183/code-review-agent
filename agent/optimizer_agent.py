"""兼容层：优化 Agent 已迁移到 ``agent/fixing/``。

保留本模块只为既有导入路径（CLI / API / 测试 / 实验脚本）不中断。
新代码请直接使用：::

    from agent.fixing import OptimizerAgent, verify_optimization
    from agent.pipelines import FixPipeline
"""

from __future__ import annotations

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
