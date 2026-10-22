"""多智能体协作：Reviewer / Critic / Verifier。

单 Agent 的自我批判（把「请自我反思」写进提示词）本质上还是在同一个上下文里做判断，
容易产生「自我确认偏差」——同一份推理既当运动员又当裁判。

这里拆成三个**上下文相互独立**的角色：

- **Reviewer**：取证并产出初版报告（已实现于 ``reviewer_agent.py``）
- **Critic**：反方律师。只拿到代码 + 初版结论，看不到 Reviewer 的工具轨迹，
  默认怀疑每一条，可用工具独立核实，对每条给出 keep / drop / downgrade
- **Verifier**：不依赖语言模型，改用**独立引擎佐证**——若 Reviewer 用规则引擎得出的结论，
  就用 pylint/bandit 去复核（反之亦然）。同一位置被两个独立引擎命中才算「有佐证」

合并规则刻意设计为「达成一致才降级处理」：

- Critic 判 drop **且** 无独立引擎佐证 → 丢弃（两方都不支持，判为误报）
- Critic 判 drop **但** 有独立引擎佐证 → 保留并标记分歧（避免单方误删真问题）
- Critic 判 downgrade → 降低置信度
- 其余 → 保留

这样既压制了误报，又不会因为某个角色一次武断判断就丢掉真实缺陷。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from agent.llm import LLMClient
from agent.reviewer_agent import EvidencePolicy, ReviewerAgent
from agent.runtime import AgentConfig, AgentRuntime, RunOutcome, extract_json_block
from agent.schema import Issue, ReviewReport
from agent.tools import build_reviewer_registry
from agent.tools.base import ToolRegistry
from agent.tools.static_analysis import run_bandit, run_pylint, run_rules
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)

CRITIC_SYSTEM_PROMPT = """
你是一名**对抗式代码审查评审员（Critic）**。你的职责不是再找新问题，而是**质疑已有的每一条结论**。

对报告中的每一条问题，请以「反方律师」的立场完成：

1. 该问题是否客观存在？还是审查方误解了业务语义？
2. 该问题是否真的可被触发？有没有防御性代码使它在实践中无法发生？
3. 该问题是否只是代码风格偏好，而非真实缺陷？
4. 该问题是否与同一份报告中的其他条目重复？

你可以调用工具来独立核实（例如用 `get_ast_outline` 看真实作用域、用 `run_static_analysis`
换一个引擎复核、用 `find_symbol` 确认变量是否真的未被使用）。**不要直接照搬原报告的理由**，
必须给出你自己的判断依据。

对每条问题给出三种裁定之一：
- `keep`：结论成立，保留
- `downgrade`：问题存在但被高估（例如严重性偏高、影响有限）
- `drop`：这是误报，应当删除

【输出格式】只输出 JSON，不要 markdown 代码块、不要解释文字：
{
  "verdicts": [
    {"index": 0, "verdict": "keep", "reason": "你的独立判断依据", "confidence": 0.8}
  ],
  "overall_comment": "整体评价（可选）"
}
`index` 必须是下方问题清单中方括号内的编号，每条都要给出裁定。
"""


class IssueVerdict(BaseModel):
    """Critic 对单条问题的裁定。"""

    model_config = ConfigDict(extra="ignore")

    index: int
    verdict: Literal["keep", "drop", "downgrade"] = "keep"
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CriticOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdicts: List[IssueVerdict] = Field(default_factory=list)
    overall_comment: str = ""


@dataclass
class Corroboration:
    """独立引擎佐证情况。"""

    index: int
    corroborated: bool
    engines: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class MultiAgentResult:
    """多智能体审查结果。"""

    report: ReviewReport
    draft: ReviewReport
    verdicts: List[IssueVerdict] = field(default_factory=list)
    corroborations: List[Corroboration] = field(default_factory=list)
    runs: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Verifier：独立引擎佐证（不依赖 LLM）
# ---------------------------------------------------------------------------


class VerifierAgent:
    """用与 Reviewer 不同的静态分析引擎对结论做交叉验证。"""

    #: 可用引擎 -> 执行函数
    ENGINES = {
        "rules": run_rules,
        "pylint": run_pylint,
        "bandit": run_bandit,
    }

    def __init__(self, line_tolerance: int = 2) -> None:
        self.line_tolerance = line_tolerance

    def verify(
        self,
        code: str,
        report: ReviewReport,
        reviewer_engines: Sequence[str] = ("rules",),
    ) -> List[Corroboration]:
        """对每条问题做独立佐证。

        优先选用 Reviewer **没有用到**的引擎，避免「用同一份证据验证自己」。
        """
        independent = [name for name in self.ENGINES if name not in set(reviewer_engines)]
        if not independent:
            independent = list(self.ENGINES)

        independent_issues: List[Tuple[str, Issue]] = []
        engine_errors: Dict[str, str] = {}
        for name in independent:
            try:
                for issue in self.ENGINES[name](code):
                    independent_issues.append((name, issue))
            except Exception as exc:  # noqa: BLE001 - 某个引擎不可用不应影响整体
                engine_errors[name] = str(exc)
                logger.info("佐证引擎 %s 执行失败: %s", name, exc)

        results: List[Corroboration] = []
        for index, issue in enumerate(report.issues):
            hits: Set[str] = set()
            for engine_name, candidate in independent_issues:
                if candidate.category != issue.category:
                    continue
                if abs(candidate.line_start - issue.line_start) <= self.line_tolerance:
                    hits.add(engine_name)
            if hits:
                results.append(
                    Corroboration(
                        index=index,
                        corroborated=True,
                        engines=sorted(hits),
                        note=f"独立引擎 {'/'.join(sorted(hits))} 在相近位置给出同类告警",
                    )
                )
            else:
                note = "独立引擎未在相近位置给出同类告警"
                if engine_errors:
                    note += f"（部分引擎不可用: {', '.join(engine_errors)}）"
                results.append(Corroboration(index=index, corroborated=False, engines=[], note=note))
        return results


# ---------------------------------------------------------------------------
# Critic：对抗式 LLM 评审
# ---------------------------------------------------------------------------


class CriticAgent:
    """对初版报告逐条质疑的 LLM 角色。"""

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

    def build_user_prompt(self, code: str, report: ReviewReport, corroborations: Sequence[Corroboration]) -> str:
        lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        numbered = "\n".join(f"{i + 1}| {line}" for i, line in enumerate(lines))

        corroboration_map = {c.index: c for c in corroborations}
        issue_lines: List[str] = []
        for index, issue in enumerate(report.issues):
            mark = corroboration_map.get(index)
            corroboration_text = (
                f"独立引擎佐证：{'有（' + '/'.join(mark.engines) + '）' if mark and mark.corroborated else '无'}"
                if mark
                else "独立引擎佐证：未知"
            )
            issue_lines.append(
                f"[{index}] 【{issue.category.value} / {issue.severity.value}】"
                f"行 {issue.line_start}-{issue.line_end} {issue.subtype}\n"
                f"    描述：{issue.risk_description}\n"
                f"    建议：{issue.fix_suggestion}\n"
                f"    {corroboration_text}"
            )

        return (
            "请评审以下代码审查报告中的每一条结论。\n\n"
            "【代码】（左侧为行号）\n"
            f"```python\n{numbered}\n```\n\n"
            "【待评审的问题清单】\n"
            + ("\n".join(issue_lines) if issue_lines else "（空）")
            + "\n\n请逐条给出裁定，并输出 JSON。"
        )

    def review(
        self,
        code: str,
        report: ReviewReport,
        corroborations: Sequence[Corroboration] = (),
        trace: Optional[TraceRecorder] = None,
    ) -> Tuple[CriticOutput, RunOutcome]:
        recorder = trace or TraceRecorder(task="critic")
        runtime = AgentRuntime(self.client, self.registry, self.config, recorder)
        total_issues = len(report.issues)

        def finalizer(text: str) -> Tuple[Optional[CriticOutput], str]:
            raw = extract_json_block(text)
            if not raw:
                return None, "未找到 JSON 对象，请只输出 JSON。"
            try:
                import json

                payload = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                return None, f"JSON 解析失败：{exc}"
            try:
                output = CriticOutput.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                return None, f"字段校验失败：{exc}"
            if total_issues and not output.verdicts:
                return None, "verdicts 为空，请对每一条问题给出裁定。"
            output.verdicts = [v for v in output.verdicts if 0 <= v.index < total_issues]
            return output, ""

        outcome = runtime.run(
            self.build_user_prompt(code, report, corroborations),
            system_prompt=CRITIC_SYSTEM_PROMPT,
            finalizer=finalizer,
            task="critic",
        )
        # 未产出的索引默认按 keep 处理（保守：不因评审失败而黑箱删项）
        output = outcome.value if isinstance(outcome.value, CriticOutput) else CriticOutput()
        covered = {v.index for v in output.verdicts}
        for index in range(total_issues):
            if index not in covered:
                output.verdicts.append(
                    IssueVerdict(index=index, verdict="keep", reason="评审未覆盖，保守保留", confidence=0.3)
                )
        return output, outcome


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


class MultiAgentReviewPipeline:
    """三智能体审查流水线。"""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        codebase_root: Optional[str] = None,
        reviewer_config: Optional[AgentConfig] = None,
        critic_config: Optional[AgentConfig] = None,
        policy: Optional[EvidencePolicy] = None,
        persist_traces: bool = True,
    ) -> None:
        self.client = client or LLMClient()
        self.persist_traces = persist_traces
        self.reviewer = ReviewerAgent(
            client=self.client,
            registry=build_reviewer_registry(codebase_root),
            config=reviewer_config or AgentConfig(max_steps=8),
            policy=policy or EvidencePolicy(),
        )
        self.verifier = VerifierAgent()
        self.critic = CriticAgent(
            client=self.client,
            registry=build_reviewer_registry(codebase_root),
            config=critic_config or AgentConfig(max_steps=6, max_tool_calls=8),
        )

    def review(
        self,
        code: str,
        file_path: Optional[str] = None,
        reviewer_engines: Sequence[str] = ("rules",),
        extra_context: str = "",
        on_event: Optional[Any] = None,
    ) -> MultiAgentResult:
        runs: Dict[str, RunOutcome] = {}

        # 1) Reviewer
        reviewer_trace = TraceRecorder(task="reviewer", on_event=on_event)
        draft, runs["reviewer"] = self.reviewer.review(
            code, file_path=file_path, extra_context=extra_context, trace=reviewer_trace
        )

        # 2) Verifier：独立引擎佐证（无 LLM 调用）
        corroborations = self.verifier.verify(code, draft, reviewer_engines=reviewer_engines)

        # 3) Critic：对抗式评审
        critic_trace = TraceRecorder(task="critic", on_event=on_event)
        critic_output, runs["critic"] = self.critic.review(code, draft, corroborations, trace=critic_trace)
        verdicts = critic_output.verdicts

        # 4) 合并
        final = self._merge(draft, verdicts, corroborations)

        if self.persist_traces:
            for name, outcome in runs.items():
                outcome.run.output = dict(outcome.run.output or {})
                outcome.run.output["role"] = name
            reviewer_trace.persist(runs["reviewer"].run)
            critic_trace.persist(runs["critic"].run)

        agree = sum(
            1
            for v in verdicts
            if v.verdict == "keep"
        )
        stats = {
            "draft_issues": len(draft.issues),
            "final_issues": len(final.issues),
            "dropped": len(draft.issues) - len(final.issues),
            "critic_keep": agree,
            "critic_drop": sum(1 for v in verdicts if v.verdict == "drop"),
            "critic_downgrade": sum(1 for v in verdicts if v.verdict == "downgrade"),
            "corroborated": sum(1 for c in corroborations if c.corroborated),
            "consensus_rate": round(
                sum(1 for v in verdicts if v.verdict == "keep") / len(verdicts), 4
            )
            if verdicts
            else 0.0,
        }
        final.review_summary = (
            f"{final.review_summary}｜多智能体：初版 {stats['draft_issues']} 条 → "
            f"终版 {stats['final_issues']} 条（Critic 判误报 {stats['critic_drop']} 条，"
            f"独立引擎佐证 {stats['corroborated']} 条）"
        )
        return MultiAgentResult(
            report=final,
            draft=draft,
            verdicts=verdicts,
            corroborations=corroborations,
            runs={name: outcome.run for name, outcome in runs.items()},
            stats=stats,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _merge(
        draft: ReviewReport,
        verdicts: Sequence[IssueVerdict],
        corroborations: Sequence[Corroboration],
    ) -> ReviewReport:
        verdict_map = {v.index: v for v in verdicts}
        corroboration_map = {c.index: c for c in corroborations}
        kept: List[Issue] = []

        for index, issue in enumerate(draft.issues):
            # 深拷贝：合并阶段会修改置信度与备注，不能污染初版报告（draft 会被单独返回）
            issue = issue.model_copy(deep=True)
            verdict = verdict_map.get(index)
            corroboration = corroboration_map.get(index)
            issue.produced_by = "reviewer+critic+verifier"

            if verdict is None:
                kept.append(issue)
                continue

            if verdict.verdict == "drop":
                if corroboration is not None and corroboration.corroborated:
                    # 一方判误报但有独立引擎佐证 -> 保留并记录分歧，避免单方误删真问题
                    issue.verify_note = (
                        f"Critic 判为误报（{verdict.reason}），但{collaboration_text(corroboration)}，"
                        "已保留并降低置信度，建议人工复核。"
                    )
                    issue.confidence = min(issue.confidence, 0.4)
                    issue.disagreement = True
                    kept.append(issue)
                continue

            if verdict.verdict == "downgrade":
                issue.confidence = round(max(0.0, issue.confidence - 0.3), 4)
                issue.verify_note = f"Critic 认为问题被高估：{verdict.reason}"
                kept.append(issue)
                continue

            if corroboration is not None:
                issue.verify_note = corroboration.note
            kept.append(issue)

        return ReviewReport(review_summary=draft.review_summary, issues=kept)


def collaboration_text(corroboration: Corroboration) -> str:
    return f"独立引擎 {'/'.join(corroboration.engines)} 给出同类告警" if corroboration.engines else "独立引擎无同类告警"
