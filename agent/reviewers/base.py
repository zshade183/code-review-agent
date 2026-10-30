"""领域 Reviewer 基类。

四个领域 Reviewer（Security / Bug / Quality / Performance）共享同一套骨架：

1. **独立消息上下文**：每个 Reviewer 一个 ``AgentRuntime``，互相看不到对方的推理与结论
2. **共享证据**：只读复用 ``EvidenceBundle``，不重复跑 pylint / bandit
3. **只读工具**：拿不到补丁、执行、测试工具，写能力只属于 Fix Agent
4. **类别收敛**：最终产出只能是本领域类别；越界结论在确定性后处理里被丢弃
5. **统一契约**：输出 ``FindingCandidate``，交给 Candidate Merger 规范化与去重
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.evidence.bundle import EvidenceBundle
from agent.llm import LLMClient
from agent.reviewer_agent import EvidencePolicy, _ReportFinalizer, output_schema_hint
from agent.runtime import AgentConfig, AgentRuntime, RunOutcome
from agent.schema import Category, FindingCandidate, Issue, ReviewReport, Severity
from agent.tools import build_reviewer_registry
from agent.tools.base import ToolRegistry
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)


#: 领域 Reviewer 可用的只读工具（与单 Agent 审查一致，不含任何写能力）
DOMAIN_REVIEWER_TOOL_NAMES = (
    "get_ast_outline",
    "find_symbol",
    "check_syntax",
    "search_codebase",
    "read_project_file",
    "find_symbol_in_project",
    "list_tools",
)


class DomainReviewer:
    """单一领域的审查角色。"""

    #: 稳定角色标识（进入 candidate.sources / trace role / 覆盖度维度名）
    role: str = ""
    #: 该角色只负责的一级分类
    category: Category = Category.BUG
    #: 给模型看的角色名
    display_name: str = ""
    #: 该维度的审查关注点（注入提示词）
    focus: str = ""
    #: 用于 auto 策略判断「这段代码是否值得启动该领域」的弱特征（正则，仅作启发式）
    triggers: Tuple[str, ...] = ()

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        codebase_root: Optional[str] = None,
    ) -> None:
        self.client = client or LLMClient()
        self.registry = registry or build_reviewer_registry(codebase_root)
        self.config = config or AgentConfig(max_steps=6, max_tool_calls=8)
        self.last_finalizer: Optional[_ReportFinalizer] = None

    # ------------------------------------------------------------------
    # 适配性判断（auto 策略使用）
    # ------------------------------------------------------------------

    def is_applicable(self, code: str, bundle: EvidenceBundle) -> bool:
        """该领域是否值得在本次审查中启动。"""
        if not bundle.syntax_ok and self.category is not Category.BUG:
            # 语法都不合法时，只有 Bug Reviewer（含语法错误本身）值得跑
            return False
        if bundle.findings_for(self.category):
            return True
        if not self.triggers:
            return True
        return any(trigger in code for trigger in self.triggers)

    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        return f"""
你是一名资深代码审查专家，担任**{self.display_name}（{self.role} reviewer）**角色。

【审查维度】{self.category.value}
你只负责这一个维度。其他维度的问题一律不要报告，即使你发现了也不要越界。

【本维度关注点】
{self.focus}

【工作方式】
1. 先阅读下方【共享取证结果】：它由 Evidence Collector 在审查开始时一次性生成，
   包含语法结论、代码结构和静态分析候选告警。候选告警**必然含误报**，请结合代码语义逐条判断。
2. 需要确认作用域或符号时，可调用只读工具（`get_ast_outline` / `find_symbol` /
   `check_syntax` / `search_codebase`）。不要重复执行整轮静态分析，避免浪费预算。
3. 只报告**能用证据支撑**、且属于【{self.category.value}】的问题。宁可少报，不要凑数。
4. 如果本维度没有任何可确认的问题，输出空 issues 数组就是正确结论。

【证据要求】
- 每条 issue 必须带 `evidence` 数组（至少 1 条），说明 kind/tool/line/detail。
- 没有证据的 issue 会在校验阶段被自动丢弃。
- `line_start` / `line_end` 必须真实存在于输入代码中。

{output_schema_hint()}
"""

    def build_user_prompt(
        self,
        code: str,
        bundle: EvidenceBundle,
        file_path: str = "",
        extra_context: str = "",
    ) -> str:
        lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        numbered = "\n".join(f"{index + 1}| {line}" for index, line in enumerate(lines))
        header = f"待审查文件：{file_path}\n" if file_path else ""
        extras = f"\n{extra_context}\n" if extra_context else ""
        return (
            f"{header}请只从【{self.category.value}】维度审查以下 Python 代码"
            "（左侧为行号，line_start/line_end 必须引用该行号）。\n\n"
            f"{bundle.prompt_block(category=self.category)}\n"
            f"{extras}"
            "【待审查代码】\n"
            f"```python\n{numbered}\n```\n\n"
            "请先基于共享证据判断，必要时用只读工具补充核实，最后输出 JSON 报告。"
        )

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def review(
        self,
        code: str,
        bundle: EvidenceBundle,
        file_path: str = "",
        extra_context: str = "",
        trace: Optional[TraceRecorder] = None,
    ) -> Tuple[List[FindingCandidate], RunOutcome]:
        """执行一次领域审查，返回 (候选问题, 运行轨迹)。"""
        recorder = trace or TraceRecorder(task=self.role or "reviewer")
        runtime = AgentRuntime(self.client, self.registry, self.config, recorder)
        holder: Dict[str, Any] = {}

        def finalizer(text: str) -> Tuple[Optional[ReviewReport], str]:
            impl = holder.get("impl")
            if impl is None:
                impl = _ReportFinalizer(
                    code=code,
                    policy=EvidencePolicy(),
                    # Evidence Collector 已经执行过的公共工具可以被领域结论引用，
                    # 不要求每个 Reviewer 再跑一遍相同静态分析。
                    tools_provider=lambda: [
                        *bundle.shared_tool_names(),
                        *runtime.trace.tool_names,
                    ],
                )
                holder["impl"] = impl
            report, error = impl(text)
            if error:
                return None, error
            assert report is not None
            scoped = [issue for issue in report.issues if issue.category is self.category]
            dropped = len(report.issues) - len(scoped)
            holder["out_of_scope_dropped"] = dropped
            return ReviewReport(review_summary=report.review_summary, issues=scoped), ""

        outcome = runtime.run(
            self.build_user_prompt(code, bundle, file_path, extra_context),
            system_prompt=self.system_prompt,
            finalizer=finalizer,
            task=self.role or "reviewer",
        )

        impl = holder.get("impl")
        self.last_finalizer = impl
        report = outcome.value if isinstance(outcome.value, ReviewReport) else None
        issues: List[Issue] = report.issues if report is not None else []

        evidence_refs = bundle.refs_for(self.category)[:8]
        candidates = [
            FindingCandidate(
                reviewer=self.role,
                issue=issue.model_copy(deep=True),
                evidence_refs=list(evidence_refs),
                notes=f"{self.display_name} 产出",
                raw_index=index,
            )
            for index, issue in enumerate(issues)
        ]

        outcome.run.output = dict(outcome.run.output or {})
        outcome.run.output.update(
            {
                "role": self.role,
                "category": self.category.value,
                "candidates": len(candidates),
                "out_of_scope_dropped": int(holder.get("out_of_scope_dropped") or 0),
                "dropped_issues": len(impl.dropped_issues) if impl is not None else 0,
            }
        )
        return candidates, outcome


def candidate_signature(candidate: FindingCandidate) -> str:
    """候选的稳定签名（角色 + 规范化字段），用于统计与去重调试。"""
    issue = candidate.issue
    return f"{candidate.reviewer}|{issue.category.value}|{issue.subtype}|{issue.line_start}"


def severity_rank(severity: Severity) -> int:
    """严重性排序权重（越大越严重）。"""
    return {
        Severity.HIGH: 4,
        Severity.MEDIUM_HIGH: 3,
        Severity.MEDIUM: 2,
        Severity.LOW: 1,
    }.get(severity, 0)


def summarize_candidates(candidates: Sequence[FindingCandidate]) -> Dict[str, int]:
    """按角色统计候选数量。"""
    counts: Dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.reviewer] = counts.get(candidate.reviewer, 0) + 1
    return counts
