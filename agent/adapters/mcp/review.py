"""把审查应用服务暴露为 MCP 工具。"""

from __future__ import annotations

import json
from typing import Any, Dict

from agent.pipelines import ReviewPipeline
from agent.schema import ToolResult
from agent.tools.base import Tool


class ReviewWorkflowTool(Tool):
    """MCP 边界适配器，不是可供 Review/Verification Agent 调用的原子工具。"""

    name = "review_code"
    description = "对一段 Python 代码执行 Review → 条件 Verification 的完整审查工作流。"
    mutating = False
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "待审查的完整 Python 源码"},
            "strategy": {
                "type": "string", "enum": ["fast", "auto", "full"],
                "description": "审查策略，默认 auto", "default": "auto",
            },
            "file_path": {"type": "string", "description": "可选文件路径，仅用于上下文展示"},
        },
        "required": ["code"],
    }

    def run(self, code: str, strategy: str = "auto", file_path: str = "") -> ToolResult:
        normalized = strategy if strategy in {"fast", "auto", "full"} else "auto"
        result = ReviewPipeline(codebase_root=None, use_memory=False).review(
            code, file_path=file_path, strategy=normalized
        )
        payload: Dict[str, Any] = {
            "strategy": result.strategy.value,
            "coverage": result.coverage.to_dict(),
            "stats": {
                "engine": result.stats.get("engine"),
                "events": result.stats.get("events"),
                "partial": result.stats.get("partial"),
            },
            "report": result.report.to_legacy(),
        }
        return ToolResult(
            tool=self.name,
            ok=True,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            data=payload,
        )


# 兼容旧类名；新代码使用能体现边界职责的 ReviewWorkflowTool。
ReviewCodeTool = ReviewWorkflowTool

__all__ = ["ReviewWorkflowTool", "ReviewCodeTool"]
