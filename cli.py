"""命令行工具。

两种调用方式：

1. **无子命令调用**（``ci_integrate.sh`` 使用，务必保持可用）::

       python cli.py <file> [--optimize] [--format text|json] [--output out.txt] [--fail-on-high-risk]

2. **子命令式调用**（功能更完整）::

       python cli.py review <file>            # Agent 审查（默认文本输出）
       python cli.py optimize <file>          # 审查 + 带验证的优化
       python cli.py multi <file>             # 多智能体协作审查
       python cli.py trace <run_id>           # 查看某次运行的完整轨迹
       python cli.py traces                   # 列出历史运行
       python cli.py stats                    # 聚合统计（步数/token/成本/工具分布）
       python cli.py memory add <text>        # 写入项目约定记忆
       python cli.py memory list              # 查看记忆库

说明：无子命令调用同样走 Agent 管线，**输出结构与退出码保持不变**，CI 集成脚本依赖这一点。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from agent.trace import TraceStore, iter_run_lines  # noqa: E402

SUBCOMMANDS = {"review", "optimize", "multi", "trace", "traces", "stats", "memory"}


def _read_file(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        print(f"Error: File {path} not found.")
        sys.exit(1)
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"Error reading file: {exc}")
        sys.exit(1)


def _review_with_agent(code: str, file_path: str, codebase_root: Optional[str] = None):
    from agent.reviewer_agent import ReviewerAgent

    agent = ReviewerAgent(codebase_root=codebase_root)
    return agent.review(code, file_path=file_path)


def _render_review_text(name: str, report: Any, run: Any) -> str:
    lines = [f"=== Code Review Report: {name} ===", f"Summary: {report.review_summary}", ""]
    lines.append("Issues Found:")
    for index, issue in enumerate(report.issues, start=1):
        lines.append(
            f"{index}. [{issue.dimension}] Line {issue.line_number}: {issue.risk_description}"
        )
        lines.append(f"   Severity: {issue.severity.value}  Confidence: {issue.confidence:.2f}")
        lines.append(f"   Suggestion: {issue.fix_suggestion}")
        for evidence in issue.evidence:
            source = evidence.tool or evidence.kind
            lines.append(f"   Evidence[{source}]: {evidence.detail or evidence.snippet}")
        if issue.verify_note:
            lines.append(f"   Verify: {issue.verify_note}")
    lines.append("")
    lines.append("--- Agent Trace ---")
    lines.append(
        f"stop_reason={run.stop_reason}  steps={len(run.steps)}  "
        f"tool_calls={sum(run.tool_call_stats().values())}  tokens={run.usage.total_tokens}  "
        f"duration={run.duration_ms / 1000:.2f}s"
    )
    for step in run.steps:
        lines.append(f"  [{step.index:02d}] {step.kind:<7} {step.brief(160)}")
    return "\n".join(lines)


def _has_high_risk(report: Any) -> bool:
    return any(i.risk_level == "高" for i in report.issues)


# ---------------------------------------------------------------------------
# 无子命令调用
# ---------------------------------------------------------------------------


def run_legacy(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="AI Code Review and Optimization CLI Tool",
        epilog="提示：也可使用子命令形式，例如 `python cli.py review file.py`，"
               "通过 `python cli.py --help-sub` 查看全部子命令。",
    )
    parser.add_argument("file", help="Path to the file to review and optimize")
    parser.add_argument("--optimize", action="store_true", help="Perform optimization after review")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--output", help="Path to save the result")
    parser.add_argument(
        "--fail-on-high-risk",
        action="store_true",
        help="Return non-zero exit code if high risk issues are found",
    )
    parser.add_argument("--codebase-root", help="代码库根目录，启用检索类工具")

    args = parser.parse_args(argv)
    code = _read_file(args.file)
    report, outcome = _review_with_agent(code, str(args.file), args.codebase_root)

    result: Dict[str, Any] = {
        "file": str(args.file),
        "review": report.to_legacy(),
        "trace": outcome.run.to_trace_dict(),
        "optimization": None,
    }

    optimization = None
    if args.optimize:
        from agent.optimizer_agent import OptimizerAgent

        optimizer = OptimizerAgent(codebase_root=args.codebase_root)
        optimization, opt_run = optimizer.optimize(code, report)
        result["optimization"] = optimization.to_legacy()
        result["optimization_trace"] = opt_run.run.to_trace_dict()

    if args.format == "json":
        output_content = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        output_content = _render_review_text(Path(args.file).name, report, outcome.run)
        if optimization is not None:
            output_content += "\n\n=== Code Optimization ===\n"
            output_content += f"Summary: {optimization.summary}\n"
            output_content += f"Signature Match: {optimization.signature_match}\n"
            output_content += f"Verification: {json.dumps(optimization.verification.to_dict(), ensure_ascii=False)}\n"
            output_content += "\nOptimized Code:\n------------------\n"
            output_content += optimization.optimized_code
            output_content += "\n------------------\n"

    if args.output:
        Path(args.output).write_text(output_content, encoding="utf-8")
        print(f"Result saved to {args.output}")
    else:
        print(output_content)

    if args.fail_on_high_risk and _has_high_risk(report):
        print("\n[CI/CD ERROR] High risk issues found! Blocking pipeline.", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _print_subcommands() -> None:
    print("可用子命令：")
    print("  review    <file>    Agent 审查（文本或 JSON 输出）")
    print("  optimize  <file>    审查 + 带三级验证的优化")
    print("  multi     <file>    多智能体协作审查（Reviewer / Critic / Verifier）")
    print("  trace     <run_id>  查看某次运行的完整轨迹")
    print("  traces              列出历史运行")
    print("  stats               聚合统计")
    print("  memory   （add|list）长期记忆管理")


def cmd_review(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py review", description="Agent 代码审查")
    parser.add_argument("file")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--codebase-root")
    parser.add_argument("--no-memory", action="store_true", help="禁用历史误报记忆")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    from agent.memory import MemoryStore
    from agent.reviewer_agent import ReviewerAgent

    code = _read_file(args.file)
    if args.no_memory:
        agent = ReviewerAgent(codebase_root=args.codebase_root)
    else:
        agent = ReviewerAgent(codebase_root=args.codebase_root, memory=MemoryStore())
    report, outcome = agent.review(code, file_path=str(args.file))

    if args.format == "json":
        content = json.dumps(
            {"report": report.to_legacy(), "trace": outcome.run.to_trace_dict()},
            ensure_ascii=False,
            indent=2,
        )
    else:
        content = _render_review_text(Path(args.file).name, report, outcome.run)

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        print(f"Result saved to {args.output}")
    else:
        print(content)
    return 0


def cmd_optimize(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py optimize", description="Agent 代码优化")
    parser.add_argument("file")
    parser.add_argument("--test-file", help="验收测试文件（用于沙箱验证）")
    parser.add_argument("--codebase-root")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    from agent.optimizer_agent import OptimizerAgent

    code = _read_file(args.file)
    test_code = _read_file(args.test_file) if args.test_file else ""
    report, _ = _review_with_agent(code, str(args.file), args.codebase_root)

    optimizer = OptimizerAgent(codebase_root=args.codebase_root)
    result, outcome = optimizer.optimize(code, report, test_code=test_code)

    if args.format == "json":
        print(json.dumps(
            {"result": result.to_legacy(), "trace": outcome.run.to_trace_dict()},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("=== Verification ===")
        for check in result.verification.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"  [{mark}] {check.name}: {check.detail}")
        print(f"\nSummary: {result.summary}")
        print(f"Attempts: {result.attempts}  Changed: {result.changed}  "
              f"ChangeRatio: {result.change_ratio:.2%}")
        if result.patch:
            print("\n=== Patch ===")
            print(result.patch)
        print("\n=== Optimized Code ===")
        print(result.optimized_code)
    return 0


def cmd_multi(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py multi", description="多智能体协作审查")
    parser.add_argument("file")
    parser.add_argument("--codebase-root")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    from agent.multi_agent import MultiAgentReviewPipeline

    code = _read_file(args.file)
    pipeline = MultiAgentReviewPipeline(codebase_root=args.codebase_root, persist_traces=True)
    result = pipeline.review(code, file_path=str(args.file))

    if args.format == "json":
        print(json.dumps(
            {
                "report": result.report.to_legacy(),
                "stats": result.stats,
                "draft": result.draft.to_legacy(),
                "verdicts": [v.model_dump() for v in result.verdicts],
                "corroborations": [
                    {"index": c.index, "corroborated": c.corroborated,
                     "engines": c.engines, "note": c.note}
                    for c in result.corroborations
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    print(f"=== 多智能体审查报告: {Path(args.file).name} ===")
    print(f"Summary: {result.report.review_summary}\n")
    for index, issue in enumerate(result.report.issues, start=1):
        print(f"{index}. [{issue.dimension}] 行 {issue.line_number} — {issue.risk_description}")
        print(f"   Severity: {issue.severity.value}  Confidence: {issue.confidence:.2f}")
        if issue.verify_note:
            print(f"   Verify: {issue.verify_note}")
    print("\n--- 各角色统计 ---")
    for key, value in result.stats.items():
        print(f"  {key}: {value}")
    print("\n--- Critic 裁定 ---")
    verdict_map = {c.index: c for c in result.corroborations}
    for verdict in result.verdicts:
        corroboration = verdict_map.get(verdict.index)
        mark = "有佐证" if corroboration and corroboration.corroborated else "无佐证"
        print(f"  [{verdict.index}] {verdict.verdict:<10} ({mark}) {verdict.reason[:80]}")
    return 0


def cmd_trace(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py trace", description="查看运行轨迹")
    parser.add_argument("run_id")
    args = parser.parse_args(argv)

    path = Path("traces") / f"{Path(args.run_id).name}.jsonl"
    if not path.exists():
        print(f"未找到轨迹文件：{path}", file=sys.stderr)
        return 1
    for line in iter_run_lines(path):
        kind = line.get("type")
        if kind == "run_meta":
            print(f"run_id={line.get('run_id')}  task={line.get('task')}  "
                  f"stop={line.get('stop_reason')}  duration={line.get('duration_ms')}ms")
            print(f"usage={line.get('usage')}  cost={line.get('cost')}  tools={line.get('tool_calls')}")
        elif kind == "step":
            payload = line.get("tool_call")
            tool = payload.get("name") if isinstance(payload, dict) else ""
            print(f"  [{line.get('index'):>2}] {line.get('kind'):<7} {tool:<22} "
                  f"{str(line.get('content') or '')[:110]}")
        elif kind == "report":
            print(f"report: {line.get('review_summary')}  issues={len(line.get('issues') or [])}")
    return 0


def cmd_traces(_argv: List[str]) -> int:
    store = TraceStore()
    files = store.files()
    if not files:
        print("暂无运行记录。")
        return 0
    for path in files:
        meta = store.load_meta(path)
        print(f"{meta.get('run_id', path.stem):<44} task={meta.get('task', '-'):<9} "
              f"stop={meta.get('stop_reason', '-'):<16} tokens={meta.get('usage', {}).get('total_tokens', 0)}")
    return 0


def cmd_stats(_argv: List[str]) -> int:
    summary = TraceStore().summary()
    if not summary.get("runs"):
        print("暂无运行记录。")
        return 0
    print("=== Agent 运行聚合统计 ===")
    print(f"运行次数: {summary['runs']}")
    print(f"平均 token: {summary['avg_tokens']}")
    print(f"平均耗时: {summary['avg_duration_ms']} ms")
    print(f"平均成本: ${summary['avg_cost']}（未配置单价时为 0）")
    print(f"工具使用分布: {json.dumps(summary['tool_usage'], ensure_ascii=False)}")
    print(f"停止原因分布: {json.dumps(summary['stop_reasons'], ensure_ascii=False)}")
    return 0


def cmd_memory(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py memory", description="长期记忆管理")
    parser.add_argument("action", choices=["add", "list"])
    parser.add_argument("text", nargs="?", default="")
    parser.add_argument("--project", default="default")
    args = parser.parse_args(argv)

    from agent.memory import MemoryStore

    store = MemoryStore()
    if args.action == "add":
        if not args.text.strip():
            print("请提供要写入的约定内容，例如：cli.py memory add \"本项目禁止 print，统一用 logging\"")
            return 1
        record = store.add_convention(args.project, args.text)
        print(f"已写入记忆：{record.id}")
        return 0

    stats = store.stats()
    print(f"记忆库：{stats['path']}  共 {stats['total']} 条")
    print(f"按类型: {json.dumps(stats['by_kind'], ensure_ascii=False)}")
    for record in store.records[-30:]:
        print(f"  [{record.kind:<14}] [{record.project}] {record.text[:100]}")
    return 0


HANDLERS = {
    "review": cmd_review,
    "optimize": cmd_optimize,
    "multi": cmd_multi,
    "trace": cmd_trace,
    "traces": cmd_traces,
    "stats": cmd_stats,
    "memory": cmd_memory,
}


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        return HANDLERS[argv[0]](argv[1:])
    if not argv or argv[0] in ("-h", "--help", "--help-sub"):
        _print_subcommands()
        if argv and argv[0] in ("-h", "--help"):
            print("\n--- 无子命令调用 ---")
            run_legacy(["--help"])
        return 0
    return run_legacy(argv)


if __name__ == "__main__":
    raise SystemExit(main())
