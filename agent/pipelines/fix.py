"""修复管线：用户选择问题 → Fix Agent → 语法/接口/测试验证。

与 Review Pipeline 严格分离：

- 审查阶段（Reviewers / Critic / Verifier）只读，不产生任何代码改动
- 修复阶段由用户确认要修哪些候选（``candidate_ids``）后才启动
- 修复结果必须通过 harness 的三级验证；不通过则回退原代码

这样才能避免「审查角色顺手改代码」带来的不可审计性，也让 Fix Agent 成为唯一
拥有补丁 / 执行 / 测试工具的角色。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from agent.fixing.agent import OptimizerAgent
from agent.llm import LLMClient
from agent.runtime import AgentConfig, RunOutcome
from agent.schema import (
    AdjudicationStatus,
    Issue,
    OptimizationResult,
    ReviewReport,
    VerificationReport,
)
from agent.trace import TraceRecorder


def issues_from_report(report: Any) -> List[Issue]:
    """从 ReviewReport / 字典 / Issue 列表中提取 Issue。"""
    if report is None:
        return []
    if isinstance(report, ReviewReport):
        return list(report.issues)
    if isinstance(report, Issue):
        return [report]
    if isinstance(report, dict):
        raw = report.get("issues")
        if isinstance(raw, list):
            parsed: List[Issue] = []
            for item in raw:
                try:
                    parsed.append(item if isinstance(item, Issue) else Issue.model_validate(item))
                except Exception:  # noqa: BLE001 - 非法条目跳过
                    continue
            return parsed
    if isinstance(report, (list, tuple)):
        return [item for item in report if isinstance(item, Issue)]
    return []


@dataclass
class FixPlan:
    """用户确认后的修复计划。"""

    code: str
    issues: List[Issue] = field(default_factory=list)
    candidate_ids: List[str] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    include_needs_review: bool = True
    include_memory_suspected: bool = False

    @property
    def empty(self) -> bool:
        return not self.issues

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_ids": list(self.candidate_ids),
            "selected": [issue.to_legacy() for issue in self.issues],
            "excluded": list(self.excluded),
            "include_needs_review": self.include_needs_review,
            "include_memory_suspected": self.include_memory_suspected,
            "empty": self.empty,
        }


class FixPipeline:
    """Fix Agent 的独立管线入口。"""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        codebase_root: Optional[str] = None,
        optimizer: Optional[OptimizerAgent] = None,
        config: Optional[AgentConfig] = None,
        max_verify_retries: int = 2,
    ) -> None:
        self.optimizer = optimizer or OptimizerAgent(
            client=client,
            config=config or AgentConfig(max_steps=6),
            codebase_root=codebase_root,
            max_verify_retries=max_verify_retries,
        )

    # ------------------------------------------------------------------
    # 1) 选择要修的问题（用户确认）
    # ------------------------------------------------------------------

    def plan(
        self,
        code: str,
        report: Any = None,
        candidate_ids: Optional[Sequence[str]] = None,
        include_needs_review: bool = True,
        include_memory_suspected: bool = False,
    ) -> FixPlan:
        issues = issues_from_report(report)
        plan = FixPlan(
            code=code,
            include_needs_review=include_needs_review,
            include_memory_suspected=include_memory_suspected,
        )
        wanted = {str(item).strip() for item in (candidate_ids or []) if str(item).strip()}

        for issue in issues:
            candidate_id = issue.ensure_candidate_id()
            if issue.adjudication is AdjudicationStatus.REJECTED:
                plan.excluded.append(
                    {"candidate_id": candidate_id, "reason": "裁决为驳回，不进入修复"}
                )
                continue
            if wanted:
                if candidate_id not in wanted:
                    plan.excluded.append(
                        {"candidate_id": candidate_id, "reason": "未被用户选中"}
                    )
                    continue
            else:
                status = issue.adjudication
                if status is AdjudicationStatus.MEMORY_SUSPECTED and not include_memory_suspected:
                    plan.excluded.append(
                        {"candidate_id": candidate_id, "reason": "记忆存疑，默认不自动修复"}
                    )
                    continue
                if status is AdjudicationStatus.NEEDS_REVIEW and not include_needs_review:
                    plan.excluded.append(
                        {"candidate_id": candidate_id, "reason": "待人工复核，未选择修复"}
                    )
                    continue
            plan.issues.append(issue)
            plan.candidate_ids.append(candidate_id)

        for missing in sorted(wanted - set(plan.candidate_ids)):
            plan.excluded.append({"candidate_id": missing, "reason": "候选不存在或已进入驳回"})
        return plan

    # ------------------------------------------------------------------
    # 2) 执行修复（含三级验证）
    # ------------------------------------------------------------------

    def fix(
        self,
        code: str,
        plan: FixPlan,
        test_code: str = "",
        trace: Optional[TraceRecorder] = None,
    ) -> "tuple[OptimizationResult, RunOutcome]":
        recorder = trace or TraceRecorder(task="fix")
        if plan.empty:
            run = recorder.build(output={"selected": 0, "reason": "empty-plan"})
            result = OptimizationResult(
                original_code=code,
                optimized_code=code,
                changed=False,
                verification=VerificationReport(),
                summary="没有选中任何可修复问题，Fix Agent 未启动（审查与修复是两条独立管线）。",
                used_fallback=True,
            )
            return result, RunOutcome(run=run, value=result)

        report = ReviewReport(
            review_summary=f"修复选中的 {len(plan.issues)} 个问题",
            issues=plan.issues,
        )
        result, outcome = self.optimizer.optimize(
            code, report, test_code=test_code, trace=recorder
        )
        outcome.run.output = dict(outcome.run.output or {})
        outcome.run.output.update(
            {
                "selected": len(plan.issues),
                "candidate_ids": list(plan.candidate_ids),
                "verification_passed": result.verification.passed,
                "used_fallback": result.used_fallback,
            }
        )
        return result, outcome


__all__ = ["FixPlan", "FixPipeline", "issues_from_report"]
