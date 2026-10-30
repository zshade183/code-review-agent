"""Deterministic Adjudicator：最终裁决由 Python 规则完成。

不在流程末尾再加一个 Judge Agent —— 那会把「最终结论是否可信」重新交回给单个模型。
每条候选在这里进入四种状态之一：

- ``confirmed``       交叉验证通过，可直接展示
- ``needs_review``    证据不足 / 角色分歧 / 无独立佐证，保留但提示人工复核
- ``memory_suspected`` 命中历史误报形态，降权并单独分组（**不删除**）
- ``rejected``        多方一致认为不成立，移出最终报告

关键边界：**没有独立佐证只降低置信度**，不会自动把候选判为不存在；
只有 Critic 判 drop 且没有任何独立引擎佐证时才会 rejected。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from agent import settings
from agent.adjudication.critic import IssueVerdict
from agent.evidence.verifier import Corroboration, VerificationResult
from agent.schema import AdjudicationStatus, Issue, Severity

#: 严重性排序（裁决后展示顺序）
_SEVERITY_ORDER = {
    Severity.HIGH: 4,
    Severity.MEDIUM_HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
}

#: 展示顺序：确认的在前，其次人工复核，最后记忆存疑
_STATUS_ORDER = {
    AdjudicationStatus.CONFIRMED: 0,
    AdjudicationStatus.NEEDS_REVIEW: 1,
    AdjudicationStatus.MEMORY_SUSPECTED: 2,
}


@dataclass
class AdjudicationResult:
    """确定性裁决结果。"""

    issues: List[Issue] = field(default_factory=list)
    rejected: List[Issue] = field(default_factory=list)
    notes: Dict[str, str] = field(default_factory=dict)

    def by_status(self, status: AdjudicationStatus) -> List[Issue]:
        return [issue for issue in self.issues if issue.adjudication is status]

    @property
    def confirmed(self) -> List[Issue]:
        return self.by_status(AdjudicationStatus.CONFIRMED)

    @property
    def needs_review(self) -> List[Issue]:
        return self.by_status(AdjudicationStatus.NEEDS_REVIEW)

    @property
    def memory_suspected(self) -> List[Issue]:
        return self.by_status(AdjudicationStatus.MEMORY_SUSPECTED)

    def counts(self) -> Dict[str, int]:
        return {
            "confirmed": len(self.confirmed),
            "needs_review": len(self.needs_review),
            "memory_suspected": len(self.memory_suspected),
            "rejected": len(self.rejected),
        }

    def to_dict(self) -> Dict[str, object]:
        return {
            "counts": self.counts(),
            "issues": [issue.to_legacy() for issue in self.issues],
            "rejected": [issue.to_legacy() for issue in self.rejected],
        }


class Adjudicator:
    """纯规则裁决器。"""

    def __init__(self, confirm_confidence: Optional[float] = None) -> None:
        self.confirm_confidence = (
            settings.ADJUDICATION_CONFIRM_CONFIDENCE
            if confirm_confidence is None
            else confirm_confidence
        )

    # ------------------------------------------------------------------

    def adjudicate(
        self,
        issues: Sequence[Issue],
        verdicts: Optional[Dict[str, IssueVerdict]] = None,
        corroborations: Optional[Dict[str, Corroboration]] = None,
        memory_flags: Optional[Dict[str, str]] = None,
        conflict_ids: Optional[Sequence[str]] = None,
        verification: Optional[VerificationResult] = None,
    ) -> AdjudicationResult:
        verdict_map = verdicts or {}
        corroboration_map = corroborations or {}
        memory_map = memory_flags or {}
        conflict_set = set(conflict_ids or ())
        no_engine_available = bool(verification is not None and not verification.engines_used)
        limitations = verification.limitations() if verification is not None else ""

        result = AdjudicationResult()
        for original in issues:
            issue = original.model_copy(deep=True)
            candidate_id = issue.ensure_candidate_id()
            verdict = verdict_map.get(candidate_id)
            corroboration = corroboration_map.get(candidate_id)
            corroborated = bool(corroboration is not None and corroboration.corroborated)
            memory_note = memory_map.get(candidate_id, "")
            reasons: List[str] = []

            # ---- 1) Critic 判误报 ----
            if verdict is not None and verdict.verdict == "drop":
                if corroborated:
                    issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                    issue.disagreement = True
                    issue.confidence = min(issue.confidence, 0.4)
                    reasons.append(
                        f"Critic 判为误报（{verdict.reason}），但独立引擎 "
                        f"{'/'.join(corroboration.engines)} 给出同类告警；保留并交人工复核"
                    )
                else:
                    issue.adjudication = AdjudicationStatus.REJECTED
                    reasons.append(f"Critic 判为误报（{verdict.reason}）且无独立引擎佐证，移出报告")
                    issue.adjudication_reason = self._join(reasons, limitations)
                    result.rejected.append(issue)
                    result.notes[candidate_id] = issue.adjudication_reason
                    continue

            # ---- 2) Critic 判高估 ----
            elif verdict is not None and verdict.verdict == "downgrade":
                issue.confidence = round(max(0.0, issue.confidence - 0.3), 4)
                issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                reasons.append(f"Critic 认为问题被高估：{verdict.reason}")

            # ---- 3) 记忆命中：只在裁决后降权分组，不删除 ----
            if memory_note:
                if corroborated:
                    issue.confidence = min(issue.confidence, 0.55)
                    reasons.append(f"{memory_note}（存在独立佐证，仅降权保留）")
                elif issue.adjudication is None:
                    issue.adjudication = AdjudicationStatus.MEMORY_SUSPECTED
                    issue.confidence = min(issue.confidence, 0.4)
                    reasons.append(memory_note)
                else:
                    reasons.append(memory_note)

            # ---- 4) 跨角色冲突 ----
            if issue.adjudication is None and (candidate_id in conflict_set or issue.disagreement):
                issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                if candidate_id in conflict_set:
                    reasons.append("多个领域 Reviewer 在同一位置给出不同类别结论，需人工复核")

            # ---- 5) 独立佐证 / 基础验证 / 置信度兜底 ----
            if issue.adjudication is None:
                if issue.verified is False:
                    issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                    reasons.append("Reviewer 证据未通过校验，需人工复核")
                elif verification is None:
                    # fast 策略没有启用独立 Verifier：以 Reviewer 的证据校验作为基础验证
                    if issue.verified is True or issue.confidence >= 0.7:
                        issue.adjudication = AdjudicationStatus.CONFIRMED
                        reasons.append("基础验证通过（证据校验成功），fast 策略未启用独立引擎比对")
                    else:
                        issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                        reasons.append("基础验证信息不足，需人工复核")
                elif corroborated:
                    issue.adjudication = AdjudicationStatus.CONFIRMED
                    reasons.append(
                        f"独立引擎 {'/'.join(corroboration.engines)} 在相近位置给出同类告警"
                    )
                elif no_engine_available:
                    issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                    reasons.append("本次没有可用的独立验证引擎，无法交叉验证")
                else:
                    # 未找到佐证只降低置信度；仍高于下限才确认
                    issue.confidence = round(issue.confidence * 0.85, 4)
                    if issue.confidence >= self.confirm_confidence:
                        issue.adjudication = AdjudicationStatus.CONFIRMED
                        reasons.append("无独立佐证，但置信度超过确认下限（已降权）")
                    else:
                        issue.adjudication = AdjudicationStatus.NEEDS_REVIEW
                        reasons.append("无独立佐证且置信度低于确认下限，需人工复核")

            issue.adjudication_reason = self._join(reasons, limitations)
            result.notes[candidate_id] = issue.adjudication_reason
            result.issues.append(issue)

        result.issues.sort(
            key=lambda issue: (
                _STATUS_ORDER.get(issue.adjudication or AdjudicationStatus.NEEDS_REVIEW, 9),
                -_SEVERITY_ORDER.get(issue.severity, 0),
                issue.line_start,
                issue.category.value,
            )
        )
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _join(reasons: Sequence[str], limitations: str = "") -> str:
        text = "；".join(reason for reason in reasons if reason)
        if limitations and ("无法交叉验证" in text or "没有可用的独立验证引擎" in text):
            text = f"{text}（能力限制：{limitations}）"
        return text
