"""Pipeline 级工具：把「多 Agent 审查」本身暴露成一个工具。

MCP 场景下，客户端（Claude Desktop / Cursor / Copilot）只认识工具，不认识本项目
的 HTTP 端点。因此这里把 Review Pipeline 包装成 ``review_code`` 工具，让任意 MCP
客户端可以直接发起一次完整的多 Agent 审查。

注意：本工具**只注册到 MCP 服务端**，不会进入领域 Reviewer 的工具白名单，
避免「审查 Agent 调用审查 Agent」的递归。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from agent.schema import ReviewStrategy, ToolResult
from agent.tools.base import Tool


class ReviewCodeTool(Tool):
    name = "review_code"
    description = (
        "对一段 Python 代码执行完整的**多 Agent 审查**：共享取证 → 领域 Reviewer 并行 → "
        "候选合并 → Critic/Verifier 并行 → 确定性裁决。"
        "strategy=fast 单 Agent（最快）；auto 按证据选择领域（默认）；"
        "full 四领域 + 对抗复核（最严格）。返回结构化报告、覆盖度与裁决统计。"
    )
    mutating = False
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "待审查的完整 Python 源码"},
            "strategy": {
                "type": "string",
                "enum": ["fast", "auto", "full"],
                "description": "审查策略，默认 auto",
                "default": "auto",
            },
            "file_path": {"type": "string", "description": "可选文件路径，仅用于提示词展示"},
        },
        "required": ["code"],
    }

    def run(self, code: str, strategy: str = "auto", file_path: str = "") -> ToolResult:
        # 延迟导入：避免 tools 包在 import 时就把整个 pipeline 依赖拉起来
        from agent.llm import LLMClient
        from agent.pipelines import ReviewPipeline

        normalized = strategy if strategy in {"fast", "auto", "full"} else "auto"
        pipeline = ReviewPipeline(client=LLMClient(), codebase_root=None, persist_traces=False)
        result = pipeline.review(code, file_path=file_path, strategy=normalized)
        payload: Dict[str, Any] = {
            "strategy": result.strategy.value,
            "coverage": result.coverage.to_dict(),
            "stats": {
                "adjudication": result.stats.get("adjudication"),
                "critic": result.stats.get("critic"),
                "verifier": result.stats.get("verifier"),
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


__all__ = ["ReviewCodeTool"]
