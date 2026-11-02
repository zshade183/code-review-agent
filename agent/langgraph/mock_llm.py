"""确定性 LangGraph 审查图的离线聊天模型。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent.langgraph.prompts import CODE_MARKER, NODE_MARKER


def _text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else "".join(str(item) for item in content)


def _system(messages: Sequence[BaseMessage]) -> str:
    return next((_text(item) for item in messages if getattr(item, "type", "") == "system"), "")


def _node(system: str) -> str:
    match = re.search(re.escape(NODE_MARKER) + r"\s*(\S+)", system)
    return match.group(1) if match else ""


def _evidence(system: str) -> Dict[str, Any]:
    marker = "【公共证据】"
    index = system.find(marker)
    if index == -1:
        return {}
    raw = system[index + len(marker):].lstrip()
    try:
        value, _end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _code(messages: Sequence[BaseMessage]) -> str:
    user = "\n".join(_text(item) for item in messages if getattr(item, "type", "") == "human")
    marker = user.find(CODE_MARKER)
    if marker == -1:
        return user
    text = user[marker + len(CODE_MARKER):]
    lines = []
    for line in text.splitlines():
        match = re.match(r"\s*\d+\|\s?(.*)", line)
        if match:
            lines.append(match.group(1))
    return "\n".join(lines)


def _normalize(item: Dict[str, Any]) -> Dict[str, Any]:
    line = int(item.get("line_start") or item.get("line_number") or 1)
    category = str(item.get("category") or "Potential Bug")
    subtype = str(item.get("subtype") or item.get("violated_rule") or "Unspecified")
    return {
        "category": category,
        "severity": item.get("severity") or "Medium",
        "subtype": subtype,
        "line_start": line,
        "line_end": int(item.get("line_end") or line),
        "violated_rule": item.get("violated_rule") or subtype,
        "risk_description": item.get("risk_description") or "[mock] 公共证据告警",
        "fix_suggestion": item.get("fix_suggestion") or "根据证据修复",
        "confidence": float(item.get("confidence") or 0.7),
        "evidence": [{
            "kind": "code_snippet",
            "line": line,
            "snippet": subtype,
            "detail": "[mock] 源码片段证据",
        }],
    }


class MockChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "langgraph-review-mock"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        system = _system(messages)
        node = _node(system)
        if node == "verification_agent":
            ids = sorted(set(re.findall(r"cand-[0-9a-f]+", "\n".join(_text(item) for item in messages))))
            payload = {
                "reviews": [
                    {
                        "candidate_id": item,
                        "verdict": "supported",
                        "reason": "[mock] 独立复核支持",
                        "evidence_refs": [],
                        "code_lines": [],
                        "missing_context": [],
                    }
                    for item in ids
                ]
            }
        elif node == "review_agent":
            from agent.tools.static_analysis import run_rules

            findings = run_rules(_code(messages))
            payload = {"issues": [_normalize(item.model_dump(mode="json")) for item in findings]}
        else:
            wanted = {
                "security_agent": "Security Vulnerability",
                "bug_agent": "Potential Bug",
                "quality_agent": "Code Smell",
                "performance_agent": "Performance Issue",
            }.get(node)
            findings = _evidence(system).get("rule_findings") or []
            issues = [
                _normalize(item) for item in findings
                if isinstance(item, dict) and (wanted is None or str(item.get("category") or "") == wanted)
            ]
            payload = {"issues": issues}
        message = AIMessage(content=json.dumps(payload, ensure_ascii=False))
        return ChatResult(generations=[ChatGeneration(message=message)])


__all__ = ["MockChatModel"]
