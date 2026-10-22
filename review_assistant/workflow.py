"""图编排。

结构是 supervisor + handoff 模式：

    START → load_target（准备审查对象）
              ↓  按对话栈选择当前助手
        ┌──────────────────────────────────────────────┐
        │ primary_assistant（主助手 / 调度 + 规范检索）│
        │   ├─ 交接工具 → enter_<子助手> → 子助手       │
        │   └─ 无工具调用 → finalize_report → END       │
        └──────────────────────────────────────────────┘
             子助手 → 只读工具节点 → 回到 load_target（再按栈顶回到子助手）
                    → 无工具调用 → collect_<子助手>（校验结论并入状态）→ 主助手
                    → CompleteOrEscalate → leave_skill → 主助手
             修复助手 → *_sensitive_tools（interrupt_before，需人工确认）

编排全部由图表达：谁在什么时候说话、子助手之间怎么交接、
哪些工具要停下来问用户，都由节点、边与状态决定。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.schema import Issue, ReviewReport
from review_assistant.dimensions import (
    ASSISTANT_CATEGORY,
    ASSISTANT_LABEL,
    DIMENSION_ASSISTANTS,
    FIX_ASSISTANT,
    HANDOFF_TARGET,
    PRIMARY_ASSISTANT,
)
from review_assistant.findings import parse_json_object, validate_issues
from review_assistant.handoffs import (
    CompleteOrEscalate,
    ToBugAssistant,
    ToFixAssistant,
    ToPerformanceAssistant,
    ToSecurityAssistant,
    ToSmellAssistant,
)
from review_assistant.prompts import (
    PRIMARY_PROMPT,
    build_system_content,
    dimension_prompt,
    fix_prompt,
)
from review_assistant.state import ReviewState
from review_assistant.toolset import Toolset

#: 打印每一步的运行时提示（CLI 开启，测试可关闭）
VERBOSE = True

HANDOFF_MODELS = (
    ToSecurityAssistant,
    ToBugAssistant,
    ToSmellAssistant,
    ToPerformanceAssistant,
    ToFixAssistant,
)


def _log(message: str) -> None:
    if VERBOSE:
        print(message)


def _last_message(state: ReviewState) -> Optional[BaseMessage]:
    messages = state.get("messages") or []
    return messages[-1] if messages else None


def _last_text(state: ReviewState) -> str:
    message = _last_message(state)
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(str(part) for part in content)
    return str(content or "")


def _tools_called_since_entry(messages: Sequence[BaseMessage]) -> Set[str]:
    """统计「最近一次交接之后」被调用过的工具名。

    证据校验需要知道某条结论声称的证据工具到底有没有被调用过。从消息尾部往前扫，
    遇到 enter 节点写入的交接消息就停——那正是当前子助手本轮工作的起点。
    """
    names: Set[str] = set()
    for message in reversed(list(messages)):
        if isinstance(message, ToolMessage) and str(message.content or "").startswith("已转交"):
            break
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                names.add(str(name))
    return names


def current_assistant(state: ReviewState) -> str:
    """对话栈栈顶即当前该说话的助手。"""
    stack = state.get("dialog_state") or []
    return stack[-1] if stack else PRIMARY_ASSISTANT


# ---------------------------------------------------------------------------
# 准备节点
# ---------------------------------------------------------------------------


def load_target(state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
    """确认审查对象。

    审查对象在第一次运行时从 ``config.configurable["target_path"]`` 读取并缓存进状态；
    之后每轮工具调用回到这里时直接跳过，避免重复读盘。
    """
    target = state.get("target") or {}
    if target.get("code"):
        return {}

    configurable = (config or {}).get("configurable", {}) or {}
    path = str(configurable.get("target_path") or "").strip()
    if not path:
        raise ValueError(
            "缺少审查对象：请在 config.configurable 中提供 target_path，"
            "或使用 `python -m review_assistant.main --file <路径>`。"
        )

    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"待审查文件不存在：{file_path}")

    code = file_path.read_text(encoding="utf-8", errors="replace")
    line_count = max(1, len(code.split("\n")))
    _log(f"[准备] 已载入 {file_path}（{line_count} 行）")
    return {
        "target": {"path": str(file_path), "code": code, "line_count": line_count},
        "events": [{"type": "load_target", "path": str(file_path), "lines": line_count}],
    }


# ---------------------------------------------------------------------------
# 助手节点
# ---------------------------------------------------------------------------


def make_assistant(
    llm: Any,
    tools: Sequence[Any],
    role_prompt: str,
    node_name: str,
) -> Callable[..., Dict[str, Any]]:
    """造一个助手节点：组装系统消息 → 调模型 → 把回复写回消息列表。"""
    model = llm.bind_tools(list(tools)) if tools else llm
    label = ASSISTANT_LABEL.get(node_name, node_name)

    def call_model(state: ReviewState) -> Dict[str, Any]:
        system_content = build_system_content(
            node=node_name,
            role_prompt=role_prompt,
            target=state.get("target") or {},
            issues=state.get("issues") or [],
            reviewed=state.get("reviewed_dimensions") or [],
            fix=state.get("fix") or {},
        )
        _log(f"\n[助手：{label}]")
        _log(f"[状态栈] {state.get('dialog_state') or []}")

        response = model.invoke([SystemMessage(content=system_content), *(state.get("messages") or [])])
        calls = getattr(response, "tool_calls", None) or []
        for call in calls:
            _log(
                "[工具请求] "
                f"{call.get('name')} {json.dumps(call.get('args') or {}, ensure_ascii=False)}"
            )
        if not calls:
            _log(f"[回复] {str(getattr(response, 'content', '') or '')[:200]}")

        return {
            "messages": [response],
            "events": [
                {
                    "type": "assistant",
                    "node": node_name,
                    "tool_calls": [call.get("name") for call in calls],
                }
            ],
        }

    return call_model


def make_entry_node(assistant_name: str) -> Callable[[ReviewState], Dict[str, Any]]:
    """交接落地节点：把 handoff 工具调用变成一条 ToolMessage，并把助手压入对话栈。"""
    label = ASSISTANT_LABEL[assistant_name]

    def enter(state: ReviewState) -> Dict[str, Any]:
        message = _last_message(state)
        call = (getattr(message, "tool_calls", None) or [{}])[0]
        request = (call.get("args") or {}).get("request", "")
        return {
            "messages": [
                ToolMessage(
                    content=f"已转交给{label}。任务：{request}",
                    tool_call_id=call.get("id") or "",
                )
            ],
            "dialog_state": assistant_name,
        }

    return enter


def leave_skill(state: ReviewState) -> Dict[str, Any]:
    """子助手主动交回主助手。"""
    message = _last_message(state)
    call = (getattr(message, "tool_calls", None) or [{}])[0]
    reason = (call.get("args") or {}).get("reason", "")
    label = ASSISTANT_LABEL.get(current_assistant(state), "子助手")
    return {
        "messages": [
            ToolMessage(
                content=f"{label}已交回主助手。说明：{reason}",
                tool_call_id=call.get("id") or "",
            )
        ],
        "dialog_state": "pop",
    }


def reject_tool_calls(state: ReviewState) -> Dict[str, Any]:
    """一次想调多个工具时全部拒绝：路由层保证每轮只执行一个工具。"""
    message = _last_message(state)
    calls = getattr(message, "tool_calls", None) or []
    return {
        "messages": [
            ToolMessage(
                content="本次没有执行任何工具。请每次只调用一个当前助手允许的工具。",
                tool_call_id=call.get("id") or "",
            )
            for call in calls
        ]
    }


def all_done_hint(state: ReviewState) -> Dict[str, Any]:
    """四个维度都审完了还想继续委派时的提示节点（防止无意义地空转）。"""
    message = _last_message(state)
    call = (getattr(message, "tool_calls", None) or [{}])[0]
    return {
        "messages": [
            ToolMessage(
                content=(
                    "四个审查维度都已完成。请直接输出最终的中文审查总结，不要再委派子助手；"
                    "若确实需要重新审查某维度，请说明理由。"
                ),
                tool_call_id=call.get("id") or "",
            )
        ],
        "events": [{"type": "all_done_hint"}],
    }


# ---------------------------------------------------------------------------
# 收集节点：把子助手的结论并入状态
# ---------------------------------------------------------------------------


def make_collect_node(assistant_name: str) -> Callable[[ReviewState], Dict[str, Any]]:
    """子助手给出最终文本后的落库节点：解析 → 校验 → 合并 → 回主助手。"""
    category = ASSISTANT_CATEGORY[assistant_name]
    label = ASSISTANT_LABEL[assistant_name]

    def collect(state: ReviewState) -> Dict[str, Any]:
        text = _last_text(state)
        target = state.get("target") or {}
        total_lines = int(target.get("line_count") or 1)
        called = _tools_called_since_entry(state.get("messages") or [])

        payload, error = parse_json_object(text)
        kept: List[Dict[str, Any]] = []
        notes: List[str] = []

        if payload is None:
            notes.append(f"未产出结构化结论（{error}）")
        else:
            raw_issues = payload.get("issues")
            if not isinstance(raw_issues, list):
                notes.append("issues 字段不是数组，按空结论处理")
                raw_issues = []
            kept, notes = validate_issues(raw_issues, category, total_lines, called)

        _log(
            f"[收集] {label}：提交 {len(kept)} 条有效结论"
            + (f"，说明：{'；'.join(notes[:3])}" if notes else "")
        )
        return {
            "issues": kept,
            "reviewed_dimensions": [assistant_name],
            "dialog_state": "pop",
            "events": [
                {
                    "type": "collect",
                    "node": assistant_name,
                    "kept": len(kept),
                    "notes": notes,
                }
            ],
        }

    return collect


def make_collect_fix_node(toolset: Toolset) -> Callable[[ReviewState], Dict[str, Any]]:
    """修复结果的落库节点：harness 侧强制做语法与接口兼容性检查。"""

    def collect_fix(state: ReviewState) -> Dict[str, Any]:
        text = _last_text(state)
        original = (state.get("target") or {}).get("code") or ""
        payload, error = parse_json_object(text)

        fix: Dict[str, Any] = {
            "summary": "",
            "fixed_code": "",
            "changed": False,
            "checks": [],
            "addressed_issues": [],
            "notes": [],
        }

        if payload is None:
            fix["notes"].append(f"未产出结构化修复结果（{error}）")
        else:
            fixed_code = str(payload.get("fixed_code") or "")
            fix["summary"] = str(payload.get("summary") or "")
            fix["addressed_issues"] = list(payload.get("addressed_issues") or [])
            fix["fixed_code"] = fixed_code
            fix["notes"].append(str(payload.get("verification_note") or ""))

            ok = True
            try:
                ast.parse(fixed_code)
                fix["checks"].append({"name": "syntax", "passed": True, "detail": "ast.parse 通过"})
            except SyntaxError as exc:
                ok = False
                fix["checks"].append(
                    {
                        "name": "syntax",
                        "passed": False,
                        "detail": f"第 {exc.lineno} 行语法错误：{exc.msg}",
                    }
                )

            registry = getattr(toolset, "registry", None)
            signature_tool = registry.get("check_signature") if registry is not None else None
            if signature_tool is not None and fixed_code:
                result = signature_tool(original_code=original, modified_code=fixed_code)
                data = result.data if isinstance(result.data, dict) else {}
                compatible = bool(data.get("compatible"))
                detail = data.get("detail") or (
                    "公开接口保持不变" if compatible else f"接口变化：{data}"
                )
                fix["checks"].append(
                    {"name": "signature_compatibility", "passed": compatible, "detail": detail}
                )
                ok = ok and compatible

            fix["changed"] = bool(fixed_code.strip()) and fixed_code.strip() != original.strip()
            fix["verified"] = ok
            if not ok:
                fix["notes"].append("修复未通过 harness 校验（语法/接口），请以 checks 为准。")

        _log(f"[收集] 修复助手：{'通过' if fix.get('verified') else '未通过'} harness 校验")
        return {
            "fix": fix,
            "reviewed_dimensions": [FIX_ASSISTANT],
            "dialog_state": "pop",
            "events": [{"type": "collect_fix", "verified": bool(fix.get("verified"))}],
        }

    return collect_fix


# ---------------------------------------------------------------------------
# 汇总节点
# ---------------------------------------------------------------------------


def make_finalize_node() -> Callable[..., Dict[str, Any]]:
    """把状态里的结论汇总成最终报告。"""

    def finalize_report(state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
        raw_issues = state.get("issues") or []
        issues: List[Issue] = []
        for item in raw_issues:
            try:
                issues.append(Issue.model_validate(item))
            except Exception:  # noqa: BLE001 - 收集阶段已校验过，这里只做兜底
                continue

        report = ReviewReport(review_summary=_last_text(state), issues=issues).dedupe()

        suppressed: List[Dict[str, Any]] = []
        configurable = (config or {}).get("configurable", {}) or {}
        if configurable.get("use_memory", True) and report.issues:
            try:
                from agent.memory import MemoryStore

                store = MemoryStore()
                kept, suppressed = store.apply_suppression(
                    str(configurable.get("project") or "default"),
                    report.issues,
                    (state.get("target") or {}).get("code") or "",
                )
                report = ReviewReport(review_summary=report.review_summary, issues=kept)
            except Exception as exc:  # noqa: BLE001 - 记忆不可用不应影响出报告
                _log(f"[汇总] 记忆压制跳过：{exc}")

        summary = report.review_summary.strip() or f"共发现 {len(report.issues)} 个问题"
        if suppressed:
            summary += f"（记忆库压制 {len(suppressed)} 条历史误报形态）"

        fix = state.get("fix") or {}
        payload = {
            "review_summary": summary,
            "issues": [issue.to_legacy() for issue in report.issues],
            "reviewed_dimensions": state.get("reviewed_dimensions") or [],
            "fix": fix,
            "stats": {
                "issues": len(report.issues),
                "high_risk": sum(1 for issue in report.issues if issue.risk_level == "高"),
                "unverified": sum(1 for issue in report.issues if issue.verified is False),
                "suppressed_by_memory": len(suppressed),
            },
        }
        _log(f"\n[汇总] 共 {len(report.issues)} 条结论，已生成结构化报告")
        return {
            "report": payload,
            "events": [{"type": "finalize", "issues": len(report.issues)}],
        }

    return finalize_report


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def make_router(
    control_routes: Dict[str, str],
    business_routes: Dict[str, str],
    fallback: str,
) -> Callable[[ReviewState], str]:
    """通用路由：控制工具 → 专用节点；业务工具 → 工具节点；无调用 → fallback。"""

    def route(state: ReviewState) -> str:
        calls = getattr(_last_message(state), "tool_calls", None) or []
        if not calls:
            return fallback
        # 代码层面保证一次只执行一个请求
        if len(calls) != 1:
            return "reject_tool_calls"
        name = str(calls[0].get("name") or "")
        if name in control_routes:
            return control_routes[name]
        return business_routes.get(name, "reject_tool_calls")

    return route


def make_primary_router(business_routes: Dict[str, str]) -> Callable[[ReviewState], str]:
    controls = {
        model.__name__: f"enter_{HANDOFF_TARGET[model.__name__]}" for model in HANDOFF_MODELS
    }
    inner = make_router(controls, business_routes, "finalize_report")

    def route(state: ReviewState) -> str:
        calls = getattr(_last_message(state), "tool_calls", None) or []
        if len(calls) == 1:
            target = HANDOFF_TARGET.get(str(calls[0].get("name") or ""))
            reviewed = state.get("reviewed_dimensions") or []
            hints = sum(1 for event in (state.get("events") or []) if event.get("type") == "all_done_hint")
            if (
                target in DIMENSION_ASSISTANTS
                and all(name in reviewed for name in DIMENSION_ASSISTANTS)
                and hints < 2
            ):
                return "all_done_hint"
        return inner(state)

    return route


# ---------------------------------------------------------------------------
# 建图
# ---------------------------------------------------------------------------


def build_graph(llm: Any, toolset: Toolset) -> Any:
    """组装并编译状态图。"""
    builder = StateGraph(ReviewState)

    # ---- 无参数节点 ----
    builder.add_node("load_target", load_target)
    builder.add_node("leave_skill", leave_skill)
    builder.add_node("reject_tool_calls", reject_tool_calls)
    builder.add_node("all_done_hint", all_done_hint)
    builder.add_node("finalize_report", make_finalize_node())

    # ---- 主助手 ----
    builder.add_node(
        PRIMARY_ASSISTANT,
        make_assistant(
            llm,
            [*toolset.primary, *HANDOFF_MODELS],
            PRIMARY_PROMPT,
            PRIMARY_ASSISTANT,
        ),
    )
    builder.add_node(
        "primary_tools",
        ToolNode(
            toolset.primary,
            handle_tool_errors="检索工具执行失败，不能编造结果或声称检索成功。",
        ),
    )
    builder.add_edge("primary_tools", "load_target")

    handoff_routes = {
        model.__name__: f"enter_{HANDOFF_TARGET[model.__name__]}" for model in HANDOFF_MODELS
    }
    primary_business = {tool.name: "primary_tools" for tool in toolset.primary}

    builder.add_conditional_edges(
        PRIMARY_ASSISTANT,
        make_primary_router(primary_business),
        [
            *handoff_routes.values(),
            "primary_tools",
            "all_done_hint",
            "reject_tool_calls",
            "finalize_report",
        ],
    )

    # ---- 子助手 ----
    assistant_names = [PRIMARY_ASSISTANT, *toolset.assistants.keys()]

    for name, tools in toolset.assistants.items():
        entry_name = f"enter_{name}"
        safe_node = f"{name}_safe_tools"
        sensitive_node = f"{name}_sensitive_tools"
        collect_node = f"collect_{name}"
        sensitive_names = toolset.sensitive.get(name) or set()

        safe_tools = [tool for tool in tools if tool.name not in sensitive_names]
        sensitive_tools = [tool for tool in tools if tool.name in sensitive_names]

        builder.add_node(
            name,
            make_assistant(
                llm,
                [*tools, CompleteOrEscalate],
                dimension_prompt(name) if name in ASSISTANT_CATEGORY else fix_prompt(),
                name,
            ),
        )
        builder.add_node(entry_name, make_entry_node(name))
        builder.add_node(
            collect_node,
            make_collect_node(name) if name in ASSISTANT_CATEGORY else make_collect_fix_node(toolset),
        )
        builder.add_edge(entry_name, name)
        builder.add_edge(collect_node, PRIMARY_ASSISTANT)

        routes = {tool.name: safe_node for tool in safe_tools}
        routes.update({tool.name: sensitive_node for tool in sensitive_tools})

        targets = ["leave_skill", collect_node, "reject_tool_calls"]

        if safe_tools:
            builder.add_node(
                safe_node,
                ToolNode(safe_tools, handle_tool_errors="查询失败，不能编造结果。"),
            )
            builder.add_edge(safe_node, "load_target")
            targets.append(safe_node)

        if sensitive_tools:
            builder.add_node(
                sensitive_node,
                ToolNode(
                    sensitive_tools,
                    handle_tool_errors=(
                        "工具执行失败，请检查实际结果，不能声称操作成功。"
                    ),
                ),
            )
            builder.add_edge(sensitive_node, "load_target")
            targets.append(sensitive_node)

        builder.add_conditional_edges(
            name,
            make_router({"CompleteOrEscalate": "leave_skill"}, routes, collect_node),
            targets,
        )

    # ---- 汇合与循环 ----
    builder.add_edge(START, "load_target")
    builder.add_edge("leave_skill", PRIMARY_ASSISTANT)
    builder.add_edge("all_done_hint", PRIMARY_ASSISTANT)
    builder.add_edge("reject_tool_calls", "load_target")
    builder.add_edge("finalize_report", END)

    builder.add_conditional_edges("load_target", current_assistant, assistant_names)

    return builder.compile(
        checkpointer=InMemorySaver(),
        interrupt_before=toolset.sensitive_nodes(),
    )
