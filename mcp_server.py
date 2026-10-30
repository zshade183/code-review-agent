"""MCP Server：把 Agent 工具以 Model Context Protocol 暴露给任意 MCP 客户端。

这是当前 Agent 生态最关键的互操作协议：只要暴露成 MCP Server，Claude Desktop、
Cursor、VS Code Copilot、以及各种 Agent 框架都能直接调用本项目的代码审查能力，
而不需要为本项目单独写插件。

实现说明
--------
- 传输：stdio + **换行分隔的 JSON-RPC 2.0**（MCP 的 stdio 传输不用 LSP 那种
  Content-Length 分帧）。所有日志一律写 stderr，stdout 只允许出现协议消息，
  否则客户端解析会失败。
- 支持方法：``initialize`` / ``notifications/initialized`` / ``ping`` /
  ``tools/list`` / ``tools/call``；支持 JSON-RPC 批量请求。
- 未使用官方 ``mcp`` SDK，避免为一个协议解析引入额外依赖；协议本身足够简单，
  这样也便于说清楚每一层的实现。

用法::

    python mcp_server.py                      # 暴露全部工具（含沙箱与补丁）
    python mcp_server.py --profile review     # 只暴露只读工具
    python mcp_server.py --codebase-root .    # 额外启用代码库检索工具

在 Claude Desktop / Cursor 的 MCP 配置中登记::

    {
      "mcpServers": {
        "code-review-agent": {
          "command": "python",
          "args": ["C:/Projects/code_review/mcp_server.py", "--profile", "review"]
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from agent.tools import build_optimizer_registry, build_registry, build_reviewer_registry
from agent.tools.base import ToolRegistry
from agent.tools.pipeline_tools import ReviewCodeTool

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "code-review-agent"
SERVER_VERSION = "1.0.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    """日志只写 stderr —— stdout 是协议通道，混入任何其他内容都会破坏通信。"""
    sys.stderr.write(f"[mcp] {message}\n")
    sys.stderr.flush()


def build_tools(profile: str, codebase_root: Optional[str]) -> ToolRegistry:
    if profile == "review":
        registry = build_reviewer_registry(codebase_root)
    elif profile == "optimize":
        registry = build_optimizer_registry(codebase_root)
    else:
        registry = build_registry(codebase_root)
    # 多 Agent 主线：把「完整审查」本身暴露成一个工具，供任意 MCP 客户端调用。
    if profile in ("review", "all"):
        registry.register(ReviewCodeTool())
    return registry


class McpServer:
    """极简 MCP stdio 服务端。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.initialized = False

    # ------------------------------------------------------------------
    # 方法分发
    # ------------------------------------------------------------------

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """处理单条 JSON-RPC 消息；通知类消息返回 None（不响应）。"""
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "请求必须是 JSON 对象")

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if not isinstance(method, str):
            return None if is_notification else self._error(request_id, INVALID_REQUEST, "缺少 method 字段")

        try:
            if method == "initialize":
                return self._ok(request_id, self._initialize(params))
            if method in ("notifications/initialized", "initialized"):
                self.initialized = True
                return None
            if method == "ping":
                return self._ok(request_id, {})
            if method == "tools/list":
                return self._ok(request_id, {"tools": self._tools_list()})
            if method == "tools/call":
                return self._ok(request_id, self._tools_call(params))
            if method in ("resources/list", "prompts/list"):
                # 声明不支持的能力时，返回空集合比报错更友好
                key = "resources" if method.startswith("resources") else "prompts"
                return self._ok(request_id, {key: []})
            if method.startswith("notifications/"):
                return None
            return None if is_notification else self._error(
                request_id, METHOD_NOT_FOUND, f"未实现的方法: {method}"
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都要转成 JSON-RPC 错误而不是崩溃
            log(f"处理 {method} 时异常: {type(exc).__name__}: {exc}")
            return None if is_notification else self._error(
                request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------
    # 各方法实现
    # ------------------------------------------------------------------

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_info = params.get("clientInfo") or {}
        log(
            f"initialize: client={client_info.get('name')} "
            f"protocol={params.get('protocolVersion')} tools={len(self.registry)}"
        )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_list(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        for name in self.registry.names():
            tool = self.registry.get(name)
            if tool is None:
                continue
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.parameters,
                }
            )
        return tools

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call 需要 name 参数")
        if not isinstance(arguments, dict):
            raise ValueError("arguments 必须是 JSON 对象")

        tool = self.registry.get(name)
        if tool is None:
            # 未知工具应作为工具级错误返回（isError=true），而不是 JSON-RPC 错误，
            # 这样模型能看到错误并自行改用其他工具。
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"未知工具 '{name}'。可用工具：{', '.join(self.registry.names())}",
                    }
                ],
                "isError": True,
            }

        result = tool(**arguments)
        payload = {
            "tool": result.tool,
            "ok": result.ok,
            "duration_ms": round(result.duration_ms, 2),
        }
        if result.error:
            payload["error"] = result.error
        if result.data is not None:
            payload["data"] = result.data

        text = result.content or ""
        if result.data is not None:
            # 同时给出「人类可读文本 + 结构化数据」，便于客户端做程序化处理
            text = f"{text}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
        return {"content": [{"type": "text", "text": text}], "isError": not result.ok}

    # ------------------------------------------------------------------
    # JSON-RPC 响应封装
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(request_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _write(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve(registry: ToolRegistry) -> int:
    server = McpServer(registry)
    log(f"就绪，已注册 {len(registry)} 个工具: {', '.join(registry.names())}")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": str(exc)}})
            continue

        if isinstance(message, list):
            responses = [server.handle(item) for item in message]
            responses = [r for r in responses if r is not None]
            if responses:
                _write(responses)
            continue

        response = server.handle(message)
        if response is not None:
            _write(response)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="代码审查 Agent 的 MCP Server（stdio）")
    parser.add_argument(
        "--profile",
        choices=["review", "optimize", "all"],
        default="all",
        help="review=只读工具（推荐给编辑器集成）；optimize=含补丁与沙箱；all=全部",
    )
    parser.add_argument("--codebase-root", default="", help="代码库根目录，启用检索类工具")
    parser.add_argument("--list", action="store_true", help="仅打印工具清单后退出（自检用）")
    args = parser.parse_args()

    registry = build_tools(args.profile, args.codebase_root or None)
    if args.list:
        print(json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "profile": args.profile,
                "tools": [{"name": t.name, "mutating": t.mutating} for t in
                          (registry.get(n) for n in registry.names()) if t],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    return serve(registry)


if __name__ == "__main__":
    raise SystemExit(main())
