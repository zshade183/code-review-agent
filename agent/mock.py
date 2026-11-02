"""离线 Mock 后端。

用途：在没有 API Key / 不想消耗额度时，仍能端到端跑通 Agent Loop、工具调用、
结构化输出校验与轨迹渲染。Mock 会模拟一条**完整的 Agent 行为轨迹**
（先调工具、再基于观察结果产出最终报告），而不是直接返回一段假数据，
这样才能真正覆盖 runtime 的每一步分支。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from agent.llm import LLMResponse, Message, StreamEvent
from agent.schema import TokenUsage

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
#: 提示词里的代码带有 "12| " 形式的行号前缀，喂给工具前必须去掉
_LINE_PREFIX_RE = re.compile(r"^\s*\d+\|\s?", re.M)
#: 领域 Reviewer 的 system prompt 里带有「审查维度」标记，mock 据此收敛输出类别
_REVIEW_CATEGORY_RE = re.compile(r"【审查维度】\s*([^\n（(]+)")
_SHARED_FINDING_RE = re.compile(
    r"·\s*行\s*(\d+):\s*\[([^\]]+)\]\s*([^\n（]+)（([^)）]+)[)）]"
)


def _prompt_review_category(messages: Sequence[Message]) -> str:
    for msg in messages:
        if msg.get("role") != "system":
            continue
        match = _REVIEW_CATEGORY_RE.search(str(msg.get("content") or ""))
        if match:
            return match.group(1).strip()
    return ""


def _strip_line_numbers(code: str) -> str:
    return _LINE_PREFIX_RE.sub("", code)


def _extract_code(messages: Sequence[Message]) -> str:
    """从提示词中取出待处理代码（提示词里代码放在围栏代码块中）。"""
    for msg in reversed(list(messages)):
        if msg.get("role") != "user":
            continue
        match = _CODE_FENCE_RE.search(str(msg.get("content") or ""))
        if match:
            return _strip_line_numbers(match.group(1))
    return ""


def _best_tool_payload(messages: Sequence[Message]) -> Optional[Any]:
    """取出最「有用」的一份工具结果：优先返回带 issues 列表的那份。

    Agent 可能调用了多个工具，最终作答时应基于信息量最大的那份观察结果，
    这也符合真实模型的行为（不会只看最后一个工具的输出）。
    """
    payloads: List[Any] = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        try:
            payloads.append(json.loads(str(msg.get("content") or "")))
        except json.JSONDecodeError:
            continue
    for payload in reversed(payloads):
        if isinstance(payload, dict) and isinstance(payload.get("issues"), list) and payload["issues"]:
            return payload
    for payload in reversed(payloads):
        if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
            return payload
    for payload in reversed(payloads):
        if isinstance(payload, list):
            return payload
    return payloads[-1] if payloads else None


def _count_tool_messages(messages: Sequence[Message]) -> int:
    return sum(1 for m in messages if m.get("role") == "tool")


def _shared_evidence_payload(prompt_text: str) -> Optional[Dict[str, Any]]:
    """从领域 Reviewer 提示词中还原 Collector 已注入的候选，供离线 mock 使用。"""
    issues: List[Dict[str, Any]] = []
    for match in _SHARED_FINDING_RE.finditer(prompt_text):
        issues.append(
            {
                "line_start": int(match.group(1)),
                "category": match.group(2).strip(),
                "subtype": match.group(3).strip(),
                "severity": match.group(4).strip(),
                "risk_description": "来自共享 Evidence Collector 的静态分析候选",
                "fix_suggestion": "结合代码语义修复该问题",
            }
        )
    return {"issues": issues} if issues else None


def _usage(prompt: str, completion: str) -> TokenUsage:
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(completion) // 4)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _build_final_report(tool_payload: Any, code: str, category_filter: str = "") -> str:
    """根据工具观察结果产出最终报告（模拟模型基于证据作答）。

    ``category_filter`` 非空时只保留该一级分类——用于模拟领域 Reviewer 的
    「只报自己维度」行为，让离线测试能覆盖分流逻辑。
    """
    issues: List[Dict[str, Any]] = []
    if isinstance(tool_payload, list):
        candidates = tool_payload
    elif isinstance(tool_payload, dict):
        candidates = tool_payload.get("issues") or []
    else:
        candidates = []

    total_lines = max(1, len(code.split("\n")))
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if category_filter and str(item.get("category") or "") != category_filter:
            continue
        line = item.get("line_start") or item.get("line_number") or 1
        try:
            line = max(1, min(total_lines, int(str(line).split(",")[0])))
        except (TypeError, ValueError):
            line = 1
        issues.append(
            {
                "category": item.get("category") or "Potential Bug",
                "severity": item.get("severity") or "Medium",
                "subtype": item.get("subtype") or "Logical Condition Error",
                "line_start": line,
                "line_end": line,
                "violated_rule": item.get("violated_rule") or item.get("subtype") or "",
                "risk_description": item.get("risk_description") or "mock 观察到的静态分析告警",
                "fix_suggestion": item.get("fix_suggestion") or "按证据修复该处逻辑",
                "confidence": 0.7,
                "evidence": [
                    {
                        "kind": "tool_output",
                        "tool": "run_static_analysis",
                        "line": line,
                        "snippet": str(item.get("subtype") or ""),
                        "detail": "来自静态分析工具的原始告警",
                    }
                ],
            }
        )

    summary = f"[mock] 共发现 {len(issues)} 个问题" if issues else "[mock] 未发现明确问题"
    return json.dumps({"review_summary": summary, "issues": issues}, ensure_ascii=False)


def mock_chat(
    messages: Sequence[Message],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> LLMResponse:
    """模拟一次 LLM 调用，返回工具调用或最终答案。"""
    time.sleep(0.01)
    prompt_text = "\n".join(str(m.get("content") or "") for m in messages)
    tool_names = {
        (t.get("function") or {}).get("name")
        for t in (tools or [])
        if isinstance(t, dict)
    }
    observed = _count_tool_messages(messages)
    code = _extract_code(messages)
    category_filter = _prompt_review_category(messages)

    # 优化任务：走「先验证 -> 再交付代码」的轨迹
    if "create_patch" in tool_names or "run_tests" in tool_names:
        return _mock_optimize(prompt_text, code, tool_names, observed)

    # 第 1 步：调用静态分析工具（模拟 Agent 先取证）
    if observed == 0 and "run_static_analysis" in tool_names:
        content = "需要先获取客观证据，调用静态分析工具扫描这段代码。"
        return LLMResponse(
            content=content,
            tool_calls=[
                _make_call("run_static_analysis", {"code": code, "engines": "rules"}, "call_mock_1")
            ],
            usage=_usage(prompt_text, content),
            model="mock",
            finish_reason="tool_calls",
        )

    # 第 2 步：补充 AST 结构信息
    if observed == 1 and "get_ast_outline" in tool_names and code:
        content = "获取代码结构，确认问题所在函数与作用域。"
        return LLMResponse(
            content=content,
            tool_calls=[_make_call("get_ast_outline", {"code": code}, "call_mock_2")],
            usage=_usage(prompt_text, content),
            model="mock",
            finish_reason="tool_calls",
        )

    # 第 3 步：基于证据产出结构化报告
    tool_payload = _best_tool_payload(messages)
    if tool_payload is None:
        tool_payload = _shared_evidence_payload(prompt_text)
    final = _build_final_report(tool_payload, code, category_filter)
    return LLMResponse(
        content=final,
        usage=_usage(prompt_text, final),
        model="mock",
        finish_reason="stop",
    )


def _mock_optimize(
    prompt_text: str,
    code: str,
    tool_names: set,
    observed: int,
) -> LLMResponse:
    """优化任务的模拟轨迹：先做语法检查，再交付代码。

    说明：mock 不会真的产生修复（那需要真实模型），这里返回原代码，
    用于端到端验证「验证管线」本身是否工作正常。
    """
    if observed == 0 and "check_syntax" in tool_names:
        content = "先确认待修复代码的语法基线。"
        return LLMResponse(
            content=content,
            tool_calls=[_make_call("check_syntax", {"code": code}, "call_mock_opt_1")],
            usage=_usage(prompt_text, content),
            model="mock",
            finish_reason="tool_calls",
        )
    final = f"[mock] 未产生真实修复，返回原代码。\n```python\n{code}\n```"
    return LLMResponse(content=final, usage=_usage(prompt_text, final), model="mock", finish_reason="stop")


def _make_call(name: str, arguments: Dict[str, Any], call_id: str):
    from agent.schema import ToolCall

    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def mock_stream(_messages: Sequence[Message]) -> Iterator[StreamEvent]:
    """模拟流式输出（参数仅为与 ``LLMClient.chat_stream`` 签名一致）。"""
    text = "[mock] 正在分析代码……"
    for char in text:
        yield StreamEvent(type="delta", text=char)
        time.sleep(0.005)
    usage = _usage("", text)
    yield StreamEvent(type="usage", usage=usage)
    yield StreamEvent(type="done")
