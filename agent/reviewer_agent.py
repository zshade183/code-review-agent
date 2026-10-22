"""审查 Agent。

审查过程由模型自主决定调用哪些工具、调几次，并且**每条结论都必须挂上证据**。

证据闭环分两层：
1. 提示词层：明确告知模型「无证据的结论会被丢弃」
2. 校验层（``EvidencePolicy``）：最终报告逐条核验——声明的工具是否真的调过、
   行号是否真的存在、二级分类是否在允许集合内

第二层是关键。它把「不要幻觉」从一句提示词变成了**可执行的约束**，同时也是可量化
的指标：被丢弃的问题条数 = 一次审查中被拦下的无依据指控。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from agent.llm import LLMClient
from agent.memory import MemoryStore
from agent.runtime import AgentConfig, AgentRuntime, RunOutcome, extract_json_block
from agent.schema import Issue, ReviewReport, SUBTYPE_WHITELIST
from agent.tools import build_reviewer_registry
from agent.tools.base import ToolRegistry
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)

try:
    from core.prompts import REVIEW_RULES, REVIEWER_ROLE
except Exception:  # pragma: no cover - 兼容独立运行
    REVIEWER_ROLE = "你是一名资深代码审查专家。"
    REVIEW_RULES = ""


REVIEWER_SYSTEM_PROMPT = f"""
{REVIEWER_ROLE}

你是一个**代码审查 Agent**，不是一次性的文本生成器。你可以调用工具来获取客观证据，
并需要基于证据迭代推理。工作方式：

1. 【取证】首先调用 `run_static_analysis` 获取静态分析告警。这些告警**必然包含误报**
   （例如对已有边界检查的代码仍报 Index Out of Bounds），你必须逐条结合代码语义判断
   哪些真实成立、哪些应当排除。
2. 【补充】如需要确认问题所在函数的作用域、或某个变量是否真的未被使用，调用
   `get_ast_outline` / `find_symbol`。若项目代码库可用，可用 `search_codebase` 查看
   项目内同类写法，避免脱离上下文的空泛建议。
3. 【收敛】只报告你**能用证据支撑**的问题。宁可少报，也不要为了凑数而猜测。
4. 【输出】给出严格 JSON 格式的最终报告。

【硬性约束】
- 每个 issue 必须携带 `evidence` 数组，至少 1 条，且必须说明证据来源：
  - `kind`: tool_output / ast_node / code_snippet / rule / test_result
  - `tool`: 产出该证据的工具名（若证据来自工具）
  - `line`: 证据所在行号
  - `detail`: 该证据如何支撑你的结论
- **没有任何证据的 issue 会在校验阶段被自动丢弃**，不要浪费时间编造。
- 禁止报告「未做参数 None 校验」这类无外部输入证据的假设性问题。
- `line_start` / `line_end` 必须真实存在于输入代码中（行号见代码左侧）。
- `subtype` 必须来自下方允许集合，不要自创。

{REVIEW_RULES}
"""

OUTPUT_SCHEMA_HINT = """
【最终输出格式】只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字：

{
  "review_summary": "一句话总结本次审查结论（含发现数量）",
  "issues": [
    {
      "category": "Security Vulnerability | Potential Bug | Code Smell | Performance Issue",
      "severity": "High | Medium-High | Medium | Low",
      "subtype": "必须来自允许集合",
      "line_start": 12,
      "line_end": 12,
      "violated_rule": "违反的规则名",
      "risk_description": "问题描述（引用具体代码，单行文本）",
      "fix_suggestion": "可落地的修复建议（单行文本）",
      "confidence": 0.85,
      "evidence": [
        {"kind": "tool_output", "tool": "run_static_analysis", "line": 12,
         "snippet": "工具原始输出片段", "detail": "该证据如何支撑结论"}
      ]
    }
  ]
}

允许的二级分类（subtype）：
{_SUBTYPE_TEXT}
""".replace(
    "{_SUBTYPE_TEXT}",
    "\n".join(
        f"- {category.value}: " + " / ".join(subtypes)
        for category, subtypes in SUBTYPE_WHITELIST.items()
    ),
)


class EvidencePolicy:
    """证据校验策略。"""

    def __init__(
        self,
        require_evidence: bool = True,
        drop_unverified: bool = True,
        min_confidence_without_tool: float = 0.5,
    ) -> None:
        self.require_evidence = require_evidence
        self.drop_unverified = drop_unverified
        self.min_confidence_without_tool = min_confidence_without_tool


class _ReportFinalizer:
    """把模型文本转成 ReviewReport，并执行证据校验。

    这是一个**有状态**的 finalizer：第一次校验失败时返回错误让 runtime 重试，
    之后转为「降级接受 + 标记」，避免无限循环。
    """

    def __init__(
        self,
        code: str,
        policy: EvidencePolicy,
        tools_provider: Callable[[], Sequence[str]],
        allow_retry: bool = True,
    ) -> None:
        self.code = code
        self.total_lines = max(1, len(code.split("\n")))
        self.policy = policy
        self.tools_provider = tools_provider
        self.tools_called: Set[str] = set()
        self.allow_retry = allow_retry
        self.attempt = 0
        self.dropped_issues: List[Dict[str, Any]] = []
        self.flagged_issues: List[Dict[str, Any]] = []
        self.report: Optional[ReviewReport] = None

    def __call__(self, text: str) -> Tuple[Optional[ReviewReport], str]:
        self.attempt += 1
        # 每轮都重新读取真实的工具调用集合：模型可能在上一轮校验失败后补充调用了工具
        self.tools_called = set(self.tools_provider())
        raw = extract_json_block(text)
        if not raw:
            return None, "未在输出中找到 JSON 对象。请只输出一个 JSON 对象。"

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"

        if not isinstance(payload, dict):
            return None, "顶层必须是 JSON 对象"

        try:
            report = ReviewReport.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            return None, f"字段校验失败：{exc}"

        # ---- 证据校验 ----
        missing_evidence = [i for i in report.issues if self.policy.require_evidence and not i.evidence]
        if missing_evidence and self.allow_retry and self.attempt == 1:
            preview = "; ".join(
                f"{i.subtype}@行{i.line_start}" for i in missing_evidence[:5]
            )
            return None, (
                f"以下 {len(missing_evidence)} 条问题缺少 evidence 字段：{preview}。"
                "请为每条问题补充证据（说明来自哪个工具、行号、以及证据如何支撑结论）；"
                "若某条问题确实无法给出证据，请直接删除该条。"
            )

        kept = self._verify_and_filter(report)
        kept.review_summary = self._augment_summary(kept)
        self.report = kept
        return kept, ""

    # ------------------------------------------------------------------

    def _verify_and_filter(self, report: ReviewReport) -> ReviewReport:
        kept: List[Issue] = []
        for issue in report.issues:
            # 行号越界 → 修正或丢弃
            if issue.line_start > self.total_lines:
                self.dropped_issues.append(
                    {"reason": "行号越界", "issue": issue.model_dump(mode="json")}
                )
                continue
            issue.line_end = max(issue.line_start, min(issue.line_end, self.total_lines))

            if not issue.evidence:
                if self.policy.drop_unverified:
                    self.dropped_issues.append(
                        {"reason": "缺少证据", "issue": issue.model_dump(mode="json")}
                    )
                    continue
                issue.verified = False
                issue.verify_note = "缺少证据"
                issue.confidence = min(issue.confidence, 0.3)
                self.flagged_issues.append(
                    {"reason": "缺少证据", "subtype": issue.subtype, "line": issue.line_start}
                )
                kept.append(issue)
                continue

            notes: List[str] = []
            verified = True
            for evidence in issue.evidence:
                if evidence.tool and evidence.tool not in self.tools_called:
                    verified = False
                    notes.append(f"声明的工具 {evidence.tool} 本轮未被调用")
                if evidence.line is not None and not (1 <= evidence.line <= self.total_lines):
                    verified = False
                    notes.append(f"证据行号 {evidence.line} 超出代码范围")
                if not evidence.is_verifiable():
                    verified = False
                    notes.append("证据既无工具来源也无代码片段")

            if not issue.is_subtype_valid():
                notes.append(f"subtype '{issue.subtype}' 不在允许集合内")

            issue.verified = verified and not notes
            issue.verify_note = "；".join(notes)
            if not issue.verified:
                issue.confidence = min(issue.confidence, self.policy.min_confidence_without_tool)
                self.flagged_issues.append(
                    {"reason": issue.verify_note, "subtype": issue.subtype, "line": issue.line_start}
                )
            kept.append(issue)
        return ReviewReport(review_summary=report.review_summary, issues=kept)

    def _augment_summary(self, kept: ReviewReport) -> str:
        parts = [kept.review_summary.strip() or f"共发现 {len(kept.issues)} 个问题"]
        if self.dropped_issues:
            parts.append(
                f"（校验阶段移除了 {len(self.dropped_issues)} 条缺乏可核验证据的问题）"
            )
        if self.flagged_issues:
            parts.append(f"（{len(self.flagged_issues)} 条问题证据不足，已降低置信度）")
        return " ".join(parts)


class ReviewerAgent:
    """代码审查 Agent。"""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        policy: Optional[EvidencePolicy] = None,
        codebase_root: Optional[str] = None,
        memory: Optional[MemoryStore] = None,
        project: str = "default",
    ) -> None:
        self.client = client or LLMClient()
        self.registry = registry or build_reviewer_registry(codebase_root)
        self.config = config or AgentConfig()
        self.policy = policy or EvidencePolicy()
        self.memory = memory
        self.project = project
        self.last_finalizer: Optional[_ReportFinalizer] = None

    # ------------------------------------------------------------------

    def build_user_prompt(self, code: str, file_path: Optional[str] = None, extra_context: str = "") -> str:
        lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        numbered = "\n".join(f"{index + 1}| {line}" for index, line in enumerate(lines))
        header = f"待审查文件：{file_path}\n" if file_path else ""
        context = f"\n{extra_context}\n" if extra_context else ""
        memory_block = ""
        if self.memory is not None:
            recalled = self.memory.build_context(code, project=self.project)
            if recalled:
                memory_block = f"\n{recalled}\n"
        return (
            f"{header}请审查以下 Python 代码（左侧为行号，line_start/line_end 必须引用该行号）：\n"
            f"{context}{memory_block}"
            "```python\n"
            f"{numbered}\n"
            "```\n\n"
            "请先调用工具取证，再给出最终 JSON 报告。"
        )

    # ------------------------------------------------------------------

    def review(
        self,
        code: str,
        file_path: Optional[str] = None,
        extra_context: str = "",
        trace: Optional[TraceRecorder] = None,
        config: Optional[AgentConfig] = None,
    ) -> Tuple[ReviewReport, RunOutcome]:
        """执行一次审查，返回 (报告, 运行轨迹)。"""
        cfg = config or self.config
        recorder = trace or TraceRecorder(task="review")
        runtime = AgentRuntime(self.client, self.registry, cfg, recorder)

        # finalizer 需要知道「本轮实际调用了哪些工具」，因此用可变容器延迟传入
        holder: Dict[str, _ReportFinalizer] = {}

        def finalizer(text: str) -> Tuple[Optional[ReviewReport], str]:
            finalizer_impl = holder.get("impl")
            if finalizer_impl is None:
                finalizer_impl = _ReportFinalizer(
                    code=code,
                    policy=self.policy,
                    tools_provider=lambda: runtime.trace.tool_names,
                )
                holder["impl"] = finalizer_impl
            return finalizer_impl(text)

        outcome = runtime.run(
            self.build_user_prompt(code, file_path, extra_context),
            system_prompt=REVIEWER_SYSTEM_PROMPT + "\n" + OUTPUT_SCHEMA_HINT,
            finalizer=finalizer,
            task="review",
        )

        impl = holder.get("impl")
        self.last_finalizer = impl

        report = outcome.value if isinstance(outcome.value, ReviewReport) else None
        if report is None:
            # 未产出合法报告时，明确返回空报告 + 原因，而不是伪造内容
            reason = outcome.run.stop_reason
            error = outcome.run.error
            summary = f"审查未产出有效结构化报告（stop_reason={reason}{'; ' + error if error else ''}）"
            report = ReviewReport(review_summary=summary, issues=[])

        if impl is not None:
            outcome.run.output = dict(outcome.run.output or {})
            outcome.run.output["dropped_issues"] = len(impl.dropped_issues)
            outcome.run.output["flagged_issues"] = len(impl.flagged_issues)

        # 长期记忆：压制与历史误报高度一致的结论
        if self.memory is not None and report.issues:
            kept, suppressed = self.memory.apply_suppression(self.project, report.issues, code)
            if suppressed:
                report = ReviewReport(
                    review_summary=(
                        f"{report.review_summary}（记忆库压制 {len(suppressed)} 条历史误报形态）"
                    ),
                    issues=kept,
                )
                outcome.run.output = dict(outcome.run.output or {})
                outcome.run.output["suppressed_by_memory"] = suppressed

        return report, outcome
