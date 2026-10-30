"""兼容层：旧的三智能体流水线已废弃。

多 Agent 现在是**正式主线**，入口统一收敛到 :class:`agent.pipelines.ReviewPipeline`，
不再需要单独的 ``multi`` 模式。本模块保留旧类名与导出，避免既有脚本/测试直接报
``ImportError``，但所有逻辑都已经走新的确定性编排管线：

    from agent.pipelines import ReviewPipeline
    result = ReviewPipeline().review(code, strategy="full")
    result.report                       # 最终报告
    result.coverage                     # 覆盖度 / 部分成功
    result.stats["adjudication"]        # confirmed / needs_review / memory_suspected / rejected
"""

from __future__ import annotations

from agent.adjudication.critic import (
    CRITIC_SYSTEM_PROMPT,
    CriticAgent,
    CriticOutput,
    CriticPolicy,
    IssueVerdict,
)
from agent.adjudication.memory_policy import MemoryPolicy
from agent.adjudication.merger import CandidateMerger, CrossAgentConflict, MergeResult
from agent.adjudication.policy import AdjudicationResult, Adjudicator
from agent.evidence.verifier import Corroboration, VerificationResult, VerifierAgent, collaboration_text
from agent.pipelines.review import ReviewPipeline, ReviewPipelineResult

#: 旧类名：等价于 ReviewPipeline（默认策略为 auto）
MultiAgentReviewPipeline = ReviewPipeline

__all__ = [
    "MultiAgentReviewPipeline",
    "ReviewPipeline",
    "ReviewPipelineResult",
    "CRITIC_SYSTEM_PROMPT",
    "CriticAgent",
    "CriticOutput",
    "CriticPolicy",
    "IssueVerdict",
    "MemoryPolicy",
    "CandidateMerger",
    "CrossAgentConflict",
    "MergeResult",
    "AdjudicationResult",
    "Adjudicator",
    "Corroboration",
    "VerificationResult",
    "VerifierAgent",
    "collaboration_text",
]
