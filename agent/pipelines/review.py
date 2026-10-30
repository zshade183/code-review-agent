"""Review Pipeline：多 Agent审查的正式入口。

这是项目唯一的主线审查入口，三种策略共享同一条确定性编排：

.. code-block:: text

    fast   单 Reviewer + 基础验证（编辑器实时检查）
    auto   按证据/代码特征选择领域 Reviewer（默认）
    full   四领域 Reviewer + 选择性 Critic + 独立 Verifier（PR / CI）

``ReviewPipeline`` 负责组装角色、预算与记忆，把具体流程交给
``ReviewOrchestrator``；结果用 ``ReviewPipelineResult`` 暴露给 CLI / API / Web / 插件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from agent import settings
from agent.evidence.bundle import EvidenceBundle
from agent.evidence.store import EvidenceStore
from agent.llm import LLMClient
from agent.memory import MemoryStore
from agent.orchestrator import (
    EventHook,
    OrchestrationResult,
    ReviewerBudget,
    ReviewOrchestrator,
)
from agent.runtime import AgentConfig
from agent.schema import AgentRun, Issue, ReviewReport, ReviewStrategy, normalize_strategy


@dataclass
class ReviewPipelineResult:
    """审查管线结果：报告 + 覆盖度 + 裁决 + 轨迹。"""

    orchestration: OrchestrationResult

    # ---------- 便捷透传 ----------

    @property
    def report(self) -> ReviewReport:
        return self.orchestration.report

    @property
    def strategy(self) -> ReviewStrategy:
        return self.orchestration.strategy

    @property
    def coverage(self) -> Any:
        return self.orchestration.coverage

    @property
    def stats(self) -> Dict[str, Any]:
        return self.orchestration.stats

    @property
    def runs(self) -> Dict[str, AgentRun]:
        return self.orchestration.runs

    @property
    def evidence(self) -> EvidenceBundle:
        return self.orchestration.evidence

    @property
    def drafts(self) -> Dict[str, List[Issue]]:
        return self.orchestration.drafts

    @property
    def rejected(self) -> List[Issue]:
        return self.orchestration.rejected

    @property
    def verdicts(self) -> List[Any]:
        return self.orchestration.verdicts

    @property
    def corroborations(self) -> List[Any]:
        return self.orchestration.corroborations

    @property
    def elapsed_ms(self) -> float:
        return self.orchestration.elapsed_ms

    # ---------- 序列化 ----------

    def to_dict(self, include_traces: bool = True) -> Dict[str, Any]:
        """给 API / CLI JSON 输出消费的结构（保持向后兼容的 ``report`` 字段）。"""
        orchestration = self.orchestration
        payload: Dict[str, Any] = {
            "report": self.report.to_legacy(),
            "strategy": self.strategy.value,
            "plan": orchestration.plan.to_dict(),
            "coverage": self.coverage.to_dict(),
            "stats": self.stats,
            "evidence": self.evidence.to_dict(),
            "draft": {
                role: [issue.to_legacy() for issue in issues]
                for role, issues in self.drafts.items()
            },
            "rejected": [issue.to_legacy() for issue in self.rejected],
            "verdicts": [verdict.model_dump() for verdict in self.verdicts],
            "corroborations": [item.to_dict() for item in self.corroborations],
        }
        if include_traces:
            payload["traces"] = {
                role: run.to_trace_dict() for role, run in self.runs.items()
            }
        payload["partial"] = bool(self.coverage.partial)
        payload["elapsed_ms"] = round(self.elapsed_ms, 2)
        return payload


class ReviewPipeline:
    """多 Agent 审查主入口。"""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        codebase_root: Optional[str] = None,
        memory: Optional[MemoryStore] = None,
        project: str = "default",
        persist_traces: bool = False,
        evidence_store: Optional[EvidenceStore] = None,
        reviewer_budgets: Optional[Dict[str, ReviewerBudget]] = None,
        critic_budget: Optional[ReviewerBudget] = None,
        default_budget: Optional[ReviewerBudget] = None,
        default_strategy: Optional[str] = None,
        collector_engines: Sequence[str] = ("rules",),
        use_memory: bool = True,
    ) -> None:
        self.default_strategy = normalize_strategy(
            default_strategy, default=settings.DEFAULT_STRATEGY
        )
        self.use_memory = use_memory
        self.orchestrator = ReviewOrchestrator(
            client=client,
            codebase_root=codebase_root,
            memory=memory,
            project=project,
            persist_traces=persist_traces,
            evidence_store=evidence_store,
            reviewer_budgets=reviewer_budgets,
            critic_budget=critic_budget,
            default_budget=default_budget,
            collector_engines=collector_engines,
            use_memory=use_memory,
        )

    # ------------------------------------------------------------------

    def review(
        self,
        code: str,
        file_path: str = "",
        strategy: Optional[str] = None,
        session_id: str = "",
        extra_context: str = "",
        on_event: Optional[EventHook] = None,
        use_memory: Optional[bool] = None,
    ) -> ReviewPipelineResult:
        """执行一次审查。

        ``strategy`` 支持 ``fast`` / ``auto`` / ``full``，并兼容历史 ``single`` / ``multi`` 写法。
        """
        result = self.orchestrator.review(
            code,
            file_path=file_path,
            strategy=strategy or self.default_strategy,
            session_id=session_id,
            extra_context=extra_context,
            on_event=on_event,
            use_memory=self.use_memory if use_memory is None else use_memory,
        )
        return ReviewPipelineResult(orchestration=result)

    # ------------------------------------------------------------------

    def evidence_stats(self) -> Dict[str, Any]:
        return self.orchestrator.evidence_store.stats()
