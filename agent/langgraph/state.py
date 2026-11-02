"""三角色审查图共享状态。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, TypedDict


def merge_dicts(current: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(current or {})
    merged.update(update or {})
    return merged

'''
三角色审查图共享状态。
包含所有角色的上下文、候选、合并问题、内存标志、证据记录、验证计划、验证审核、验证状态、覆盖度报告、代理轨迹、报告、事件。
'''
class ReviewState(TypedDict, total=False):
    target: Dict[str, Any]
    strategy: str
    project: str
    use_memory: bool
    extra_context: str
    context: Dict[str, Any]
    candidates: Annotated[List[Dict[str, Any]], operator.add]
    merged_issues: List[Dict[str, Any]]
    memory_flags: Dict[str, str]
    evidence_records: Annotated[Dict[str, Any], merge_dicts]
    verification_plan: List[str]
    verification_reviews: Dict[str, Any]
    verification_status: str
    coverage: Annotated[Dict[str, Any], merge_dicts]
    agent_traces: Annotated[List[Dict[str, Any]], operator.add]
    report: Dict[str, Any]
    events: Annotated[List[Dict[str, Any]], operator.add]


__all__ = ["ReviewState", "merge_dicts"]
