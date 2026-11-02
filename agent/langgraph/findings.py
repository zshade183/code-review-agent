"""子助手结论的解析与校验。

子助手返回的是**结论**，不是工具执行结果，所以每条结论都要在收集阶段过一遍校验：

- 结构化输出必须是合法 JSON，字段必须能通过 ``Issue`` 契约
- 行号必须落在真实源码范围内，越界直接丢弃
- 每条结论必须带证据；**无证据的结论一律丢弃**（不是降权，是丢弃）
- 声明了「证据来自某工具」但该工具本轮没被调用过 → 标记未核实并下调置信度
- 越界的分类（例如安全助手报了个性能问题）会被强制归位并记录说明

这样做的好处是：多智能体架构下每个子助手的输出都要过同一道闸门，
「谁报的都算数」变成「谁能给出证据才算数」。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from agent.runtime import extract_json_block
from agent.schema import Category, Issue


def parse_json_object(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """从模型输出里取出第一个 JSON 对象。"""
    raw = extract_json_block(text or "")
    if not raw:
        return None, "未在输出中找到 JSON 对象"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
    if not isinstance(payload, dict):
        return None, "顶层必须是 JSON 对象"
    return payload, ""

'''
校验并归一化一个维度助手提交的结论。
返回校验通过的结论列表和校验失败的说明列表。
'''
def validate_issues(
    raw_issues: Sequence[Any],
    expected_category: Category,
    total_lines: int,
    called_tools: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """校验并归一化一个维度助手提交的结论。"""
    kept: List[Dict[str, Any]] = []
    notes: List[str] = []

    for index, item in enumerate(raw_issues):
        if not isinstance(item, dict):
            notes.append(f"第 {index} 条不是对象，已丢弃")
            continue

        try:
            issue = Issue.model_validate(item)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            notes.append(f"第 {index} 条字段校验失败，已丢弃：{exc}")
            continue

        if issue.category != expected_category:
            notes.append(
                f"第 {index} 条分类越界（{issue.category.value} -> {expected_category.value}），已归位"
            )
            issue.category = expected_category

        if issue.line_start > total_lines:
            notes.append(f"第 {index} 条行号 {issue.line_start} 超出源码范围，已丢弃")
            continue
        issue.line_start = max(1, issue.line_start)
        issue.line_end = max(issue.line_start, min(issue.line_end, total_lines))

        if not issue.evidence:
            notes.append(f"第 {index} 条缺少 evidence，已丢弃")
            continue

        problems: List[str] = []
        for evidence in issue.evidence:
            if evidence.tool and evidence.tool not in called_tools:
                problems.append(f"声明的工具 {evidence.tool} 本轮未被调用")
            if evidence.line is not None and not (1 <= evidence.line <= total_lines):
                problems.append(f"证据行号 {evidence.line} 超出源码范围")
            if not evidence.is_verifiable():
                problems.append("证据既无工具来源也无代码片段")

        # 「分类不在白名单」属于分类待确认，不算证据不足，单独由 subtype_valid 表达
        evidence_ok = not problems
        if not issue.is_subtype_valid():
            # 规范外问题不丢弃：记录建议分类名，作为动态白名单的候选保留到最终报告
            if not issue.suggested_subtype and issue.subtype not in ("", "Unspecified"):
                issue.suggested_subtype = issue.subtype
            label = issue.suggested_subtype or issue.subtype
            problems.append(
                f"分类 '{issue.subtype}' 未落入 {expected_category.value} 的允许集合"
                f"（候选：{label}），已作为规范外问题保留"
            )

        issue.verified = evidence_ok
        issue.verify_note = "；".join(problems)
        if not evidence_ok:
            # 只有证据不可核验才降权；分类待确认只记录说明，不否定结论本身
            issue.confidence = min(issue.confidence, 0.5)
            notes.append(f"第 {index} 条未完全核实：{issue.verify_note}")
        elif problems:
            notes.append(f"第 {index} 条分类待确认：{issue.verify_note}")

        kept.append(issue.model_dump(mode="json"))

    return kept, notes
