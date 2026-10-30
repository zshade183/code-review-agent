"""Candidate Merger：确定性的候选规范化、去重与冲突检测。

**刻意不使用 LLM**：同一批候选无论并发返回顺序如何，合并结果必须完全一致，
否则相同输入会得到不稳定的审查结论，候选 ID 也会漂移，前端反馈无法锚定。

处理步骤（纯 Python，可按顺序复现）：

1. 规范化：行号裁剪到合法范围、补齐缺失二级分类、过滤无证据候选
2. 去重：``(类别, 规范化二级分类, 起始行)`` 相同即同一问题，合并来源角色与证据
3. 冲突检测：同一位置附近出现「不同类别」的结论 -> 标记跨角色冲突，交给选择性 Critic
4. 生成稳定 ``candidate_id`` 并按固定顺序排序
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from agent.schema import Category, FindingCandidate, Issue

_WS_RE = re.compile(r"\s+")


def normalize_subtype(subtype: str) -> str:
    """规范化二级分类（去多余空白、大小写不敏感用于去重键）。"""
    return _WS_RE.sub(" ", str(subtype or "")).strip().lower()


def _merge_evidence(issues: Sequence[Issue]) -> List[Any]:
    """合并多条候选的证据，按 (kind, tool, line, detail) 去重。"""
    seen: Set[Tuple[Any, ...]] = set()
    merged: List[Any] = []
    for issue in issues:
        for evidence in issue.evidence:
            key = (evidence.kind, evidence.tool, evidence.line, normalize_subtype(evidence.detail))
            if key in seen:
                continue
            seen.add(key)
            merged.append(evidence.model_copy(deep=True))
    return merged


def _pick_confidence(issues: Sequence[Issue]) -> float:
    """多角色对同一问题的置信度：取最高值（一致命中说明更可信）。"""
    return max((issue.confidence for issue in issues), default=0.5)


def _pick_severity(issues: Sequence[Issue]) -> Any:
    """多角色对同一问题的严重性：取更严重的一个（保守）。"""
    order = {"High": 4, "Medium-High": 3, "Medium": 2, "Low": 1}
    return max(issues, key=lambda issue: order.get(issue.severity.value, 0)).severity


@dataclass
class CrossAgentConflict:
    """两个角色在同一位置给出不同类别结论。"""

    line: int
    categories: List[str]
    reviewers: List[str]
    candidate_ids: List[str]
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line": self.line,
            "categories": list(self.categories),
            "reviewers": list(self.reviewers),
            "candidate_ids": list(self.candidate_ids),
            "reason": self.reason,
        }


@dataclass
class MergeResult:
    """确定性合并的产出。"""

    issues: List[Issue] = field(default_factory=list)
    conflicts: List[CrossAgentConflict] = field(default_factory=list)
    dropped: List[Dict[str, Any]] = field(default_factory=list)
    source_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def conflict_ids(self) -> Set[str]:
        return {cid for conflict in self.conflicts for cid in conflict.candidate_ids}

    @property
    def candidate_ids(self) -> List[str]:
        return [issue.candidate_id for issue in self.issues]


class CandidateMerger:
    """把多角色候选合并成一份规范化的候选清单。"""

    def __init__(self, line_tolerance: int = 2, max_issues: int = 60) -> None:
        self.line_tolerance = max(0, int(line_tolerance))
        self.max_issues = max(1, int(max_issues))

    # ------------------------------------------------------------------

    def merge(
        self,
        candidates: Sequence[FindingCandidate],
        total_lines: int = 0,
    ) -> MergeResult:
        result = MergeResult()
        total_lines = max(1, int(total_lines or 1))

        # 1) 规范化 + 过滤（按稳定键排序，消除并发返回顺序差异）
        normalized: List[Issue] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item.issue.category.value,
                normalize_subtype(item.issue.subtype),
                item.issue.line_start,
                item.reviewer,
            ),
        ):
            issue = candidate.to_issue()
            result.source_counts[candidate.reviewer] = (
                result.source_counts.get(candidate.reviewer, 0) + 1
            )
            if issue.line_start > total_lines:
                result.dropped.append(
                    {"reason": "行号超出代码范围", "issue": issue.to_legacy()}
                )
                continue
            issue.line_end = max(issue.line_start, min(issue.line_end, total_lines))
            if not issue.evidence:
                result.dropped.append({"reason": "缺少证据", "issue": issue.to_legacy()})
                continue
            if not issue.suggested_subtype and not issue.is_subtype_valid():
                issue.suggested_subtype = issue.subtype
            normalized.append(issue)

        # 2) 确定性去重：同一 (类别, 二级分类, 起始行) 合并为一条
        grouped: Dict[Tuple[str, str, int], List[Issue]] = {}
        for issue in normalized:
            key = (issue.category.value, normalize_subtype(issue.subtype), issue.line_start)
            grouped.setdefault(key, []).append(issue)

        merged: List[Issue] = []
        for key in sorted(grouped):
            group = grouped[key]
            base = max(group, key=lambda item: (item.confidence, len(item.evidence))).model_copy(
                deep=True
            )
            base.severity = _pick_severity(group)
            base.confidence = _pick_confidence(group)
            base.evidence = _merge_evidence(group)
            base.sources = sorted({source for item in group for source in (item.sources or [item.produced_by])})
            base.produced_by = "+".join(base.sources)
            base.disagreement = any(item.disagreement for item in group)
            notes = [item.verify_note for item in group if item.verify_note]
            base.verify_note = "；".join(dict.fromkeys(notes))
            base.ensure_candidate_id()
            merged.append(base)

        # 3) 跨角色冲突：位置相近但类别不同
        conflicts: List[CrossAgentConflict] = []
        conflict_ids: Set[str] = set()
        for left_index in range(len(merged)):
            for right_index in range(left_index + 1, len(merged)):
                left, right = merged[left_index], merged[right_index]
                if left.category is right.category:
                    continue
                if abs(left.line_start - right.line_start) > self.line_tolerance:
                    continue
                if set(left.sources) == set(right.sources):
                    # 同一角色产出的两条不同类别结论不算「跨角色冲突」
                    continue
                categories = sorted({left.category.value, right.category.value})
                reviewers = sorted(set(left.sources) | set(right.sources))
                conflict_ids.update({left.candidate_id, right.candidate_id})
                conflicts.append(
                    CrossAgentConflict(
                        line=min(left.line_start, right.line_start),
                        categories=categories,
                        reviewers=reviewers,
                        candidate_ids=[left.candidate_id, right.candidate_id],
                        reason=(
                            f"角色 {'/'.join(reviewers)} 在行 {left.line_start}/{right.line_start} "
                            f"分别给出 {' 与 '.join(categories)} 结论"
                        ),
                    )
                )
        for issue in merged:
            if issue.candidate_id in conflict_ids:
                issue.disagreement = True
                conflict_note = "存在跨角色冲突（同位置不同类别），建议人工复核"
                issue.verify_note = "；".join(filter(None, [issue.verify_note, conflict_note]))

        # 4) 固定排序 + 容量上限
        severity_order = {"High": 4, "Medium-High": 3, "Medium": 2, "Low": 1}
        merged.sort(
            key=lambda issue: (
                issue.line_start,
                -severity_order.get(issue.severity.value, 0),
                issue.category.value,
                issue.subtype,
            )
        )
        if len(merged) > self.max_issues:
            result.dropped.append(
                {"reason": f"超过单次候选上限 {self.max_issues}", "count": len(merged) - self.max_issues}
            )
            merged = merged[: self.max_issues]

        result.issues = merged
        result.conflicts = conflicts
        return result
