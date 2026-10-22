"""离线假模型（沿用 ``agent/mock.py`` 的思路，但适配 LangChain 的聊天模型接口）。

作用：没有 API Key、也不想消耗额度时，仍然能端到端跑通整张图——
主助手委派 → 子助手取证 → 收集校验 → 汇总，以及修复链路上的人工确认中断。

它不是「返回一份假报告」，而是**按节点逐步模拟一次真实的行为轨迹**，
因此能覆盖路由、状态合并、证据校验、interrupt 恢复等分支。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from review_assistant.dimensions import (
    ASSISTANT_CATEGORY,
    ASSISTANT_LABEL,
    DIMENSION_ASSISTANTS,
    DIMENSION_ENGINES,
    HANDOFF_TARGET,
    PRIMARY_ASSISTANT,
)
from review_assistant.prompts import CODE_MARKER, NODE_MARKER, PROGRESS_MARKER

_CODE_FENCE_RE = re.compile(r"```python\n(.*?)```", re.S)
_LINE_PREFIX_RE = re.compile(r"^\s*\d+\|\s?", re.M)
_LABEL_TO_ASSISTANT = {label: name for name, label in ASSISTANT_LABEL.items()}
_HANDOFF_OF_ASSISTANT = {assistant: tool for tool, assistant in HANDOFF_TARGET.items()}


def _text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):  # 多模态消息
        return "".join(str(part) for part in content)
    return str(content or "")


def _system_content(messages: Sequence[BaseMessage]) -> str:
    for message in messages:
        if getattr(message, "type", "") == "system":
            return _text(message)
    return ""


def _last_human_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(list(messages)):
        if getattr(message, "type", "") == "human":
            return _text(message)
    return ""


def _current_node(system: str) -> str:
    match = re.search(re.escape(NODE_MARKER) + r"\s*(\S+)", system)
    return match.group(1) if match else ""


def _extract_code(system: str) -> str:
    index = system.find(CODE_MARKER)
    if index == -1:
        return ""
    match = _CODE_FENCE_RE.search(system[index:])
    if not match:
        return ""
    return _LINE_PREFIX_RE.sub("", match.group(1))


def _tool_payloads(messages: Sequence[BaseMessage]) -> List[Any]:
    payloads: List[Any] = []
    for message in messages:
        if getattr(message, "type", "") != "tool":
            continue
        try:
            payloads.append(json.loads(_text(message)))
        except json.JSONDecodeError:
            continue
    return payloads


def _pending_dimensions(system: str) -> List[str]:
    """从系统消息的进度行里解析出尚未完成的维度助手。"""
    line = next((ln for ln in system.split("\n") if ln.startswith(PROGRESS_MARKER)), "")
    match = re.search(r"未完成：([^；\n]*)", line)
    if not match:
        return list(DIMENSION_ASSISTANTS)
    raw = match.group(1).strip()
    if not raw or raw.startswith("（"):
        return []
    pending = [_LABEL_TO_ASSISTANT.get(item.strip(), "") for item in raw.split("、")]
    return [name for name in pending if name in DIMENSION_ASSISTANTS]


def _completed(system: str) -> List[str]:
    line = next((ln for ln in system.split("\n") if ln.startswith(PROGRESS_MARKER)), "")
    match = re.search(r"已完成：([^；\n]*)", line)
    if not match:
        return []
    raw = match.group(1).strip()
    if not raw or raw.startswith("（"):
        return []
    return [name for name in (_LABEL_TO_ASSISTANT.get(i.strip(), "") for i in raw.split("、")) if name]


def _call(name: str, arguments: Dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _tool_count(messages: Sequence[BaseMessage]) -> int:
    """统计「最近一次交接之后」的工具结果条数。

    enter 节点写入的交接消息本身也是 ToolMessage，必须排除，否则助手会误以为
    自己已经取过证，直接跳到下一步。
    """
    count = 0
    for message in reversed(list(messages)):
        if getattr(message, "type", "") != "tool":
            continue
        if _text(message).startswith("已转交"):
            break
        count += 1
    return count


def _normalize_issue(item: Dict[str, Any], category: str) -> Optional[Dict[str, Any]]:
    line = item.get("line_start") or item.get("line_number") or 1
    try:
        line = int(str(line).split(",")[0])
    except (TypeError, ValueError):
        line = 1
    subtype = item.get("subtype") or item.get("violated_rule") or "Unspecified"
    return {
        "category": item.get("category") or category,
        "severity": item.get("severity") or "Medium",
        "subtype": subtype,
        "line_start": line,
        "line_end": int(item.get("line_end") or line),
        "violated_rule": item.get("violated_rule") or subtype,
        "risk_description": item.get("risk_description") or "静态分析告警（mock 观察）",
        "fix_suggestion": item.get("fix_suggestion") or "按证据修复该处逻辑",
        "confidence": 0.7,
        "evidence": [
            {
                "kind": "tool_output",
                "tool": "run_static_analysis",
                "line": line,
                "snippet": str(subtype),
                "detail": "来自静态分析工具的原始告警",
            }
        ],
    }


class MockChatModel(BaseChatModel):
    """按图节点逐步给出「下一动作」的假模型。"""

    _tool_names: List[str] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "review-assistant-mock"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "MockChatModel":  # noqa: D102 - 覆写基类
        names: List[str] = []
        for tool in tools:
            name = getattr(tool, "name", None)
            if not name and isinstance(tool, dict):
                name = (tool.get("function") or {}).get("name")
            if name:
                names.append(str(name))
        self._tool_names = names
        return self

    # ------------------------------------------------------------------

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        system = _system_content(messages)
        node = _current_node(system)
        code = _extract_code(system)

        if node == PRIMARY_ASSISTANT:
            message = self._primary(messages, system)
        elif node in ASSISTANT_CATEGORY:
            message = self._dimension(messages, system, node, code)
        elif node == "fix_assistant":
            message = self._fix(messages, code)
        else:
            message = AIMessage(content="[mock] 未识别的节点，直接结束。")

        return ChatResult(generations=[ChatGeneration(message=message)])

    # ------------------------------------------------------------------

    def _primary(self, messages: Sequence[BaseMessage], system: str) -> AIMessage:
        pending = _pending_dimensions(system)
        done = _completed(system)
        request = _last_human_text(messages)

        wants_fix = any(word in request for word in ("修复", "优化", "改一下", "改进"))
        if wants_fix and "fix_assistant" not in done:
            return _call(
                "ToFixAssistant",
                {"request": "按已确认的问题清单修复代码"},
                "mock_handoff_fix",
            )

        if pending:
            assistant = pending[0]
            return _call(
                _HANDOFF_OF_ASSISTANT[assistant],
                {"request": f"请审查该文件在{ASSISTANT_LABEL[assistant]}范围内的所有问题"},
                f"mock_handoff_{assistant}",
            )

        summary = (
            "[mock] 各维度审查已完成。"
            "本次结论均来自静态分析证据，修复建议见结构化报告；"
            "沙箱验证相关的结论需要用户确认后才能执行。"
        )
        return AIMessage(content=summary)

    def _dimension(
        self,
        messages: Sequence[BaseMessage],
        system: str,
        node: str,
        code: str,
    ) -> AIMessage:
        category = ASSISTANT_CATEGORY[node].value
        if _tool_count(messages) == 0 and "run_static_analysis" in self._tool_names:
            return _call(
                "run_static_analysis",
                {"code": code, "engines": DIMENSION_ENGINES.get(node, "rules")},
                f"mock_static_{node}",
            )

        issues: List[Dict[str, Any]] = []
        for payload in reversed(_tool_payloads(messages)):
            if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
                for item in payload["issues"]:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("category") or "") != category:
                        continue
                    normalized = _normalize_issue(item, category)
                    if normalized is not None:
                        issues.append(normalized)
                break

        payload = {
            "dimension": category,
            "summary": f"[mock] {category}：共 {len(issues)} 条（基于静态分析证据）",
            "issues": issues,
        }
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))

    def _fix(self, messages: Sequence[BaseMessage], code: str) -> AIMessage:
        steps = _tool_count(messages)

        if steps == 0 and "check_syntax" in self._tool_names:
            return _call("check_syntax", {"code": code}, "mock_fix_syntax")

        if steps == 1 and "execute_python_code" in self._tool_names:
            # 这一步会触发工作流的敏感节点，停下来请用户确认
            return _call(
                "execute_python_code",
                {"code": code, "timeout": 5, "strict": False},
                "mock_fix_exec",
            )

        payload = {
            "summary": "[mock] 未产生真实修复，返回原代码（用于验证修复链路本身）。",
            "fixed_code": code,
            "addressed_issues": [],
            "verification_note": "[mock] 已执行语法检查；沙箱执行需用户确认。",
        }
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))
