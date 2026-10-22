"""把现有的 ``agent/tools`` 工具适配成 LangChain 工具。

项目内的工具是自定义的 ``Tool`` 抽象（``name`` / ``description`` / ``parameters`` /
``run``），而 LangGraph 的 ``ToolNode`` 需要 LangChain ``BaseTool``。这里做一层
薄适配，好处是**工具定义只有一份**：

- ``parameters``（JSON Schema）在运行时转成 Pydantic 模型，交给 LangChain 做参数校验
- ``mutating`` 标记原样保留，工作流据此把工具分成「只读」与「需确认」两类
- 执行结果统一转成字符串回填给模型，失败也变成可观察的文本而不是异常
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, create_model

_TYPE_MAP: Dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def build_args_schema(tool_name: str, parameters: Dict[str, Any]) -> Type[BaseModel]:
    """把工具的 JSON Schema 转成 Pydantic 模型。"""
    properties = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])

    fields: Dict[str, Any] = {}
    for key, spec in properties.items():
        spec = spec or {}
        annotation = _TYPE_MAP.get(str(spec.get("type") or "string"), str)
        description = str(spec.get("description") or "")
        if key in required:
            fields[key] = (annotation, Field(description=description))
        else:
            default = spec.get("default", None)
            fields[key] = (
                Optional[annotation],
                Field(default=default, description=description),
            )

    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
    return create_model(model_name, **fields)


class RegistryTool(BaseTool):
    """LangChain 形态的 ``agent.tools.base.Tool``。"""

    name: str
    description: str
    #: 原工具的副作用标记；工作流用它做「只读 / 需确认」分流
    mutating: bool = False
    args_schema: Type[BaseModel] = BaseModel

    _inner: Any = PrivateAttr()

    @classmethod
    def from_inner(cls, tool: Any) -> "RegistryTool":
        instance = cls(
            name=tool.name,
            description=tool.description,
            mutating=bool(getattr(tool, "mutating", False)),
            args_schema=build_args_schema(tool.name, getattr(tool, "parameters", {}) or {}),
        )
        instance._inner = tool
        return instance

    def _run(self, **kwargs: Any) -> str:  # noqa: D102 - 覆写基类
        result = self._inner(**kwargs)
        if getattr(result, "ok", False):
            return result.content or "（工具无输出）"
        return f"[工具执行失败] {getattr(result, 'error', '') or getattr(result, 'content', '')}"
