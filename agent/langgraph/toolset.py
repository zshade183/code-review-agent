"""兼容旧导入；新工作流直接使用 :class:`ToolRegistry`。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agent.tools import build_review_workflow_registry
from agent.tools.base import ToolRegistry


@dataclass
class Toolset:
    """已弃用的单字段包装器。"""

    registry: ToolRegistry


def build_toolset(codebase_root: Optional[str] = None) -> Toolset:
    return Toolset(registry=build_review_workflow_registry(codebase_root))


__all__ = ["Toolset", "build_toolset"]
