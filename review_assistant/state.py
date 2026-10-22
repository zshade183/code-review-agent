"""审查工作流的状态定义。

通用字段：

- ``messages``：用 ``add_messages`` reducer 累加，天然支持多助手共享上下文
- ``dialog_state``：显式的「对话栈」，栈顶就是当前该说话的助手

审查业务字段：

- ``target``：本轮审查对象（文件路径 + 源码 + 行数），由 ``load_target`` 节点准备
- ``issues``：各维度子助手收集到的结论；用自定义 reducer 做**跨助手去重合并**
- ``reviewed_dimensions``：已完成维度，注入主助手提示词，让它知道自己走到哪一步
- ``fix``：修复助手产出的修复结果（经语法/签名校验）
- ``report``：汇总后的结构化报告

``issues`` 里存的是纯 dict 而不是 ``Issue`` 对象：状态需要能被 checkpoint 与
人工确认流程原样保存，保持 JSON 可序列化最省事；进入校验环节时再用
``Issue.model_validate`` 还原成强类型对象。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


def update_dialog_stack(current: List[str], update: Optional[str]) -> List[str]:
    """对话栈的 reducer：``None`` 不变，``"pop"`` 出栈，其余值入栈。"""
    if update is None:
        return current
    if update == "pop":
        return current[:-1]
    return current + [update]


def merge_issues(current: List[Dict[str, Any]], update: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """问题合并 reducer：按 (分类, 二级分类, 起始行) 去重，保留置信度更高的一条。

    这是「多个子助手先后写入同一状态字段」的关键：没有 reducer 时后写入的会
    覆盖先写入的，多维度审查就只能留下最后一个维度的结论。
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    for item in list(current) + list(update):
        if not isinstance(item, dict):
            continue
        key = (item.get("category"), item.get("subtype"), item.get("line_start"))
        previous = merged.get(key)
        if previous is None or float(item.get("confidence") or 0) > float(previous.get("confidence") or 0):
            merged[key] = item
    return list(merged.values())


def merge_dimensions(current: List[str], update: List[str]) -> List[str]:
    """已完成维度的 reducer：只追加没出现过的。"""
    result = list(current)
    for name in update:
        if name not in result:
            result.append(name)
    return result


class ReviewState(TypedDict, total=False):
    """图状态。"""

    messages: Annotated[List[AnyMessage], add_messages]
    dialog_state: Annotated[List[str], update_dialog_stack]
    #: 本轮审查对象
    target: Dict[str, Any]
    #: 各维度收集到的结论（dict 形态）
    issues: Annotated[List[Dict[str, Any]], merge_issues]
    #: 已完成的子助手（含 fix_assistant）
    reviewed_dimensions: Annotated[List[str], merge_dimensions]
    #: 修复结果
    fix: Dict[str, Any]
    #: 最终结构化报告
    report: Dict[str, Any]
    #: 简易事件流（每次工具调用/收集动作一条），便于 CLI 与测试观察
    events: Annotated[List[Dict[str, Any]], operator.add]
