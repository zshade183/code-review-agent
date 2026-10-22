"""代码审查 Agent 包。

设计目标：「LLM 自主选择工具 → 观察结果 → 迭代推理 → 自我验证」的 Agent 系统。

分层：
- schema    : Pydantic 数据契约（结构化输出、轨迹、证据）
- llm       : LLM Provider 抽象（tool calling / 流式 / 重试退避）
- tools     : 可被 LLM 调用的工具集合
- runtime   : 手写轻量 Agent Loop（ReAct），含步数/预算/重复动作控制
- *_agent   : 具体 Agent 组装（审查 / 优化）
- multi_agent: 多智能体协作编排
- memory    : 长期记忆（误报反馈与项目规范沉淀）
- trace     : 可观测性（单次 run 全链路轨迹 + token/成本统计）
"""

from agent.schema import (
    AgentRun,
    AgentStep,
    Category,
    Evidence,
    Issue,
    ReviewReport,
    Severity,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from agent.runtime import AgentConfig, AgentRuntime

__all__ = [
    "AgentRun",
    "AgentStep",
    "AgentConfig",
    "AgentRuntime",
    "Category",
    "Evidence",
    "Issue",
    "ReviewReport",
    "Severity",
    "TokenUsage",
    "ToolCall",
    "ToolResult",
]
