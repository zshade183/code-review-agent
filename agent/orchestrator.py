"""确定性编排器（Orchestrator）。

主线流程（固定拓扑，由 Python 决定，不由 LLM 决定）：

    Evidence Collector
    → 领域 Reviewer 并行审查
    → Candidate Merger（确定性）
    → Verifier / 选择性 Critic 并行
    → Deterministic Adjudicator
    → Memory Adjustment
    → Final Report

Orchestrator 本身**不分析代码、不产生审查结论**，只负责：创建任务状态、收集公共证据、
选择要启动的领域、并行调度、管理预算/超时/失败、汇总结果、调用最终裁决。

失败处理支持部分成功：某个 Reviewer 超时或报错时，其余领域的结果照常返回，
覆盖度里明确标记该维度 ``timeout`` / ``error``，而不是让整次审查失败。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent import settings
from agent.adjudication.critic import (
    CriticAgent,
    CriticOutput,
    CriticPolicy,
    CriticSelection,
    IssueVerdict,
)
from agent.adjudication.memory_policy import MemoryPolicy
from agent.adjudication.merger import CandidateMerger, MergeResult
from agent.adjudication.policy import AdjudicationResult, Adjudicator
from agent.evidence.bundle import EvidenceBundle
from agent.evidence.store import EvidenceStore
from agent.evidence.verifier import Corroboration, VerificationResult, VerifierAgent
from agent.llm import LLMClient
from agent.memory import MemoryStore
from agent.reviewer_agent import ReviewerAgent
from agent.reviewers import (
    REVIEWER_CLASSES,
    build_domain_reviewers,
    select_applicable_roles,
)
from agent.reviewers.base import DomainReviewer
from agent.runtime import AgentConfig
from agent.schema import (
    AdjudicationStatus,
    AgentRun,
    Category,
    CoverageReport,
    DimensionCoverage,
    FindingCandidate,
    Issue,
    ReviewReport,
    ReviewStrategy,
    Severity,
    normalize_strategy,
)
from agent.tools import build_reviewer_registry
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)

EventHook = Callable[[str, Any], None]


#: 角色 -> 中文名（用于报告摘要）
ROLE_LABELS = {
    "single_agent": "单 Agent",
    "security": "安全",
    "bug": "缺陷",
    "quality": "质量",
    "performance": "性能",
    "critic": "Critic",
    "verifier": "Verifier",
}


@dataclass
class ReviewerBudget:
    """单个角色的预算与失败策略。"""

    timeout_s: float = field(default_factory=lambda: settings.REVIEWER_TIMEOUT_S)
    retries: int = field(default_factory=lambda: settings.REVIEWER_RETRIES)
    config: Optional[AgentConfig] = None


@dataclass
class RoleRun:
    """一个角色的执行结果。"""

    role: str
    status: str = "skipped"  # completed / timeout / error / skipped
    detail: str = ""
    elapsed_ms: float = 0.0
    candidates: List[FindingCandidate] = field(default_factory=list)
    run: Optional[AgentRun] = None
    recorder: Optional[TraceRecorder] = None
    attempts: int = 0

    @property
    def issues(self) -> List[Issue]:
        return [candidate.issue for candidate in self.candidates]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "attempts": self.attempts,
            "candidates": len(self.candidates),
            "stop_reason": self.run.stop_reason if self.run is not None else "",
            "tokens": self.run.usage.total_tokens if self.run is not None else 0,
        }


@dataclass
class StrategyPlan:
    """一次审查的策略快照（确定性、可审计）。"""

    strategy: ReviewStrategy
    reviewer_roles: List[str] = field(default_factory=list)
    critic_enabled: bool = False
    verifier_enabled: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "reviewer_roles": list(self.reviewer_roles),
            "critic_enabled": self.critic_enabled,
            "verifier_enabled": self.verifier_enabled,
            "reason": self.reason,
        }


@dataclass
class OrchestrationResult:
    """一次多 Agent 审查的完整结果。"""

    strategy: ReviewStrategy
    report: ReviewReport
    evidence: EvidenceBundle
    coverage: CoverageReport
    plan: StrategyPlan
    merge: MergeResult
    drafts: Dict[str, List[Issue]] = field(default_factory=dict)
    candidates: List[FindingCandidate] = field(default_factory=list)
    role_runs: Dict[str, RoleRun] = field(default_factory=dict)
    adjudication: Optional[AdjudicationResult] = None
    critic_output: Optional[CriticOutput] = None
    critic_selection: Optional[CriticSelection] = None
    verification: Optional[VerificationResult] = None
    runs: Dict[str, AgentRun] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def rejected(self) -> List[Issue]:
        return list(self.adjudication.rejected) if self.adjudication else []

    @property
    def verdicts(self) -> List[IssueVerdict]:
        return list(self.critic_output.verdicts) if self.critic_output else []

    @property
    def corroborations(self) -> List[Corroboration]:
        if self.verification is None:
            return []
        return list(self.verification.corroborations.values())


class ReviewOrchestrator:
    """确定性多 Agent 编排器。"""

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
        collector_engines: Sequence[str] = ("rules",),
        merger: Optional[CandidateMerger] = None,
        adjudicator: Optional[Adjudicator] = None,
        critic: Optional[CriticAgent] = None,
        verifier: Optional[VerifierAgent] = None,
        use_memory: bool = True,
    ) -> None:
        self.client = client or LLMClient()
        self.codebase_root = codebase_root
        self.memory = memory
        self.project = project or "default"
        self.persist_traces = persist_traces
        self.evidence_store = evidence_store or EvidenceStore()
        self.default_budget = default_budget or ReviewerBudget()
        self.reviewer_budgets = reviewer_budgets or {}
        self.critic_budget = critic_budget or ReviewerBudget(
            timeout_s=settings.CRITIC_TIMEOUT_S, retries=0
        )
        self.collector_engines = tuple(collector_engines)
        self.merger = merger or CandidateMerger()
        self.adjudicator = adjudicator or Adjudicator()
        self._reviewer_registry = build_reviewer_registry(codebase_root)
        # 领域 Reviewer 需要移除公共取证已经执行过的 run_static_analysis。
        # 使用独立注册表，避免 subset() 重绑 list_tools 时影响 Critic 的工具视图。
        self._domain_registry = build_reviewer_registry(codebase_root)
        self.critic = critic or CriticAgent(
            client=self.client,
            registry=self._reviewer_registry,
            config=AgentConfig(
                max_steps=5,
                max_tool_calls=6,
                max_wall_clock_s=settings.CRITIC_TIMEOUT_S,
            ),
            codebase_root=codebase_root,
        )
        self.verifier = verifier or VerifierAgent()
        self.memory_policy = MemoryPolicy(memory, project=self.project, enabled=use_memory)
        self._critic_policy = CriticPolicy()

    # ------------------------------------------------------------------
    # 策略规划
    # ------------------------------------------------------------------

    def plan(self, code: str, bundle: EvidenceBundle, strategy: Any) -> StrategyPlan:
        normalized = normalize_strategy(strategy, default=settings.DEFAULT_STRATEGY)
        if normalized is ReviewStrategy.FAST:
            return StrategyPlan(
                strategy=normalized,
                reviewer_roles=[],
                critic_enabled=False,
                verifier_enabled=False,
                reason="fast：单 Reviewer + 基础验证，跳过领域拆分与对抗复核",
            )
        if normalized is ReviewStrategy.FULL:
            return StrategyPlan(
                strategy=normalized,
                reviewer_roles=list(REVIEWER_CLASSES),
                critic_enabled=True,
                verifier_enabled=True,
                reason="full：四领域 Reviewer + 选择性 Critic + 独立 Verifier",
            )
        roles = select_applicable_roles(code, bundle, max_reviewers=settings.AUTO_MAX_REVIEWERS)
        return StrategyPlan(
            strategy=ReviewStrategy.AUTO,
            reviewer_roles=roles,
            critic_enabled=True,
            verifier_enabled=True,
            reason=(
                "auto：按共享证据与代码特征选择领域（"
                + ("、".join(ROLE_LABELS.get(role, role) for role in roles) or "无")
                + "）"
            ),
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def review(
        self,
        code: str,
        file_path: str = "",
        strategy: Any = None,
        session_id: str = "",
        extra_context: str = "",
        on_event: Optional[EventHook] = None,
        use_memory: Optional[bool] = None,
    ) -> OrchestrationResult:
        started = time.time()
        normalized = normalize_strategy(strategy, default=settings.DEFAULT_STRATEGY)
        # 每次审查用独立的记忆策略对象，避免共享实例在并发请求间串改 enabled。
        effective_memory = self.memory if use_memory is None else (self.memory if use_memory else None)
        memory_policy = MemoryPolicy(
            effective_memory, project=self.project, enabled=effective_memory is not None
        )

        # 1) 公共取证：一次，供所有下游角色复用
        bundle = self.evidence_store.get_or_collect(
            code,
            file_path=file_path,
            project=self.project,
            engines=self.collector_engines,
            memory=effective_memory,
            session_id=session_id,
            codebase_root=self.codebase_root,
        )

        # 2) 策略规划（确定性）
        plan = self.plan(code, bundle, normalized)

        # 3) 领域 Reviewer（fast 走单 Agent 基线）
        if plan.strategy is ReviewStrategy.FAST:
            role_runs, candidates = self._run_fast(
                code, bundle, file_path, extra_context, on_event
            )
        else:
            role_runs, candidates = self._run_domain_reviewers(
                code,
                bundle,
                plan.reviewer_roles,
                file_path,
                extra_context,
                on_event,
                plan.strategy,
            )

        # 4) 确定性合并
        merge = self.merger.merge(candidates, total_lines=bundle.total_lines)

        # 5) 记忆召回信号（裁决输入，不直接删候选）
        memory_flags = memory_policy.recall_flags(code, merge.issues)

        # 6) Verifier 与选择性 Critic 并行
        (
            verification,
            critic_output,
            critic_selection,
            verifier_status,
            critic_status,
            critic_run,
            critic_recorder,
        ) = self._run_verification_and_critic(
            code=code,
            bundle=bundle,
            merge=merge,
            plan=plan,
            role_runs=role_runs,
            memory_flags=memory_flags,
        )

        # 7) 确定性裁决
        corroborations = verification.corroborations if verification is not None else {}
        verdicts = critic_output.verdict_map() if critic_output is not None else {}
        adjudication = self.adjudicator.adjudicate(
            merge.issues,
            verdicts=verdicts,
            corroborations=corroborations,
            memory_flags=memory_flags,
            conflict_ids=merge.conflict_ids,
            verification=verification,
        )

        # 8) 记忆调整（只降权/分组，不删除）
        adjudication = memory_policy.adjust(adjudication)

        # 9) 最终报告
        report = ReviewReport(
            review_summary=self._build_summary(plan, merge, adjudication, verification),
            issues=adjudication.issues,
        )
        memory_stats = memory_policy.record(
            report,
            code,
            file_path=file_path,
            session_id=session_id,
            issues=[*adjudication.issues, *adjudication.rejected],
        )

        # 10) 覆盖度与统计
        coverage = self._build_coverage(plan, role_runs, verifier_status, critic_status)
        runs = {
            role: role_run.run
            for role, role_run in role_runs.items()
            if role_run.run is not None
        }
        if critic_run is not None:
            runs["critic"] = critic_run
        if self._persist_enabled():
            self._persist_role_traces(role_runs)
            if critic_run is not None and critic_recorder is not None:
                critic_recorder.persist(critic_run)

        elapsed_ms = (time.time() - started) * 1000.0
        stats = self._build_stats(
            plan=plan,
            bundle=bundle,
            role_runs=role_runs,
            merge=merge,
            adjudication=adjudication,
            verification=verification,
            critic_output=critic_output,
            critic_selection=critic_selection,
            coverage=coverage,
            memory_stats=memory_stats,
            runs=runs,
            verifier_status=verifier_status,
            critic_status=critic_status,
            elapsed_ms=elapsed_ms,
        )
        return OrchestrationResult(
            strategy=plan.strategy,
            report=report,
            evidence=bundle,
            coverage=coverage,
            plan=plan,
            merge=merge,
            drafts={role: role_run.issues for role, role_run in role_runs.items()},
            candidates=candidates,
            role_runs=role_runs,
            adjudication=adjudication,
            critic_output=critic_output,
            critic_selection=critic_selection,
            verification=verification,
            runs=runs,
            stats=stats,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # fast 策略：单 Agent 基线
    # ------------------------------------------------------------------

    def _run_fast(
        self,
        code: str,
        bundle: EvidenceBundle,
        file_path: str,
        extra_context: str,
        on_event: Optional[EventHook],
    ) -> Tuple[Dict[str, RoleRun], List[FindingCandidate]]:
        role = "single_agent"
        budget = self.reviewer_budgets.get(role, self.default_budget)
        config = budget.config or AgentConfig(
            max_steps=8,
            max_tool_calls=10,
            max_wall_clock_s=budget.timeout_s,
        )
        reviewer = ReviewerAgent(
            client=self.client,
            registry=self._reviewer_registry,
            config=config,
            memory=None,  # 记忆统一由 MemoryPolicy 在裁决后处理
            project=self.project,
            apply_memory_suppression=False,
        )
        recorder = TraceRecorder(
            task=role, on_event=self._event_forwarder(role, on_event)
        )
        started = time.time()
        candidates: List[FindingCandidate] = []

        def run_reviewer() -> Tuple[ReviewReport, Any]:
            context = bundle.prompt_block(category=None)
            if extra_context:
                context = f"{context}\n\n{extra_context}"
            return reviewer.review(
                code,
                file_path=file_path,
                extra_context=context,
                trace=recorder,
                session_id="",
            )

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fast-review")
        future = executor.submit(run_reviewer)
        try:
            report, outcome = future.result(timeout=budget.timeout_s)
            candidates = [
                FindingCandidate(
                    reviewer=role,
                    issue=issue.model_copy(deep=True),
                    evidence_refs=bundle.refs_for(),
                    notes="fast 策略：单 Agent 审查",
                    raw_index=index,
                )
                for index, issue in enumerate(report.issues)
            ]
            if outcome.run.stop_reason == "final":
                status, detail = "completed", ""
            elif outcome.run.stop_reason == "timeout":
                status, detail = "timeout", "单 Agent 超出时间预算"
            else:
                status, detail = "error", f"stop_reason={outcome.run.stop_reason}"
            role_run = RoleRun(
                role=role,
                status=status,
                detail=detail,
                elapsed_ms=(time.time() - started) * 1000.0,
                candidates=candidates,
                run=outcome.run,
                recorder=recorder,
                attempts=1,
            )
        except TimeoutError:
            future.cancel()
            role_run = RoleRun(
                role=role,
                status="timeout",
                detail=f"超过 fast 策略时间预算 {budget.timeout_s:.0f}s",
                elapsed_ms=(time.time() - started) * 1000.0,
                recorder=recorder,
                attempts=1,
            )
            candidates = []
        except Exception as exc:  # noqa: BLE001 - 失败也要返回可用的空结果
            logger.exception("fast 策略执行失败")
            role_run = RoleRun(
                role=role,
                status="error",
                detail=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(time.time() - started) * 1000.0,
                recorder=recorder,
                attempts=1,
            )
            candidates = []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return {role: role_run}, list(candidates)

    # ------------------------------------------------------------------
    # auto / full：并行领域 Reviewer
    # ------------------------------------------------------------------

    def _run_domain_reviewers(
        self,
        code: str,
        bundle: EvidenceBundle,
        roles: Sequence[str],
        file_path: str,
        extra_context: str,
        on_event: Optional[EventHook],
        strategy: ReviewStrategy,
    ) -> Tuple[Dict[str, RoleRun], List[FindingCandidate]]:
        if not roles:
            return {}, []
        configs: Dict[str, AgentConfig] = {}
        for role in roles:
            budget = self.reviewer_budgets.get(role, self.default_budget)
            configs[role] = budget.config or AgentConfig(
                max_steps=6 if strategy is ReviewStrategy.AUTO else 5,
                max_tool_calls=8 if strategy is ReviewStrategy.AUTO else 6,
                max_wall_clock_s=budget.timeout_s,
            )
        reviewers = build_domain_reviewers(
            roles,
            client=self.client,
            configs=configs,
            registry=self._domain_registry,
        )
        role_runs: Dict[str, RoleRun] = {}
        futures: Dict[str, Future] = {}
        started = time.time()
        executor = ThreadPoolExecutor(max_workers=len(roles), thread_name_prefix="reviewer")
        try:
            for role in roles:
                futures[role] = executor.submit(
                    self._run_single_domain,
                    role,
                    reviewers[role],
                    code,
                    bundle,
                    file_path,
                    extra_context,
                    on_event,
                )
            # 每个角色按自己的预算独立判超时：轮询 pending，避免「等 A 超时却把已完成的 B 判成超时」
            pending: Dict[str, Future] = dict(futures)
            while pending:
                wait(list(pending.values()), timeout=0.1)
                elapsed_s = time.time() - started
                for role in list(pending):
                    budget = self.reviewer_budgets.get(role, self.default_budget)
                    future = pending[role]
                    if future.done():
                        try:
                            role_runs[role] = future.result()
                        except Exception as exc:  # noqa: BLE001
                            role_runs[role] = RoleRun(
                                role=role,
                                status="error",
                                detail=f"{type(exc).__name__}: {exc}",
                            )
                        pending.pop(role, None)
                    elif elapsed_s > budget.timeout_s:
                        future.cancel()
                        role_runs[role] = RoleRun(
                            role=role,
                            status="timeout",
                            detail=f"超过该角色时间预算 {budget.timeout_s:.0f}s，其余领域结果照常返回",
                            elapsed_ms=elapsed_s * 1000.0,
                        )
                        pending.pop(role, None)
                if not pending:
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        candidates: List[FindingCandidate] = []
        for role in roles:
            candidates.extend(role_runs[role].candidates)
        return role_runs, candidates

    def _run_single_domain(
        self,
        role: str,
        reviewer: DomainReviewer,
        code: str,
        bundle: EvidenceBundle,
        file_path: str,
        extra_context: str,
        on_event: Optional[EventHook],
    ) -> RoleRun:
        budget = self.reviewer_budgets.get(role, self.default_budget)
        attempts = max(1, budget.retries + 1)
        started = time.time()
        last_error = ""
        for attempt in range(1, attempts + 1):
            recorder = TraceRecorder(task=role, on_event=self._event_forwarder(role, on_event))
            try:
                candidates, outcome = reviewer.review(
                    code,
                    bundle,
                    file_path=file_path,
                    extra_context=extra_context,
                    trace=recorder,
                )
                if outcome.run.stop_reason == "final":
                    status, detail = "completed", ""
                elif outcome.run.stop_reason == "timeout":
                    status, detail = "timeout", "该角色超出时间预算"
                else:
                    status, detail = "error", f"stop_reason={outcome.run.stop_reason}"
                return RoleRun(
                    role=role,
                    status=status,
                    detail=detail,
                    elapsed_ms=(time.time() - started) * 1000.0,
                    candidates=candidates,
                    run=outcome.run,
                    recorder=recorder,
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001 - 单角色失败不应中断整体
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("领域 Reviewer %s 第 %d 次执行失败：%s", role, attempt, last_error)
        return RoleRun(
            role=role,
            status="error",
            detail=last_error,
            elapsed_ms=(time.time() - started) * 1000.0,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # Verifier + 选择性 Critic（并行）
    # ------------------------------------------------------------------

    def _run_verification_and_critic(
        self,
        code: str,
        bundle: EvidenceBundle,
        merge: MergeResult,
        plan: StrategyPlan,
        role_runs: Dict[str, RoleRun],
        memory_flags: Dict[str, str],
    ) -> Tuple[
        Optional[VerificationResult],
        Optional[CriticOutput],
        Optional[CriticSelection],
        str,
        str,
        Optional[AgentRun],
        Optional[TraceRecorder],
    ]:
        verification: Optional[VerificationResult] = None
        critic_output: Optional[CriticOutput] = None
        critic_selection: Optional[CriticSelection] = None
        verifier_status = "skipped"
        critic_status = "skipped"
        critic_run: Optional[AgentRun] = None
        critic_recorder: Optional[TraceRecorder] = None

        if not merge.issues:
            return (
                verification,
                critic_output,
                critic_selection,
                verifier_status,
                critic_status,
                critic_run,
                critic_recorder,
            )

        used_engines = self._collect_used_engines(bundle, role_runs)
        proxy_corroborations = self._proxy_corroborations(merge.issues, bundle)
        critic_selection = (
            self._critic_policy.select(
                merge.issues,
                corroborations=proxy_corroborations,
                conflict_ids=merge.conflict_ids,
                memory_flags=memory_flags,
            )
            if plan.critic_enabled
            else CriticSelection()
        )

        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="adjudicate")
        verify_future: Optional[Future] = None
        critic_future: Optional[Future] = None
        critic_trace = TraceRecorder(task="critic") if plan.critic_enabled else None
        try:
            if plan.verifier_enabled:
                verify_future = executor.submit(
                    self.verifier.verify_issues, merge.issues, bundle, tuple(used_engines)
                )
            if plan.critic_enabled and critic_selection.issues:
                critic_future = executor.submit(
                    self.critic.review,
                    code,
                    critic_selection.issues,
                    proxy_corroborations,
                    critic_selection,
                    critic_trace,
                )
            # 两个角色从同一时刻开始，但各自按自己的预算判超时，不能让较长的
            # Critic 预算顺带延长 Verifier 的截止时间。
            parallel_started = time.time()
            pending = [future for future in (verify_future, critic_future) if future is not None]
            while pending:
                wait(pending, timeout=0.1)
                elapsed = time.time() - parallel_started
                pending = [
                    future
                    for future in pending
                    if not future.done()
                    and not (
                        future is verify_future and elapsed > settings.VERIFIER_TIMEOUT_S
                    )
                    and not (
                        future is critic_future and elapsed > self.critic_budget.timeout_s
                    )
                ]
                if verify_future is not None and not verify_future.done() and elapsed > settings.VERIFIER_TIMEOUT_S:
                    verify_future.cancel()
                if critic_future is not None and not critic_future.done() and elapsed > self.critic_budget.timeout_s:
                    critic_future.cancel()

            if verify_future is not None:
                if verify_future.done():
                    try:
                        verification = verify_future.result()
                        verifier_status = "completed"
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("独立验证失败：%s", exc)
                        verification = VerificationResult(engine_errors={"verifier": str(exc)})
                        verifier_status = "error"
                else:
                    verify_future.cancel()
                    verification = VerificationResult(engine_errors={"verifier": "超时"})
                    verifier_status = "timeout"

            if critic_future is not None:
                if critic_future.done():
                    try:
                        critic_output, critic_outcome = critic_future.result()
                        critic_recorder = critic_trace
                        critic_run = (
                            critic_outcome.run if critic_outcome is not None else None
                        )
                        critic_status = "completed" if not critic_output.skipped else "skipped"
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Critic 执行失败：%s", exc)
                        critic_status = "error"
                else:
                    critic_future.cancel()
                    critic_status = "timeout"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return (
            verification,
            critic_output,
            critic_selection,
            verifier_status,
            critic_status,
            critic_run,
            critic_recorder,
        )

    @staticmethod
    def _proxy_corroborations(
        issues: Sequence[Issue], bundle: EvidenceBundle
    ) -> Dict[str, Corroboration]:
        """裁决前用于「选择性 Critic」的佐证代理：共享取证是否覆盖该候选。

        真正的独立佐证由 Verifier 并行计算，并用于最终裁决；这里只是为了让
        Critic 与 Verifier 能真正并行（否则必须先等完 Verifier 才能选候选）。
        """
        proxy: Dict[str, Corroboration] = {}
        for issue in issues:
            candidate_id = issue.ensure_candidate_id()
            hint = [
                finding
                for finding in bundle.findings_for(issue.category)
                if abs(finding.line_start - issue.line_start) <= 2
            ]
            proxy[candidate_id] = Corroboration(
                index=0,
                corroborated=bool(hint),
                engines=["shared_evidence"] if hint else [],
                note="共享取证命中（代理信号）" if hint else "共享取证未命中（代理信号）",
                candidate_id=candidate_id,
            )
        return proxy

    @staticmethod
    def _collect_used_engines(
        bundle: EvidenceBundle, role_runs: Dict[str, RoleRun]
    ) -> List[str]:
        """收集取证与各 Reviewer 实际用过的静态分析引擎（用于挑选独立验证引擎）。"""
        used = list(bundle.engines_used)
        for role_run in role_runs.values():
            run = role_run.run
            if run is None:
                continue
            for call in run.tool_calls:
                if call.name != "run_static_analysis":
                    continue
                raw = str(call.arguments.get("engines") or "")
                for engine in raw.split(","):
                    engine = engine.strip().lower()
                    if engine and engine not in used:
                        used.append(engine)
        return used

    # ------------------------------------------------------------------
    # 覆盖度 / 统计 / 摘要
    # ------------------------------------------------------------------

    def _build_coverage(
        self,
        plan: StrategyPlan,
        role_runs: Dict[str, RoleRun],
        verifier_status: str,
        critic_status: str,
    ) -> CoverageReport:
        coverage = CoverageReport(strategy=plan.strategy.value)
        for role in plan.reviewer_roles:
            role_run = role_runs.get(role)
            if role_run is None:
                continue
            coverage.mark(
                role,
                role_run.status,
                detail=role_run.detail,
                elapsed_ms=role_run.elapsed_ms,
                issues=len(role_run.candidates),
            )
        if plan.strategy is ReviewStrategy.FAST:
            role_run = role_runs.get("single_agent")
            if role_run is not None:
                coverage.mark(
                    "single_agent",
                    role_run.status,
                    detail=role_run.detail,
                    elapsed_ms=role_run.elapsed_ms,
                    issues=len(role_run.candidates),
                )
        return coverage

    def _build_stats(
        self,
        plan: StrategyPlan,
        bundle: EvidenceBundle,
        role_runs: Dict[str, RoleRun],
        merge: MergeResult,
        adjudication: AdjudicationResult,
        verification: Optional[VerificationResult],
        critic_output: Optional[CriticOutput],
        critic_selection: Optional[CriticSelection],
        coverage: CoverageReport,
        memory_stats: Dict[str, Any],
        runs: Dict[str, AgentRun],
        verifier_status: str,
        critic_status: str,
        elapsed_ms: float,
    ) -> Dict[str, Any]:
        total_tokens = sum(run.usage.total_tokens for run in runs.values())
        return {
            "strategy": plan.strategy.value,
            "plan": plan.to_dict(),
            "draft_candidates": sum(len(run.candidates) for run in role_runs.values()),
            "merged_candidates": len(merge.issues),
            "final_issues": len(adjudication.issues),
            "dropped_in_merge": len(merge.dropped),
            "conflicts": len(merge.conflicts),
            "adjudication": adjudication.counts(),
            "roles": {role: run.to_dict() for role, run in role_runs.items()},
            "verifier": verification.to_dict() if verification is not None else None,
            "verifier_status": verifier_status,
            "critic": {
                "status": critic_status,
                "reviewed": len(critic_selection.issues) if critic_selection is not None else 0,
                "skipped": bool(critic_output is None or critic_output.skipped),
                "keep": sum(1 for v in (critic_output.verdicts if critic_output else []) if v.verdict == "keep"),
                "drop": sum(1 for v in (critic_output.verdicts if critic_output else []) if v.verdict == "drop"),
                "downgrade": sum(
                    1 for v in (critic_output.verdicts if critic_output else []) if v.verdict == "downgrade"
                ),
            },
            "coverage": coverage.to_dict(),
            "partial": coverage.partial,
            "engines": list(bundle.engines_used),
            "engine_errors": dict(bundle.engine_errors),
            "memory": memory_stats,
            "tokens": total_tokens,
            "llm_runs": len(runs),
            "elapsed_ms": round(elapsed_ms, 2),
        }

    def _build_summary(
        self,
        plan: StrategyPlan,
        merge: MergeResult,
        adjudication: AdjudicationResult,
        verification: Optional[VerificationResult],
    ) -> str:
        counts = adjudication.counts()
        labels = "、".join(ROLE_LABELS.get(role, role) for role in plan.reviewer_roles) or "单 Agent"
        parts = [
            f"多 Agent（{plan.strategy.value}）：{labels} "
            f"{'并行完成' if plan.reviewer_roles else '完成'}；候选 {len(merge.issues)} 条 → "
            f"确认 {counts['confirmed']} / 待人工复核 {counts['needs_review']} / "
            f"记忆存疑 {counts['memory_suspected']} / 驳回 {counts['rejected']}"
        ]
        if verification is not None:
            parts.append(
                f"独立佐证 {verification.corroborated_count}/{len(verification.corroborations)} 条"
            )
            limitations = verification.limitations()
            if limitations:
                parts.append(f"能力限制：{limitations}")
        if len(merge.conflicts):
            parts.append(f"跨角色冲突 {len(merge.conflicts)} 处")
        if plan.strategy is ReviewStrategy.FAST:
            parts.append("fast 策略未启用领域拆分与对抗复核")
        return "；".join(parts)

    # ------------------------------------------------------------------

    def _event_forwarder(
        self, role: str, on_event: Optional[EventHook]
    ) -> Optional[Callable[[Any], None]]:
        if on_event is None:
            return None

        def forward(step: Any) -> None:
            try:
                on_event(role, step)
            except Exception:  # noqa: BLE001 - 观测失败不应影响审查
                pass

        return forward

    def _persist_enabled(self) -> bool:
        return bool(self.persist_traces)

    def _persist_role_traces(self, role_runs: Dict[str, RoleRun]) -> None:
        for role_run in role_runs.values():
            if role_run.run is None or role_run.recorder is None:
                continue
            role_run.run.output = dict(role_run.run.output or {})
            role_run.run.output["role"] = role_run.role
            role_run.recorder.persist(role_run.run)
