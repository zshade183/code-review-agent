"""请求级证据记录、缓存与引用校验。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple

from pydantic import BaseModel, ConfigDict, Field

from agent.schema import ToolResult
from agent.tools.base import ToolRegistry

'''
证据记录模型。
包含工具调用的详细信息，包括工具名称、参数、引擎、结果等。

:param evidence_id: 证据记录ID。
:param source_hash: 代码哈希值。
:param producer_agent: 生成证据的智能体。
:param tool_name: 工具名称。
:param engine: 工具引擎。
:param arguments: 工具调用的详细信息。
:param ok: 是否成功执行。
:param content: 工具输出。
:param error: 错误信息。
:param from_cache: 是否从缓存中获取。
:param data: 工具调用数据。
:param error_type: 错误类型。
:param retryable: 是否可重试。
:param attempts: 尝试次数。
:'''
class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_id: str
    source_hash: str
    producer_agent: str
    tool_name: str
    engine: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)
    ok: bool = False
    content: str = ""
    error: str = ""
    from_cache: bool = False
    data: Any = None
    error_type: str = ""
    retryable: bool = False
    attempts: int = 1

'''
计算代码哈希值。
:param code: 代码。
:return: 代码哈希值。
'''
def source_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

'''
创建稳定的工具调用负载。
:param tool_name: 工具名称。
:param arguments: 工具调用的详细信息。
:param code_hash: 代码哈希值。
:return: 稳定的工具调用负载。
'''
def _stable_payload(tool_name: str, arguments: Dict[str, Any], code_hash: str) -> str:
    sanitized = dict(arguments)
    if "code" in sanitized:
        sanitized["code"] = f"sha256:{code_hash}"
    return json.dumps(
        {"tool": tool_name, "arguments": sanitized, "source_hash": code_hash},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )

'''
证据服务。
包含证据记录的缓存、引用校验和执行。
'''
class EvidenceService:
    """同一次请求内，相同工具和输入只执行一次。"""

    def __init__(
        self,
        registry: ToolRegistry,
        code: str,
        records: Dict[str, Dict[str, Any]] | None = None,
    ) -> None:
        self.registry = registry
        self.code = code
        self.code_hash = source_hash(code)
        self.records: Dict[str, EvidenceRecord] = {}
        for key, value in (records or {}).items():
            try:
                self.records[key] = EvidenceRecord.model_validate(value)
            except Exception:
                continue

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        producer_agent: str,
    ) -> Tuple[ToolResult, EvidenceRecord]:
        payload = _stable_payload(tool_name, arguments, self.code_hash)
        evidence_id = "ev-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        existing = self.records.get(evidence_id)
        if existing is not None and existing.ok:
            cached = existing.model_copy(update={"producer_agent": producer_agent, "from_cache": True})
            result = ToolResult(
                tool=tool_name,
                ok=cached.ok,
                content=cached.content,
                error=cached.error,
                from_cache=True,
                data=cached.data,
                attempts=0,
            )
            return result, cached

        tool = self.registry.get(tool_name)
        if tool is None:
            result = ToolResult(
                tool=tool_name,
                ok=False,
                error=f"未知或无权使用工具 {tool_name}",
                content=f"未知或无权使用工具 {tool_name}",
            )
        else:
            result = tool(**arguments)
        engine = str(arguments.get("engines") or tool_name.replace("run_", "").replace("_analysis", ""))
        record = EvidenceRecord(
            evidence_id=evidence_id,
            source_hash=self.code_hash,
            producer_agent=producer_agent,
            tool_name=tool_name,
            engine=engine,
            arguments={key: value for key, value in arguments.items() if key != "code"},
            ok=bool(result.ok),
            content=result.content[:8000],
            error=result.error,
            data=result.data,
            error_type=result.error_type,
            retryable=result.retryable,
            attempts=result.attempts,
        )
        self.records[evidence_id] = record
        return result, record

    def export(self) -> Dict[str, Dict[str, Any]]:
        return {key: value.model_dump(mode="json") for key, value in self.records.items()}

    def successful_ids(self) -> set[str]:
        return {key for key, value in self.records.items() if value.ok}


__all__ = ["EvidenceRecord", "EvidenceService", "source_hash"]
