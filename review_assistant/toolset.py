"""工具组装。

按**维度**给每个子助手挑选工具，并遵守两条原则：

1. **最小权限**：四个审查维度助手只拿只读工具，拿不到任何执行/改代码能力
2. **显式区分副作用**：把 ``mutating`` 的工具挑出来作为敏感节点，
   交给工作流用 ``interrupt_before`` 拦一道人工确认
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from agent.tools import build_registry
from review_assistant.dimensions import DIMENSION_ASSISTANTS, SENSITIVE_TOOL_NAMES
from review_assistant.tool_adapter import RegistryTool

#: 所有维度助手共用的只读取证工具
READ_ONLY_NAMES = (
    "run_static_analysis",
    "get_ast_outline",
    "find_symbol",
    "check_syntax",
)

#: 某些维度额外可用的只读工具
DIMENSION_EXTRA: Dict[str, Sequence[str]] = {
    "security_assistant": ("scan_dangerous_code",),
}

#: 修复助手的工具（含敏感工具）
FIX_NAMES = (
    "run_static_analysis",
    "get_ast_outline",
    "check_syntax",
    "check_signature",
    "create_patch",
    "apply_patch",
    "execute_python_code",
    "run_tests",
    "scan_dangerous_code",
)

#: 代码库检索工具（配置了代码库根目录后才存在）
CODEBASE_NAMES = (
    "search_codebase",
    "read_project_file",
    "find_symbol_in_project",
)


@dataclass
class Toolset:
    """各节点可用的工具集合与敏感工具名单。"""

    primary: List[object] = field(default_factory=list)
    assistants: Dict[str, List[object]] = field(default_factory=dict)
    #: 子助手 -> 需要人工确认的工具名
    sensitive: Dict[str, Set[str]] = field(default_factory=dict)
    #: 原始 ToolRegistry（收集节点用它做无副作用的结果校验，例如签名比对）
    registry: Optional[object] = None

    def sensitive_nodes(self) -> List[str]:
        """需要 ``interrupt_before`` 的节点名。"""
        return [
            f"{name}_sensitive_tools"
            for name, names in self.sensitive.items()
            if names
        ]


def _pick(converted: Dict[str, RegistryTool], names: Sequence[str]) -> List[RegistryTool]:
    return [converted[name] for name in names if name in converted]

#: 按角色装配工具。
def build_toolset(codebase_root: Optional[str] = None) -> Toolset:
    """按角色装配工具。"""
    from review_assistant.policy_tools import lookup_rule

    inner = build_registry(codebase_root)
    converted = {name: RegistryTool.from_inner(inner.get(name)) for name in inner.names()}

    codebase = _pick(converted, CODEBASE_NAMES)
    policy = [lookup_rule]

    assistants: Dict[str, List[object]] = {}
    sensitive: Dict[str, Set[str]] = {}

    for name in DIMENSION_ASSISTANTS:
        names = tuple(READ_ONLY_NAMES) + tuple(DIMENSION_EXTRA.get(name, ()))
        tools = _pick(converted, names) + codebase + policy
        assistants[name] = tools
        sensitive[name] = {
            tool.name for tool in tools if getattr(tool, "mutating", False)
        }

    fix_tools = _pick(converted, FIX_NAMES) + codebase + policy
    assistants["fix_assistant"] = fix_tools
    sensitive["fix_assistant"] = {
        tool.name
        for tool in fix_tools
        if getattr(tool, "mutating", False) or tool.name in SENSITIVE_TOOL_NAMES
    }

    return Toolset(
        # 主助手只负责调度、规范检索与代码库定位，取证交给子助手
        primary=[*policy, *codebase],
        assistants=assistants,
        sensitive={name: names for name, names in sensitive.items() if names},
        registry=inner,
    )
