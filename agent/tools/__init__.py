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

#: Review Agent：发现候选，允许静态扫描和源码/项目上下文读取。
REVIEW_TOOL_NAMES = (
    "run_static_analysis",
    "get_ast_outline",
    "find_symbol",
    "search_codebase",
    "read_project_file",
)

#: Verification Agent：围绕已有候选寻找成立条件或反证，不默认重复完整静态扫描。
VERIFICATION_TOOL_NAMES = (
    "check_syntax",
    "get_ast_outline",
    "find_symbol",
    "search_codebase",
    "read_project_file",
    "find_symbol_in_project",
)

#: Fix Agent 的工具白名单：允许分析和自测，不允许生成或应用最终补丁。
FIX_AGENT_TOOL_NAMES = (
    "get_ast_outline",
    "find_symbol",
    "check_syntax",
    "check_signature",
    "execute_python_code",
    "run_tests",
    "scan_dangerous_code",
    "search_codebase",
    "read_project_file",
    "find_symbol_in_project",
)

#: Harness 的确定性验证能力，不直接作为一组权限暴露给模型。
HARNESS_TOOL_NAMES = (
    "check_signature",
    "execute_python_code",
    "run_tests",
    "scan_dangerous_code",
)

# 兼容旧常量名。
OPTIMIZE_TOOL_NAMES = FIX_AGENT_TOOL_NAMES


def build_full_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """构建全量工具注册表；仅供显式管理/调试入口使用。"""
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


def _profile_registry(names: tuple[str, ...], codebase_root: Optional[str]) -> ToolRegistry:
    full = build_full_registry(codebase_root)
    available = [name for name in names if name in full]
    return full.subset(available)


def build_review_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """Review Agent 的只读发现工具。"""
    return _profile_registry(REVIEW_TOOL_NAMES, codebase_root)


def build_verification_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """Verification Agent 的只读核验工具。"""
    return _profile_registry(VERIFICATION_TOOL_NAMES, codebase_root)


def build_review_workflow_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """正式审查图所需的最小只读工具并集。"""
    names = tuple(dict.fromkeys((*REVIEW_TOOL_NAMES, *VERIFICATION_TOOL_NAMES)))
    return _profile_registry(names, codebase_root)


def build_reviewer_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """兼容旧名称；等价于 ``build_review_registry``。"""
    return build_review_registry(codebase_root)


def build_fix_agent_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """Fix Agent 的分析和自测工具；最终补丁与交付裁决由 Harness 掌握。"""
    return _profile_registry(FIX_AGENT_TOOL_NAMES, codebase_root)


def build_harness_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """构建 Fix Harness 的确定性验证工具集。"""
    return _profile_registry(HARNESS_TOOL_NAMES, codebase_root)


def build_optimizer_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """兼容旧名称；等价于 ``build_fix_agent_registry``。"""
    return build_fix_agent_registry(codebase_root)


def build_registry(codebase_root: Optional[str] = None) -> ToolRegistry:
    """兼容旧名称；新代码必须优先选择显式 capability profile。"""
    return build_full_registry(codebase_root)


__all__ = [
    "Tool",
    "ToolRegistry",
    "CodebaseIndex",
    "build_full_registry",
    "build_registry",
    "build_review_registry",
    "build_reviewer_registry",
    "build_verification_registry",
    "build_review_workflow_registry",
    "build_fix_agent_registry",
    "build_harness_registry",
    "build_optimizer_registry",
    "available_engines",
    "REVIEW_TOOL_NAMES",
    "VERIFICATION_TOOL_NAMES",
    "OPTIMIZE_TOOL_NAMES",
    "FIX_AGENT_TOOL_NAMES",
    "HARNESS_TOOL_NAMES",
]
