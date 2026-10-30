"""代码审查 Agent 包（多 Agent 主线）。

项目定位：**基于共享证据、领域专家协作、独立验证和确定性裁决的多 Agent 代码审查系统。**

主线架构（确定性拓扑，Python 决定流程而不是 LLM）::

    Evidence Collector
    → 领域 Reviewer 并行审查（Security / Bug / Quality / Performance）
    → Candidate Merger（确定性合并）
    → Critic / Verifier 并行复核
    → Deterministic Adjudicator
    → Memory Adjustment
    → Final Report
    → 用户确认
    → Fix Agent（独立第二条管线）

分层：

- ``schema``        ：Pydantic 数据契约（Issue / FindingCandidate / 覆盖度 / 裁决状态）
- ``evidence``      ：一次性公共取证（collector / store / verifier）
- ``reviewers``     ：四个领域 Reviewer 与动态选择
- ``adjudication``  ：确定性合并、选择性 Critic、规则裁决、记忆策略
- ``orchestrator``  ：确定性编排（并行调度 / 预算 / 超时 / 部分成功）
- ``pipelines``     ：Review Pipeline 与 Fix Pipeline 两条正式入口
- ``fixing``        ：Fix Agent 与三级验证（唯一拥有写/执行能力的地方）
- ``runtime``       ：手写轻量 ReAct Agent Loop
- ``memory``        ：四层记忆（感知 / 短期 / 长期 / 实体）
- ``trace``         ：可观测性（单次 run 全链路轨迹 + token/成本统计）

单 Agent 保留为 ``fast`` 策略（``ReviewerAgent``）与实验基线，不再是默认入口。
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
