"""AST / 语法类工具。

让 Agent 能「看清代码结构」而不是只靠语言模型猜：函数边界、参数、复杂度、
定义与引用位置、语法是否合法、改动前后签名是否兼容。
"""

from __future__ import annotations

import ast
import json
from typing import Any, Dict, List

from agent.tools.base import Tool
from agent.schema import ToolResult


def _complexity(node: ast.AST) -> int:
    """粗略的圈复杂度：统计分支与循环节点数 + 1。"""
    branch_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.BoolOp, ast.IfExp)
    return 1 + sum(1 for n in ast.walk(node) if isinstance(n, branch_types))


def _func_label(node: ast.AST) -> str:
    return type(node).__name__


class GetAstOutlineTool(Tool):
    result_fields = {"syntax_ok": bool, "definitions": list}
    name = "get_ast_outline"
    description = (
        "解析 Python 代码的抽象语法树，返回函数与类的结构清单：名称、起止行号、参数、"
        "是否含文档字符串、圈复杂度、嵌套深度。在判断问题所在函数、评估改动影响范围时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "待解析的完整 Python 源码"},
            "max_items": {
                "type": "integer",
                "description": "最多返回多少个顶层/嵌套定义，默认 40",
                "default": 40,
            },
        },
        "required": ["code"],
    }

    def run(self, code: str, max_items: int = 40) -> ToolResult:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"语法错误：第 {exc.lineno} 行 {exc.msg}",
                content=f"无法解析 AST，代码存在语法错误：第 {exc.lineno} 行 {exc.msg}",
                data={"syntax_ok": False, "line": exc.lineno, "msg": exc.msg},
            )

        entries: List[Dict[str, Any]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start = int(getattr(node, "lineno", 1) or 1)
            end = int(getattr(node, "end_lineno", start) or start)
            item: Dict[str, Any] = {
                "kind": _func_label(node),
                "name": node.name,
                "line_start": start,
                "line_end": end,
                "lines": end - start + 1,
                "has_docstring": ast.get_docstring(node) is not None,
                "complexity": _complexity(node),
            }
            indent = getattr(node, "col_offset", 0)
            item["indent"] = indent
            item["nested_in_class"] = any(
                isinstance(p, ast.ClassDef)
                for p in ast.walk(tree)
                if node in ast.walk(p) and p is not node
            )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                item["args"] = [a.arg for a in list(args.posonlyargs) + list(args.args)]
                item["kwonly_args"] = [a.arg for a in args.kwonlyargs]
                item["vararg"] = args.vararg.arg if args.vararg else None
                item["kwarg"] = args.kwarg.arg if args.kwarg else None
                item["defaults_count"] = len(args.defaults)
                item["returns"] = ast.unparse(node.returns) if node.returns else None
            entries.append(item)

        entries.sort(key=lambda e: e["line_start"])
        truncated = len(entries) > max_items
        entries = entries[:max_items]

        top_level_imports = [
            ast.unparse(n)
            for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        payload = {
            "syntax_ok": True,
            "total_definitions": len(entries),
            "truncated": truncated,
            "definitions": entries,
            "top_level_imports": top_level_imports,
        }
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)


class CheckSyntaxTool(Tool):
    name = "check_syntax"
    description = (
        "校验一段 Python 代码能否通过编译（ast.parse）。在提出修改建议或生成修复代码后，"
        "必须先用本工具确认没有语法错误。"
    )
    parameters = {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "待校验的 Python 源码"}},
        "required": ["code"],
    }

    def run(self, code: str) -> ToolResult:
        try:
            ast.parse(code)
        except SyntaxError as exc:
            payload = {"syntax_ok": False, "line": exc.lineno, "column": exc.offset, "msg": exc.msg}
            return ToolResult(
                tool=self.name,
                ok=True,  # 工具本身执行成功；「代码不合法」是有效观察结果
                content=json.dumps(payload, ensure_ascii=False),
                data=payload,
            )
        except ValueError as exc:
            payload = {"syntax_ok": False, "line": None, "msg": str(exc)}
            return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)
        payload = {"syntax_ok": True}
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)


def _signature_map(code: str) -> Dict[str, Any]:
    tree = ast.parse(code)
    result: Dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            result[node.name] = {
                "kind": "function",
                "positional": [a.arg for a in list(args.posonlyargs) + list(args.args)],
                "kwonly": [a.arg for a in args.kwonlyargs],
                "vararg": args.vararg.arg if args.vararg else None,
                "kwarg": args.kwarg.arg if args.kwarg else None,
                "arity": len(args.posonlyargs) + len(args.args),
            }
        elif isinstance(node, ast.ClassDef):
            result[node.name] = {"kind": "class"}
    return result


class CheckSignatureTool(Tool):
    name = "check_signature"
    description = (
        "对比改动前后代码的公开接口（函数名、参数列表、参数个数、类名），判断是否破坏了"
        "调用方兼容性。在生成修复代码后必须用本工具验证「最小修改」约束是否被遵守。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "original_code": {"type": "string", "description": "原始代码"},
            "modified_code": {"type": "string", "description": "修改后的代码"},
        },
        "required": ["original_code", "modified_code"],
    }

    def run(self, original_code: str, modified_code: str) -> ToolResult:
        try:
            original = _signature_map(original_code)
            modified = _signature_map(modified_code)
        except SyntaxError as exc:
            payload = {"compatible": False, "reason": f"语法错误：第 {exc.lineno} 行 {exc.msg}"}
            return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)

        removed = [name for name in original if name not in modified]
        changed: List[Dict[str, Any]] = []
        for name, spec in original.items():
            other = modified.get(name)
            if other is None:
                continue
            if spec.get("kind") == "class":
                continue
            if (
                spec.get("positional") != other.get("positional")
                or spec.get("arity") != other.get("arity")
                or spec.get("vararg") != other.get("vararg")
                or spec.get("kwarg") != other.get("kwarg")
            ):
                changed.append({"name": name, "before": spec, "after": other})
        added = [name for name in modified if name not in original]

        compatible = not removed and not changed
        payload = {
            "compatible": compatible,
            "removed_symbols": removed,
            "changed_signatures": changed,
            "added_symbols": added,
            "note": "added_symbols 通常无害；removed_symbols 或 changed_signatures 会破坏调用方兼容性。",
        }
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)


class FindSymbolTool(Tool):
    result_fields = {"symbol": str, "definitions": list, "reference_lines": list}
    name = "find_symbol"
    description = (
        "在给定代码中查找某个标识符的定义位置与所有引用行号。当需要确认一个变量/函数"
        "在哪里被赋值、被谁调用时使用（例如核实「未使用变量」「重复代码」类结论）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python 源码"},
            "symbol": {"type": "string", "description": "要查找的标识符名称"},
        },
        "required": ["code", "symbol"],
    }

    def run(self, code: str, symbol: str) -> ToolResult:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"语法错误：第 {exc.lineno} 行 {exc.msg}",
                content=f"无法解析代码：第 {exc.lineno} 行 {exc.msg}",
            )

        definitions: List[Dict[str, Any]] = []
        stores: List[int] = []
        loads: List[int] = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                definitions.append(
                    {
                        "kind": type(node).__name__,
                        "line_start": node.lineno,
                        "line_end": getattr(node, "end_lineno", node.lineno),
                    }
                )
            elif isinstance(node, ast.Name) and node.id == symbol:
                if isinstance(node.ctx, ast.Store):
                    stores.append(node.lineno)
                else:
                    loads.append(node.lineno)

        payload = {
            "symbol": symbol,
            "definitions": definitions,
            "assignment_lines": sorted(set(stores)),
            "reference_lines": sorted(set(loads)),
            "is_unused": bool(stores) and not loads,
        }
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)


class ListToolsTool(Tool):
    """元工具：让 Agent 在文本 ReAct 模式下也能自省可用工具。"""

    name = "list_tools"
    description = "列出当前可用工具及其参数说明。仅在不清楚有哪些工具时调用。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    def bind_registry(self, registry: Any) -> None:
        self._registry = registry

    def run(self) -> ToolResult:
        if self._registry is None:
            return ToolResult(tool=self.name, ok=True, content="未绑定注册表")
        return ToolResult(tool=self.name, ok=True, content=self._registry.text_spec())
