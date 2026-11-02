"""Review → 条件 Verification → 规则裁决的 LangGraph 主线。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agent import settings
from agent.agents import (
    REVIEW_AGENT_CONFIGS,
    VERIFICATION_AGENT_CONFIG,
    build_review_prompt,
    build_verification_prompt,
)
from agent.evidence_service import EvidenceService
from agent.executors import ToolCallingExecutor
from agent.langgraph.findings import parse_json_object, validate_issues
from agent.langgraph.state import ReviewState
from agent.memory import MemoryStore, get_shared_memory
from agent.schema import AdjudicationStatus, Category, FindingCandidate, Issue, ReviewReport, Severity
from agent.tools.ast_tools import CheckSyntaxTool, GetAstOutlineTool
from agent.tools.base import ToolRegistry

VERBOSE = False


def _validated_output(llm: Any, content: str, field: str) -> Dict[str, Any]:
    """Allow one format-only repair; never interpret an invalid envelope as no findings."""
    for attempt in range(2):
        payload, error = parse_json_object(content)
        if payload is not None:
            if isinstance(payload.get(field), list) and all(isinstance(x, dict) for x in payload[field]):
                return payload
            error = f"{field} 必须存在且为对象列表"
        if attempt == 0:
            response = llm.invoke([
                SystemMessage(content=f"仅修复输出格式，返回包含 {field} 对象列表的 JSON。保留原有事实和结论，不得补造证据或将无法恢复的内容改成空列表。无法修复则返回错误对象。"),
                HumanMessage(content=f"校验错误：{error}\n原始输出：{content}"),
            ])
            content = str(getattr(response, "content", "") or "")
    raise ValueError(f"输出格式修复失败：{error}")


def _config(config: RunnableConfig) -> Dict[str, Any]:
    return dict((config or {}).get("configurable", {}) or {})


def _trace_recorder(config: RunnableConfig) -> Any:
    """取出 pipeline 注入的 TraceRecorder；未注入时返回 None。"""
    return ((config or {}).get("configurable") or {}).get("trace")


def load_target(_state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
    configurable = _config(config)
    code = str(configurable.get("target_code") or "")
    path = str(configurable.get("target_path") or "").strip()
    if not code:
        if not path:
            raise ValueError("缺少 target_code 或 target_path")
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"待审查文件不存在：{file_path}")
        code = file_path.read_text(encoding="utf-8", errors="replace")
    target = {
        "path": path or "snippet.py",
        "code": code,
        "line_count": max(1, len(code.splitlines()) or 1),
    }
    return {
        "target": target,
        "strategy": str(configurable.get("strategy") or "auto"),
        "project": str(configurable.get("project") or "default"),
        "use_memory": bool(configurable.get("use_memory", True)),
        "extra_context": str(configurable.get("extra_context") or ""),
        "events": [{"type": "load_target", "path": target["path"], "lines": target["line_count"]}],
    }


def prepare_context(state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
    started = time.perf_counter()
    code = str(state["target"]["code"])
    syntax = CheckSyntaxTool()(code=code)
    outline = GetAstOutlineTool()(code=code, max_items=60)
    errors: Dict[str, str] = {}
    memory_context = ""
    if state.get("use_memory", True):
        try:
            memory_context = get_shared_memory().build_context(
                code,
                project=state.get("project") or "default",
                session_id=str(_config(config).get("session_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            errors["memory"] = str(exc)
    context = {
        "syntax_ok": bool(syntax.ok and (syntax.data or {}).get("syntax_ok", True)),
        "syntax_detail": syntax.content,
        "ast": outline.data if outline.ok else {},
        "memory_context": memory_context,
        "extra_context": state.get("extra_context") or "",
        "shared_tools": ["check_syntax", "get_ast_outline"],
        "errors": errors,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return {"context": context, "events": [{"type": "context", "syntax_ok": context["syntax_ok"]}]}


def _registry_for(registry: ToolRegistry, names: tuple[str, ...]) -> ToolRegistry:
    return registry.subset([name for name in names if name in registry])


def _validate_mixed_issues(
    raw: List[Any], total_lines: int, called_tools: set[str], valid_evidence_ids: set[str]
) -> tuple[List[Dict[str, Any]], List[str]]:
    kept: List[Dict[str, Any]] = []
    notes: List[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            notes.append(f"第 {index} 条不是对象")
            continue
        try:
            category = Issue.model_validate(item).category
        except Exception as exc:  # noqa: BLE001
            notes.append(f"第 {index} 条字段无效：{exc}")
            continue
        valid, item_notes = validate_issues([item], category, total_lines, called_tools)
        notes.extend(item_notes)
        for payload in valid:
            issue = Issue.model_validate(payload)
            refs = set(issue.evidence_refs) | {e.evidence_id for e in issue.evidence if e.evidence_id}
            invalid = sorted(refs - valid_evidence_ids)
            unbound_tools = sorted({
                str(e.tool) for e in issue.evidence
                if e.tool and e.tool not in {"check_syntax", "get_ast_outline"} and not e.evidence_id
            })
            if invalid or unbound_tools:
                issue.verified = False
                issue.confidence = min(issue.confidence, 0.5)
                problems = []
                if invalid:
                    problems.append(f"无效证据引用：{','.join(invalid)}")
                if unbound_tools:
                    problems.append(f"工具证据缺少 evidence_id：{','.join(unbound_tools)}")
                issue.verify_note = "；".join(filter(None, [issue.verify_note, *problems]))
                notes.append(f"第 {index} 条工具证据未绑定真实调用")
            kept.append(issue.model_dump(mode="json"))
    return kept, notes


def make_review_agent(llm: Any, registry: ToolRegistry) -> Callable[[ReviewState], Dict[str, Any]]:
    def review(state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
        started = time.perf_counter()
        recorder = _trace_recorder(config)
        strategy = str(state.get("strategy") or "auto")
        role_config = REVIEW_AGENT_CONFIGS.get(strategy, REVIEW_AGENT_CONFIGS["auto"])
        role_registry = _registry_for(registry, role_config.tool_names)
        service = EvidenceService(role_registry, str(state["target"]["code"]), state.get("evidence_records"))
        status, detail = "error", ""
        candidates: List[Dict[str, Any]] = []
        trace: Dict[str, Any] = {"agent": "review", "steps": 0, "stop_reason": "error", "tool_calls": []}
        try:
            code = str(state["target"]["code"])
            if recorder is not None:
                recorder.add_step("think", content=f"[review] 策略 {strategy}，开始审查 {len(code.splitlines())} 行代码")
            system, user = build_review_prompt(
                role_config, state.get("context") or {}, code, str(state.get("extra_context") or "")
            )
            result = ToolCallingExecutor(
                llm, role_registry, role_config, service, trace=recorder
            ).run(code, system, user)
            trace = {"agent": "review", "steps": result.steps, "stop_reason": result.stop_reason, "tool_calls": result.tool_calls}
            payload = _validated_output(llm, result.content, "issues")
            raw = payload["issues"]
            called_tools = set((state.get("context") or {}).get("shared_tools") or [])
            called_tools.update(record.tool_name for record in service.records.values() if record.ok)
            kept, notes = _validate_mixed_issues(
                raw, int(state["target"]["line_count"]), called_tools, service.successful_ids()
            )
            for item in kept:
                issue = Issue.model_validate(item)
                issue.produced_by = "review"
                issue.sources = ["review"]
                issue.ensure_candidate_id()
                candidates.append(FindingCandidate(reviewer="review", issue=issue).model_dump(mode="json"))
            status = "completed" if result.stop_reason in {"final", "budget_final"} else "error"
            detail = "；".join(notes[:4]) or result.stop_reason
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if recorder is not None:
                recorder.mark_error(f"review: {detail}")
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "candidates": candidates,
            "evidence_records": service.export(),
            "coverage": {"review": {"status": status, "detail": detail, "elapsed_ms": elapsed, "issues": len(candidates)}},
            "agent_traces": [trace],
            "events": [{"type": "review_agent", "node": "review", "status": status, "issues": len(candidates), "tool_calls": len(trace["tool_calls"]), "elapsed_ms": round(elapsed, 2)}],
        }
    return review


def normalize_findings(state: ReviewState) -> Dict[str, Any]:
    best: Dict[tuple[str, str, int], Issue] = {}
    for raw in state.get("candidates") or []:
        try:
            issue = FindingCandidate.model_validate(raw).to_issue()
        except Exception:
            continue
        key = issue.identity()
        if key not in best or issue.confidence > best[key].confidence:
            best[key] = issue
    merged = sorted(best.values(), key=lambda item: (item.line_start, item.category.value, item.subtype))
    memory_flags: Dict[str, str] = {}
    if state.get("use_memory", True):
        try:
            store = MemoryStore()
            for issue in merged:
                flagged, reason = store.suppression_check(state.get("project") or "default", issue, str(state["target"]["code"]))
                if flagged:
                    memory_flags[issue.ensure_candidate_id()] = reason
        except Exception:  # noqa: BLE001
            pass
    return {
        "merged_issues": [issue.model_dump(mode="json") for issue in merged],
        "memory_flags": memory_flags,
        "events": [{"type": "normalize", "candidates": len(state.get("candidates") or []), "merged": len(merged)}],
    }


def plan_verification(state: ReviewState) -> Dict[str, Any]:
    issues = [Issue.model_validate(item) for item in state.get("merged_issues") or []]
    strategy = str(state.get("strategy") or "auto")
    if strategy == "fast":
        selected: List[str] = []
    elif strategy == "full":
        selected = [item.ensure_candidate_id() for item in issues]
    else:
        selected = [
            item.ensure_candidate_id() for item in issues
            if item.severity in {Severity.HIGH, Severity.MEDIUM_HIGH}
            or item.confidence < settings.VERIFICATION_LOW_CONFIDENCE
            or item.disagreement
            or item.ensure_candidate_id() in (state.get("memory_flags") or {})
        ]
    verification_status = "pending" if selected else "not_required"
    detail = "fast 策略不进行模型复核" if strategy == "fast" else "没有候选需要复核"
    update: Dict[str, Any] = {
        "verification_plan": selected,
        "events": [{"type": "verification_plan", "strategy": strategy, "selected": len(selected)}],
    }
    if not selected:
        update["coverage"] = {"verification": {"status": "not_required", "detail": detail, "elapsed_ms": 0, "issues": 0}}
        update["verification_status"] = verification_status
    return update


def route_verification(state: ReviewState) -> str:
    return "verification_agent" if state.get("verification_plan") else "adjudicate"


def make_verification_agent(llm: Any, registry: ToolRegistry) -> Callable[[ReviewState], Dict[str, Any]]:
    def verify(state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
        started = time.perf_counter()
        recorder = _trace_recorder(config)
        selected_ids = set(state.get("verification_plan") or [])
        issues = [Issue.model_validate(item) for item in state.get("merged_issues") or [] if Issue.model_validate(item).ensure_candidate_id() in selected_ids]
        role_registry = _registry_for(registry, VERIFICATION_AGENT_CONFIG.tool_names)
        previous_records = dict(state.get("evidence_records") or {})
        service = EvidenceService(role_registry, str(state["target"]["code"]), previous_records)
        reviews: Dict[str, Any] = {}
        status, detail = "error", ""
        trace: Dict[str, Any] = {"agent": "verification", "steps": 0, "stop_reason": "error", "tool_calls": []}
        try:
            code = str(state["target"]["code"])
            if recorder is not None:
                recorder.add_step("think", content=f"[verification] 复核 {len(selected_ids)} 个候选")
            system, user = build_verification_prompt(issues, code, state.get("context") or {})
            result = ToolCallingExecutor(
                llm, role_registry, VERIFICATION_AGENT_CONFIG, service, trace=recorder
            ).run(code, system, user)
            trace = {"agent": "verification", "steps": result.steps, "stop_reason": result.stop_reason, "tool_calls": result.tool_calls}
            payload = _validated_output(llm, result.content, "reviews")
            valid_ids = service.successful_ids()
            for raw in payload.get("reviews") or []:
                if not isinstance(raw, dict):
                    continue
                candidate_id = str(raw.get("candidate_id") or "")
                verdict = str(raw.get("verdict") or "insufficient").lower()
                if candidate_id not in selected_ids or verdict not in {"supported", "refuted", "insufficient"}:
                    continue
                refs = [str(item) for item in raw.get("evidence_refs") or []]
                invalid = sorted(set(refs) - valid_ids)
                code_lines = sorted({
                    int(item) for item in raw.get("code_lines") or []
                    if str(item).isdigit() and 1 <= int(item) <= int(state["target"]["line_count"])
                })
                if invalid:
                    verdict = "insufficient"
                if verdict == "refuted" and not refs and not code_lines:
                    verdict = "insufficient"
                reviews[candidate_id] = {
                    "verdict": verdict,
                    "reason": str(raw.get("reason") or ""),
                    "evidence_refs": [item for item in refs if item in valid_ids],
                    "missing_context": [str(item) for item in raw.get("missing_context") or []],
                    "code_lines": code_lines,
                    "invalid_evidence_refs": invalid,
                }
            complete = len(reviews) == len(selected_ids)
            status = "completed" if result.stop_reason in {"final", "budget_final"} and complete else "error"
            detail = result.stop_reason if complete else f"仅复核 {len(reviews)}/{len(selected_ids)} 个候选"
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if recorder is not None:
                recorder.mark_error(f"verification: {detail}")
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "verification_reviews": reviews,
            "verification_status": status,
            "evidence_records": service.export(),
            "coverage": {"verification": {"status": status, "detail": detail, "elapsed_ms": elapsed, "issues": len(reviews)}},
            "agent_traces": [trace],
            "events": [{"type": "verification_agent", "node": "verification", "status": status, "selected": len(selected_ids), "reviews": len(reviews), "tool_calls": len(trace["tool_calls"]), "elapsed_ms": round(elapsed, 2)}],
        }
    return verify


def adjudicate(state: ReviewState) -> Dict[str, Any]:
    reviews = state.get("verification_reviews") or {}
    selected = set(state.get("verification_plan") or [])
    memory_flags = state.get("memory_flags") or {}
    adjudicated: List[Issue] = []
    counts = {status.value: 0 for status in AdjudicationStatus}
    for raw in state.get("merged_issues") or []:
        issue = Issue.model_validate(raw)
        candidate_id = issue.ensure_candidate_id()
        review = reviews.get(candidate_id) or {}
        verdict = review.get("verdict")
        if verdict == "refuted":
            status = AdjudicationStatus.REJECTED
            reason = str(review.get("reason") or "复核发现明确反证")
        elif verdict == "supported":
            status = AdjudicationStatus.CONFIRMED
            reason = str(review.get("reason") or "独立复核支持该候选")
            issue.verified = True
        elif candidate_id in memory_flags:
            status = AdjudicationStatus.MEMORY_SUSPECTED
            reason = memory_flags[candidate_id]
            issue.confidence = min(issue.confidence, 0.4)
        else:
            status = AdjudicationStatus.NEEDS_REVIEW
            if candidate_id in selected:
                reason = str(review.get("reason") or "复核失败、证据不足或预算未完成")
            elif state.get("strategy") == "fast":
                reason = "fast 策略未进行独立模型复核"
            else:
                reason = "未触发独立复核，保留人工确认"
        issue.adjudication = status
        issue.adjudication_reason = reason
        issue.verify_note = "；".join(filter(None, [issue.verify_note, reason]))
        counts[status.value] += 1
        adjudicated.append(issue)
    return {
        "merged_issues": [item.model_dump(mode="json") for item in adjudicated],
        "events": [{"type": "adjudication", "counts": counts}],
    }


def finalize(state: ReviewState) -> Dict[str, Any]:
    all_issues = [Issue.model_validate(item) for item in state.get("merged_issues") or []]
    visible = [item for item in all_issues if item.adjudication is not AdjudicationStatus.REJECTED]
    counts: Dict[str, int] = {}
    for issue in all_issues:
        key = issue.adjudication.value if issue.adjudication else "unadjudicated"
        counts[key] = counts.get(key, 0) + 1
    report = ReviewReport(
        review_summary=f"三角色审查完成：{len(visible)} 条展示结论，{len(all_issues) - len(visible)} 条被驳回",
        issues=visible,
    ).dedupe()
    payload = {
        "review_summary": report.review_summary,
        "issues": [item.to_legacy() for item in report.issues],
        "stats": {
            "issues": len(report.issues),
            "adjudication": counts,
            "selected_roles": ["review"] + (["verification"] if state.get("verification_plan") else []),
            "verification": {
                "selected": len(state.get("verification_plan") or []),
                "completed": len(state.get("verification_reviews") or {}),
                "status": state.get("verification_status") or "not_required",
                "unreviewed_candidate_ids": sorted(set(state.get("verification_plan") or []) - set((state.get("verification_reviews") or {}).keys())),
            },
            "agent_traces": list(state.get("agent_traces") or []),
            "evidence_records": len(state.get("evidence_records") or {}),
        },
    }
    return {"report": payload, "events": [{"type": "finalize", "issues": len(report.issues)}]}


def build_graph(llm: Any, registry: ToolRegistry) -> Any:
    builder = StateGraph(ReviewState)
    builder.add_node("load_target", load_target)
    builder.add_node("prepare_context", prepare_context)
    builder.add_node("review_agent", make_review_agent(llm, registry))
    builder.add_node("normalize_findings", normalize_findings)
    builder.add_node("plan_verification", plan_verification)
    builder.add_node("verification_agent", make_verification_agent(llm, registry))
    builder.add_node("adjudicate", adjudicate)
    builder.add_node("finalize_report", finalize)
    builder.add_edge(START, "load_target")
    builder.add_edge("load_target", "prepare_context")
    builder.add_edge("prepare_context", "review_agent")
    builder.add_edge("review_agent", "normalize_findings")
    builder.add_edge("normalize_findings", "plan_verification")
    builder.add_conditional_edges(
        "plan_verification", route_verification,
        {"verification_agent": "verification_agent", "adjudicate": "adjudicate"},
    )
    builder.add_edge("verification_agent", "adjudicate")
    builder.add_edge("adjudicate", "finalize_report")
    builder.add_edge("finalize_report", END)
    # Conversation continuity comes from bounded memory, not stale graph state.
    return builder.compile()


__all__ = [
    "build_graph", "load_target", "prepare_context", "normalize_findings",
    "plan_verification", "route_verification", "adjudicate", "finalize",
]
