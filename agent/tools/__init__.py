"""工具集合与默认注册表。

运行时按「角色」裁剪工具白名单，这是 Agent 设计里的重要一环：
- 审查 Agent 只需要**只读**工具（拿证据），拿不到任何写能力
- 优化 Agent 才需要 patch 与沙箱执行能力

工具越少，模型选错的概率越低，同时也能限制单次运行的副作用范围。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent import settings
from agent.tools.ast_tools import (
    CheckSignatureTool,
    CheckSyntaxTool,
    FindSymbolTool,
    GetAstOutlineTool,
    ListToolsTool,
)
from agent.tools.base import Tool, ToolRegistry
from agent.tools.codebase import (
    CodebaseIndex,
    FindSymbolInProjectTool,
    ReadFileTool,
    SearchCodebaseTool,
)
from agent.tools.patch import ApplyPatchTool, CreatePatchTool
from agent.tools.sandbox import RunPythonTool, RunTestsTool, ScanDangerousCodeTool
from agent.tools.static_analysis import RunStaticAnalysisTool, available_engines

#: 审查 Agent 的工具白名单（全部只读）
REVIEW_TOOL_NAMES = (
    "run_static_analysis",
    "get_ast_outline",
    "find_symbol",
    "check_syntax",
    "search_codebase",
    "read_project_file",
    "find_symbol_in_project",
    "list_tools",
)

#: 优化 Agent 的工具白名单（含验证与补丁）
OPTIMIZE_TOOL_NAMES = (
    "run_static_analysis",
    "get_ast_outline",
    "find_symbol",
    "check_syntax",
    "check_signature",
    "create_patch",
    "apply_patch",
    "execute_python_code",
    "run_tests",
    "scan_dangerous_code",
    "search_codebase",
    "find_symbol_in_project",
    "list_tools",
)


def build_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """构建全量工具注册表。"""
    registry = ToolRegistry()

    # 只读分析类
    registry.register(RunStaticAnalysisTool())
    registry.register(GetAstOutlineTool())
    registry.register(FindSymbolTool())
    registry.register(CheckSyntaxTool())
    registry.register(CheckSignatureTool())

    # 补丁类
    registry.register(CreatePatchTool())
    registry.register(ApplyPatchTool())

    # 沙箱类
    registry.register(ScanDangerousCodeTool())
    if settings.ENABLE_SANDBOX:
        registry.register(RunPythonTool())
        registry.register(RunTestsTool())

    # 代码库检索类
    root = (codebase_root or settings.CODEBASE_ROOT or "").strip()
    if root and Path(root).exists():
        index = CodebaseIndex(root)
        registry.register(SearchCodebaseTool(index))
        registry.register(ReadFileTool(index))
        registry.register(FindSymbolInProjectTool(index))

    registry.register(ListToolsTool(registry))
    return registry


def build_reviewer_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """审查 Agent 的只读工具集。"""
    full = build_registry(codebase_root)
    available = [name for name in REVIEW_TOOL_NAMES if name in full]
    return full.subset(available)


def build_optimizer_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """优化 Agent 的工具集（含验证与补丁能力）。"""
    full = build_registry(codebase_root)
    available = [name for name in OPTIMIZE_TOOL_NAMES if name in full]
    return full.subset(available)


__all__ = [
    "Tool",
    "ToolRegistry",
    "CodebaseIndex",
    "build_registry",
    "build_reviewer_registry",
    "build_optimizer_registry",
    "available_engines",
    "REVIEW_TOOL_NAMES",
    "OPTIMIZE_TOOL_NAMES",
]
