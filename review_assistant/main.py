"""命令行入口 + 敏感操作人工确认。

用法::

    python -m review_assistant.main --file demo.py                    # 交互式对话
    python -m review_assistant.main --file demo.py --question "审查这个文件"
    python -m review_assistant.main --file demo.py --mock             # 离线假模型
    python -m review_assistant.main --file demo.py --format json

人工确认是这套设计里最有价值的一环：修复助手要用 ``execute_python_code`` /
``run_tests`` / ``apply_patch`` 时，工作流会停在 ``*_sensitive_tools`` 节点上，
由本文件负责把「工具 + 参数」摊给用户看，用户点头才继续。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 旧配置（API_KEY / MODEL_NAME）缺失时 config.py 会警告。以下两种情况这个警告没有意义，
# 先关掉再导入 config：① 走离线 mock；② 用的是本包专属的 LLM_* 配置。
_OFFLINE = "--mock" in sys.argv or os.getenv("MOCK_MODE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}
_LLM_CONFIGURED = bool(os.getenv("LLM_API_KEY")) and bool(os.getenv("LLM_MODEL"))
if _OFFLINE or _LLM_CONFIGURED:
    logging.getLogger("config").setLevel(logging.ERROR)

from review_assistant import workflow as workflow_module  # noqa: E402
from review_assistant.dimensions import ASSISTANT_LABEL  # noqa: E402
from review_assistant.llm import create_llm, describe_backend  # noqa: E402
from review_assistant.toolset import build_toolset  # noqa: E402

#: 人工确认回调：返回 None 表示放行，返回字符串表示拒绝（字符串是拒绝原因）
Approver = Callable[[str, Dict[str, Any]], Optional[str]]


def build_agent(
    codebase_root: Optional[str] = None,
    provider: Optional[str] = None,
    verbose: bool = True,
) -> Any:
    """按默认配置建图。"""
    workflow_module.VERBOSE = verbose
    toolset = build_toolset(codebase_root)
    return workflow_module.build_graph(create_llm(provider=provider), toolset)


def make_config(
    target_path: str,
    thread_id: Optional[str] = None,
    project: str = "default",
    use_memory: bool = True,
    recursion_limit: int = 60,
) -> Dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id or str(uuid.uuid4()),
            "target_path": str(target_path),
            "project": project,
            "use_memory": use_memory,
        },
        "recursion_limit": recursion_limit,
    }


def _print_pending(call: Dict[str, Any]) -> None:
    print("\n待确认操作：")
    print("工具：", call.get("name"))
    print("参数：", json.dumps(call.get("args") or {}, ensure_ascii=False, indent=2)[:2000])
    print("批准只表示允许执行，是否成功要看工具返回结果。")


def ask_user(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """默认确认器：在终端问用户。"""
    _print_pending({"name": tool_name, "args": arguments})
    answer = input("输入 y 执行；其它输入表示拒绝，也可填写拒绝原因：").strip()
    if answer.lower() == "y":
        return None
    return answer or "用户未批准"


def auto_approve(_tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    return None


def run_turn(
    graph: Any,
    question: str,
    config: Dict[str, Any],
    approver: Approver = ask_user,
) -> Dict[str, Any]:
    """跑一轮完整对话，遇到敏感工具就停下来问用户，直到图执行结束。"""
    graph.invoke({"messages": [HumanMessage(content=question)]}, config=config)

    while True:
        snapshot = graph.get_state(config)
        pending = list(snapshot.next or ())

        if not pending:
            break

        if len(pending) != 1:
            raise RuntimeError(f"出现多个待执行节点：{pending}")

        node = pending[0]
        if not node.endswith("_sensitive_tools"):
            raise RuntimeError(f"遇到非预期暂停节点：{node}")

        message = snapshot.values["messages"][-1]
        calls = getattr(message, "tool_calls", None) or []
        if len(calls) != 1:
            raise RuntimeError("待确认操作必须对应一个工具调用")

        call = calls[0]
        reason = approver(str(call.get("name")), call.get("args") or {})

        if reason is not None:
            rejection = ToolMessage(
                content=json.dumps(
                    {
                        "status": "rejected",
                        "message": "用户拒绝执行，工具未运行",
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=call.get("id") or "",
            )
            graph.update_state(config, {"messages": [rejection]}, as_node=node)

        graph.invoke(None, config=config)

    values = graph.get_state(config).values
    report = values.get("report") or {}
    reply = str(getattr((values.get("messages") or [HumanMessage(content="")])[-1], "content", ""))

    if workflow_module.VERBOSE:
        print(f"\n助手：{reply}")
    return {
        "reply": reply,
        "report": report,
        "fix": values.get("fix") or {},
        "events": values.get("events") or [],
        "reviewed_dimensions": values.get("reviewed_dimensions") or [],
    }


def render_text(result: Dict[str, Any]) -> str:
    """把结果渲染成人类可读文本。"""
    report = result.get("report") or {}
    issues = report.get("issues") or []
    dimensions = result.get("reviewed_dimensions") or []

    lines: List[str] = ["=== 代码审查报告 ==="]
    lines.append(f"摘要：{report.get('review_summary') or '（无）'}")
    lines.append(
        "已完成维度：" + "、".join(ASSISTANT_LABEL.get(name, name) for name in dimensions)
    )

    stats = report.get("stats") or {}
    if stats:
        lines.append(
            f"统计：结论 {stats.get('issues', 0)} 条"
            f"｜高风险 {stats.get('high_risk', 0)} 条"
            f"｜未核实 {stats.get('unverified', 0)} 条"
            f"｜记忆压制 {stats.get('suppressed_by_memory', 0)} 条"
        )

    lines.append("")
    if not issues:
        lines.append("未发现带证据支撑的问题。")
    for index, issue in enumerate(issues, start=1):
        flag = "" if issue.get("verified") is not False else "（证据不足）"
        lines.append(
            f"{index}. [{issue.get('category')} / {issue.get('severity')}] "
            f"行 {issue.get('line_number')}：{issue.get('risk_description')}{flag}"
        )
        lines.append(f"   规则：{issue.get('violated_rule')}")
        lines.append(f"   建议：{issue.get('fix_suggestion')}")
        lines.append(f"   置信度：{issue.get('confidence')}")
        for evidence in issue.get("evidence") or []:
            source = evidence.get("tool") or evidence.get("kind")
            lines.append(f"   证据[{source}] 行 {evidence.get('line')}：{evidence.get('detail')}")
        if issue.get("verify_note"):
            lines.append(f"   校验：{issue['verify_note']}")

    fix = result.get("fix") or {}
    if fix:
        lines.append("")
        lines.append("=== 修复结果 ===")
        lines.append(f"说明：{fix.get('summary') or '（无）'}")
        for check in fix.get("checks") or []:
            mark = "PASS" if check.get("passed") else "FAIL"
            lines.append(f"  [{mark}] {check.get('name')}: {check.get('detail')}")
        if fix.get("fixed_code"):
            lines.append("修复后代码：")
            lines.append(fix["fixed_code"])

    lines.append("")
    lines.append("=== 节点事件 ===")
    for event in result.get("events") or []:
        lines.append(f"  {event}")
    return "\n".join(lines)


def run_once(args: argparse.Namespace, graph: Any) -> int:
    config = make_config(
        target_path=args.file,
        thread_id=args.thread,
        project=args.project,
        use_memory=not args.no_memory,
    )
    result = run_turn(graph, args.question, config, approver=auto_approve if args.yes else ask_user)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        text = render_text(result)
        print(text[: args.max_chars] if args.max_chars else text)
    return 0


def chat_loop(graph: Any, args: argparse.Namespace) -> int:
    config = make_config(
        target_path=args.file,
        thread_id=args.thread,
        project=args.project,
        use_memory=not args.no_memory,
    )
    print(f"审查助手已启动（模型后端：{describe_backend(args.provider)}）。输入 quit 退出。")
    print(f"审查对象：{args.file}")
    while True:
        try:
            question = input("\n用户：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n对话结束。")
            return 0
        if question.lower() in {"quit", "exit", "q"}:
            print("对话结束。")
            return 0
        if not question:
            continue
        try:
            result = run_turn(graph, question, config)
        except Exception as exc:  # noqa: BLE001 - 交互式场景下报错不应直接退出
            print(f"[错误] {type(exc).__name__}: {exc}")
            continue
        if args.format == "json":
            print(json.dumps(result["report"], ensure_ascii=False, indent=2))
        elif (result.get("report") or {}).get("issues"):
            print()
            print(render_text(result))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m review_assistant.main",
        description="LangGraph 版代码审查 Agent（主助手 + 维度子助手 + 人工确认）",
    )
    parser.add_argument("--file", required=True, help="待审查的 Python 文件")
    parser.add_argument("--question", help="只跑一轮问题后退出（不进入交互模式）")
    parser.add_argument("--codebase-root", help="代码库根目录，启用检索类工具")
    parser.add_argument("--provider", help="模型供应商：openai / deepseek / mock")
    parser.add_argument("--mock", action="store_true", help="强制使用离线假模型")
    parser.add_argument("--yes", action="store_true", help="自动批准所有敏感操作（仅供脚本/测试）")
    parser.add_argument("--thread", help="会话线程 ID（默认随机）")
    parser.add_argument("--project", default="default", help="项目名，用于长期记忆隔离")
    parser.add_argument("--no-memory", action="store_true", help="禁用历史误报记忆压制")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--max-chars", type=int, default=0, help="文本输出截断长度，0 表示不截断")
    parser.add_argument("--quiet", action="store_true", help="不打印每个节点的运行时提示")
    args = parser.parse_args(argv)

    provider = "mock" if args.mock else args.provider
    if Path(args.file).is_dir():
        parser.error("--file 需要指向单个 Python 文件")

    graph = build_agent(args.codebase_root, provider=provider, verbose=not args.quiet)
    if args.question:
        return run_once(args, graph)
    return chat_loop(graph, args)


if __name__ == "__main__":
    raise SystemExit(main())
