"""代码审查 Agent 包（多 Agent 主线）。

项目定位：**基于 LangGraph 状态图、领域专家协作和证据校验的多 Agent 代码审查系统。**

主线架构::

    基础上下文
    → Review Agent 自主取证
    → 条件式 Verification Agent → 确定性裁决
    → Final Report
    → 用户确认
    → Fix Agent（独立第二条管线）

分层：

- ``schema``        ：Pydantic 数据契约（Issue / FindingCandidate / 覆盖度 / 裁决状态）
- ``langgraph``     ：固定状态图、并行评审、证据校验与确定性裁决
- ``pipelines``     ：Review Pipeline 与 Fix Pipeline 两条正式入口
- ``fixing``        ：Fix Agent 与确定性交付 Harness
- ``executors``     ：Review / Verification / Fix 共用的工具调用执行器
- ``runtime``       ：旧执行 API 的兼容门面
- ``memory``        ：四层记忆（感知 / 短期 / 长期 / 实体）
- ``trace``         ：可观测性（单次 run 全链路轨迹 + token/成本统计）

所有正式入口都通过 ``pipelines.ReviewPipeline`` 进入 LangGraph。
"""

from agent.schema import (
    AdjudicationStatus,
    AgentRun,
    AgentStep,
    Category,
    CoverageReport,
    DimensionCoverage,
    Evidence,
    FindingCandidate,
    Issue,
    ReviewReport,
    ReviewStrategy,
    Severity,
    TokenUsage,
    ToolCall,
    ToolResult,
    normalize_strategy,
)
from agent.runtime import AgentConfig, AgentRuntime

__all__ = [
    "AdjudicationStatus",
    "AgentRun",
    "AgentStep",
    "AgentConfig",
    "AgentRuntime",
    "Category",
    "CoverageReport",
    "DimensionCoverage",
    "Evidence",
    "FindingCandidate",
    "Issue",
    "ReviewReport",
    "ReviewStrategy",
    "Severity",
    "TokenUsage",
    "ToolCall",
    "ToolResult",
    "normalize_strategy",
]
