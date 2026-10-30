"""选择性 Critic：只复核真正需要对抗式质疑的候选。

全量 Critic 会让成本翻倍，而大多数候选并不需要第二意见。触发复核的条件（任一命中）：

- 高危问题
- 低置信度问题
- **没有独立佐证**的问题（Verifier 未命中）
- 多个 Reviewer 结论冲突的问题
- 命中历史误报形态的问题
- 修复风险较大的问题（高危安全/缺陷类）

Critic 是「反方律师」：只拿到代码 + 候选清单 + 独立佐证，看不到其他角色的推理过程，
从而避免自我确认偏差。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from agent import settings
from agent.evidence.verifier import Corroboration
from agent.llm import LLMClient
from agent.runtime import AgentConfig, AgentRuntime, RunOutcome, extract_json_block
from agent.schema import Category, Issue, Severity
from agent.tools import build_reviewer_registry
from agent.tools.base import ToolRegistry
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)


CRITIC_SYSTEM_PROMPT = """
你是一名**对抗式代码审查评审员（Critic）**。你的职责不是再找新问题，而是**质疑已有的每一条结论**。

对每条候选问题，请以「反方律师」的立场完成：

1. 该问题是否客观存在？还是审查方误解了业务语义？
2. 该问题是否真的可被触发？有没有防御性代码使它在实践中无法发生？
3. 该问题是否只是代码风格偏好，而非真实缺陷？
4. 该问题是否与同一份清单中的其他条目重复？

你可以调用工具独立核实（`get_ast_outline` 看真实作用域、`find_symbol` 确认变量是否真未被使用等）。
**不要直接照搬原报告的理由**，必须给出你自己的判断依据。

对每条候选给出三种裁定之一：
- `keep`：结论成立，保留
- `downgrade`：问题存在但被高估（严重性偏高、影响有限）
- `drop`：这是误报，应当删除

【输出格式】只输出 JSON，不要 markdown 代码块、不要解释文字：
{
  "verdicts": [
    {"index": 0, "verdict": "keep", "reason": "你的独立判断依据", "confidence": 0.8}
  ],
  "overall_comment": "整体评价（可选）"
}
`index` 必须是下方清单中方括号内的编号，每条都要给出裁定。
"""


class IssueVerdict(BaseModel):
    """Critic 对单条候选的裁定。"""

    model_config = ConfigDict(extra="ignore")

    index: int
    verdict: str = "keep"
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CriticOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdicts: List[IssueVerdict] = Field(default_factory=list)
    overall_comment: str = ""
    #: 本轮实际送审的候选 ID（与 verdicts 的 index 一一对应）
    reviewed: List[str] = Field(default_factory=list)
    #: 没有候选需要复核时跳过 Critic（不产生 LLM 调用）
    skipped: bool = False

    def verdict_map(self) -> Dict[str, IssueVerdict]:
        """candidate_id -> 裁定；未覆盖的候选由调用方保守补 keep。"""
        mapping: Dict[str, IssueVerdict] = {}
        for index, verdict in enumerate(self.verdicts):
            if 0 <= index < len(self.reviewed):
                mapping[self.reviewed[index]] = verdict
        return mapping


@dataclass
class CriticSelection:
    """选择性复核的入选清单与原因。"""

    issues: List[Issue] = field(default_factory=list)
    reasons: Dict[str, List[str]] = field(default_factory=dict)

    def reason_text(self, candidate_id: str) -> str:
        return "、".join(self.reasons.get(candidate_id, []))

    def to_dict(self) -> Dict[str, object]:
        return {
            "count": len(self.issues),
            "candidates": list(self.reasons),
            "reasons": {key: list(value) for key, value in self.reasons.items()},
        }


class CriticPolicy:
    """确定性选择「哪些候选值得复核」。"""

    def __init__(
        self,
        low_confidence: Optional[float] = None,
        force_severities: Optional[Sequence[str]] = None,
    ) -> None:
        self.low_confidence = (
            settings.CRITIC_LOW_CONFIDENCE if low_confidence is None else low_confidence
        )
        raw = force_severities if force_severities is not None else settings.CRITIC_FORCE_REVIEW_SEVERITIES
        self.force_severities = {str(item).upper() for item in raw}

    def select(
        self,
        issues: Sequence[Issue],
        corroborations: Dict[str, Corroboration],
        conflict_ids: Optional[Sequence[str]] = None,
        memory_flags: Optional[Dict[str, str]] = None,
    ) -> CriticSelection:
        conflict_set = set(conflict_ids or ())
        memory_map = memory_flags or {}
        selection = CriticSelection()
        for issue in issues:
            candidate_id = issue.ensure_candidate_id()
            reasons: List[str] = []
            if issue.severity.value.upper() in self.force_severities:
                reasons.append("高危问题")
            if issue.confidence < self.low_confidence:
                reasons.append(f"置信度偏低（{issue.confidence:.2f}）")
            corroboration = corroborations.get(candidate_id)
            if corroboration is None or not corroboration.corroborated:
                reasons.append("没有独立佐证")
            if candidate_id in conflict_set or issue.disagreement:
                reasons.append("跨角色结论冲突")
            if candidate_id in memory_map:
                reasons.append("命中历史误报形态")
            if issue.category is Category.SECURITY and issue.severity in (
                Severity.HIGH,
                Severity.MEDIUM_HIGH,
            ):
                reasons.append("修复风险较大")
            if reasons:
                selection.issues.append(issue)
                selection.reasons[candidate_id] = list(dict.fromkeys(reasons))
        return selection


class CriticAgent:
    """对选中候选逐条质疑的 LLM 角色。"""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        codebase_root: Optional[str] = None,
    ) -> None:
        self.client = client or LLMClient()
        self.registry = registry or build_reviewer_registry(codebase_root)
        self.config = config or AgentConfig(max_steps=5, max_tool_calls=6)
        self.policy = CriticPolicy()

    # ------------------------------------------------------------------

    def build_user_prompt(
        self,
        code: str,
        issues: Sequence[Issue],
        corroborations: Dict[str, Corroboration],
    ) -> str:
        code_lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        numbered = "\n".join(f"{index + 1}| {line}" for index, line in enumerate(code_lines))
        issue_lines: List[str] = []
        for index, issue in enumerate(issues):
            corroboration = corroborations.get(issue.ensure_candidate_id())
            if corroboration is None:
                corroboration_text = "独立引擎佐证：未知"
            elif corroboration.corroborated:
                corroboration_text = "独立引擎佐证：有（" + "/".join(corroboration.engines) + "）"
            else:
                corroboration_text = "独立引擎佐证：无（仅降低置信度，不代表问题不存在）"
            issue_lines.append(
                f"[{index}] 【{issue.category.value} / {issue.severity.value}】"
                f"行 {issue.line_start}-{issue.line_end} {issue.subtype}"
                f"（产出角色：{'/'.join(issue.sources) or issue.produced_by}，"
                f"置信度 {issue.confidence:.2f}）\n"
                f"    描述：{issue.risk_description}\n"
                f"    建议：{issue.fix_suggestion}\n"
                f"    {corroboration_text}"
            )
        return (
            "请评审以下候选问题清单中的每一条结论。\n\n"
            "【代码】（左侧为行号）\n"
            f"```python\n{numbered}\n```\n\n"
            "【待评审的问题清单】\n"
            + ("\n".join(issue_lines) if issue_lines else "（空）")
            + "\n\n请逐条给出裁定，并输出 JSON。"
        )

    def review(
        self,
        code: str,
        issues: Sequence[Issue],
        corroborations: Dict[str, Corroboration],
        selection: Optional[CriticSelection] = None,
        trace: Optional[TraceRecorder] = None,
    ) -> Tuple[CriticOutput, Optional[RunOutcome]]:
        """只复核 ``issues``（已由 CriticPolicy 选中）；为空时跳过，不产生 LLM 调用。"""
        selected = list(issues)
        reviewed_ids = [issue.ensure_candidate_id() for issue in selected]
        if not selected:
            return CriticOutput(skipped=True, reviewed=[]), None

        recorder = trace or TraceRecorder(task="critic")
        runtime = AgentRuntime(self.client, self.registry, self.config, recorder)
        total = len(selected)

        def finalizer(text: str) -> Tuple[Optional[CriticOutput], str]:
            raw = extract_json_block(text)
            if not raw:
                return None, "未找到 JSON 对象，请只输出 JSON。"
            try:
                payload = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                return None, f"JSON 解析失败：{exc}"
            try:
                output = CriticOutput.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                return None, f"字段校验失败：{exc}"
            if not output.verdicts:
                return None, "verdicts 为空，请对每一条候选给出裁定。"
            output.verdicts = [v for v in output.verdicts if 0 <= v.index < total]
            output.reviewed = list(reviewed_ids)
            return output, ""

        outcome = runtime.run(
            self.build_user_prompt(code, selected, corroborations),
            system_prompt=CRITIC_SYSTEM_PROMPT,
            finalizer=finalizer,
            task="critic",
        )
        output = outcome.value if isinstance(outcome.value, CriticOutput) else CriticOutput(
            skipped=False
        )
        output.reviewed = list(reviewed_ids)
        covered = {verdict.index for verdict in output.verdicts}
        for index in range(total):
            if index not in covered:
                # 未覆盖的候选保守保留，不因评审失败而黑箱删项
                output.verdicts.append(
                    IssueVerdict(index=index, verdict="keep", reason="评审未覆盖，保守保留", confidence=0.3)
                )
        outcome.run.output = dict(outcome.run.output or {})
        outcome.run.output.update(
            {
                "role": "critic",
                "reviewed": len(selected),
                "selection": (selection.to_dict() if selection is not None else {}),
            }
        )
        return output, outcome
