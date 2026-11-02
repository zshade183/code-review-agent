"""Verification Agent 的角色提示词。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from agent.langgraph.prompts import NODE_MARKER
from agent.schema import Issue


def _numbered(code: str) -> str:
    return "\n".join(f"{index}| {line}" for index, line in enumerate(code.splitlines(), 1))


def build_verification_prompt(issues: List[Issue], code: str,
                              context: Dict[str, Any]) -> Tuple[str, str]:
    candidate_ids = [item.ensure_candidate_id() for item in issues]
    system = f"""{NODE_MARKER} verification_agent
你是独立 Verification Agent。逐条检查候选成立条件，主动寻找保护条件和反证；需要时调用只读工具补充上下文。
只输出 JSON：{{"reviews":[{{"candidate_id":"...","verdict":"supported|refuted|insufficient","reason":"...","evidence_refs":[],"code_lines":[],"missing_context":[]}}]}}。
supported 和 refuted 都必须引用有效 evidence_id 或明确源码行；信息不足必须用 insufficient。
待复核 ID：{json.dumps({"candidate_ids": candidate_ids, "candidate_id": candidate_ids[0] if candidate_ids else ""}, ensure_ascii=False)}
基础上下文：{json.dumps(context, ensure_ascii=False, default=str)}"""
    user = f"候选：{json.dumps([item.to_legacy() for item in issues], ensure_ascii=False)}\n源码：\n{_numbered(code)}"
    return system, user


__all__ = ["build_verification_prompt"]
