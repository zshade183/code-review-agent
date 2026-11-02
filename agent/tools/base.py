"""工具抽象与注册表。

Agent 的关键在于「LLM 自主决定调哪个工具」。因此每个能力都需要三要素：

1. ``name`` / ``description``：告诉模型这个工具干什么、什么时候该用
2. ``parameters``：JSON Schema，约束模型传参（原生 function calling 直接消费）
3. ``run``：真正执行并返回**可观察**的结果

注册表同时负责生成文本版工具说明，供不支持 function calling 的模型走文本 ReAct。
"""

from __future__ import annotations

import json
import logging
import time
import subprocess
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Type

from agent.schema import ToolResult

logger = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """工具参数非法或执行失败。"""


class Tool(ABC):
    """所有可被 LLM 调用能力的基类。"""

    #: 工具名（模型通过该名称调用，需为英文 snake_case）
    name: str = ""
    #: 给模型看的说明，务必写清「何时使用」
    description: str = ""
    #: 参数的 JSON Schema
    parameters: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    #: 该工具是否可能产生副作用（执行代码 / 改写代码），用于把「纯只读分析」与
    #: 「会执行或改写产物」的能力区分开；MCP 客户端与运行时审计据此决定是否需要人工确认
    mutating: bool = False
    # Only explicitly idempotent tools may retry automatically.
    retry_safe: bool = False
    max_attempts: int = 2
    retry_delay_s: float = 0.1
    result_fields: Dict[str, type] | None = None

    def validate_result(self, result: ToolResult) -> None:
        """Override to validate a tool-specific data contract before accepting evidence."""
        if not isinstance(result, ToolResult):
            raise ValueError("工具必须返回 ToolResult")
        if result.ok and self.result_fields is not None:
            if not isinstance(result.data, dict):
                raise ValueError("工具 data 必须是对象")
            for key, expected in self.result_fields.items():
                if key not in result.data or not isinstance(result.data[key], expected):
                    raise ValueError(f"工具 data.{key} 必须为 {expected.__name__}")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name and not cls.description:
            raise TypeError(f"工具 {cls.__name__} 必须提供 description")

    # ---------- 对外协议 ----------

    def openai_schema(self) -> Dict[str, Any]:
        """转成 OpenAI 兼容的 tools 声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def text_spec(self) -> str:
        """文本版说明（供文本 ReAct 降级模式使用）。"""
        props = self.parameters.get("properties") or {}
        required = set(self.parameters.get("required") or [])
        arg_lines = []
        for key, spec in props.items():
            flag = "必填" if key in required else "可选"
            arg_lines.append(
                f"    - {key} ({spec.get('type', 'any')}, {flag}): {spec.get('description', '')}"
            )
        args_text = "\n".join(arg_lines) if arg_lines else "    - 无参数"
        return f"- {self.name}: {self.description}\n{args_text}"

    def validate_arguments(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """轻量参数校验：必填项、类型粗检、默认值填充。

        这里刻意不引入 jsonschema 依赖，只做必要的边界校验（够用且零成本）。
        """
        if not isinstance(arguments, dict):
            raise ToolError("工具参数必须是 JSON 对象")

        props = self.parameters.get("properties") or {}
        required = list(self.parameters.get("required") or [])

        unknown = [k for k in arguments if k not in props and props]
        if unknown:
            logger.info("工具 %s 收到未声明参数: %s（已忽略）", self.name, unknown)

        cleaned: Dict[str, Any] = {}
        for key, spec in props.items():
            if key in arguments and arguments[key] is not None:
                value = arguments[key]
            elif "default" in spec:
                value = spec["default"]
            elif key in required:
                raise ToolError(f"缺少必填参数 '{key}'")
            else:
                continue
            cleaned[key] = self._coerce(value, spec, key)
        return cleaned

    def _coerce(self, value: Any, spec: Dict[str, Any], key: str) -> Any:
        """按 schema 做一次宽容的类型转换（LLM 常见把数字写成字符串）。"""
        expected = spec.get("type")
        try:
            if expected == "integer" and not isinstance(value, bool):
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError("不能截断小数")
                return int(value)
            if expected == "number" and not isinstance(value, bool):
                return float(value)
            if expected == "boolean" and isinstance(value, str):
                normalized = value.strip().lower()
                if normalized not in {"1", "true", "yes", "y", "on", "0", "false", "no", "n", "off"}:
                    raise ValueError("非法布尔值")
                return normalized in {"1", "true", "yes", "y", "on"}
            if expected == "string" and not isinstance(value, str):
                return str(value)
            if expected == "array" and isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
        except (TypeError, ValueError) as exc:
            raise ToolError(f"参数 '{key}' 期望 {expected}，但无法解析 {value!r}: {exc}") from exc
        types = {"integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict, "string": str}
        if expected in types and (not isinstance(value, types[expected]) or
                                  (expected in {"integer", "number"} and isinstance(value, bool))):
            raise ToolError(f"参数 '{key}' 期望 {expected}")
        return value

    def __call__(self, **arguments: Any) -> ToolResult:
        """Validate inputs, classify failures and retry explicitly safe transient failures."""
        started = time.perf_counter()
        try:
            cleaned = self.validate_arguments(arguments)
        except ToolError as exc:
            return ToolResult(
                tool=self.name, ok=False, error=str(exc),
                error_type="invalid_arguments", attempts=0,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        limit = max(1, min(self.max_attempts, 3)) if self.retry_safe and not self.mutating else 1
        for attempt in range(1, limit + 1):
            try:
                result = self.run(**cleaned)
                try:
                    self.validate_result(result)
                except Exception as exc:
                    result = ToolResult(tool=self.name, ok=False, error=str(exc), error_type="invalid_output")
            except (TimeoutError, ConnectionError, subprocess.TimeoutExpired) as exc:
                result = ToolResult(tool=self.name, ok=False, error=str(exc), error_type="transient_error", retryable=True)
            except PermissionError as exc:
                result = ToolResult(tool=self.name, ok=False, error=str(exc), error_type="permission_denied")
            except Exception as exc:
                logger.exception("工具 %s 执行异常", self.name)
                result = ToolResult(tool=self.name, ok=False, error=f"{type(exc).__name__}: {exc}", error_type="execution_error")
            result.tool = self.name
            result.attempts = attempt
            if result.ok or not result.retryable or attempt == limit:
                break
            time.sleep(max(0, min(self.retry_delay_s, 1)) * 2 ** (attempt - 1))
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """真正执行工具逻辑。"""

    # ---------- 便捷构造 ----------

    @staticmethod
    def ok(content: str, data: Any = None) -> ToolResult:
        return ToolResult(tool="", ok=True, content=content, data=data)

    @staticmethod
    def fail(message: str) -> ToolResult:
        return ToolResult(tool="", ok=False, error=message, content=message)


class ToolRegistry:
    """工具注册表。"""

    def __init__(self, tools: Optional[Sequence[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> "ToolRegistry":
        if not tool.name:
            raise ValueError(f"工具 {type(tool).__name__} 未定义 name")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复: {tool.name}")
        self._tools[tool.name] = tool
        return self

    def register_class(self, tool_cls: Type[Tool]) -> "ToolRegistry":
        return self.register(tool_cls())

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def subset(self, names: Sequence[str]) -> "ToolRegistry":
        """按白名单裁剪工具集（不同 Agent 角色只应看到自己需要的工具）。"""
        registry = ToolRegistry()
        missing = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                missing.append(name)
                continue
            registry.register(tool)
        if missing:
            raise KeyError(f"未注册的工具: {missing}")
        # 自省类工具需要重新绑定，否则它会列出被裁剪掉的能力，误导模型
        for tool in registry._tools.values():
            binder = getattr(tool, "bind_registry", None)
            if callable(binder):
                binder(registry)
        return registry

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def text_spec(self) -> str:
        return "\n".join(tool.text_spec() for tool in self._tools.values())

    def describe(self) -> str:
        return json.dumps(
            [
                {"name": t.name, "mutating": t.mutating, "description": t.description}
                for t in self._tools.values()
            ],
            ensure_ascii=False,
            indent=2,
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
