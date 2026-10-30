"""命令行工具。

多 Agent 是默认主线，三种审查策略共享同一入口：

.. code-block:: powershell

    python cli.py review  demo.py                  # 默认 strategy=auto
    python cli.py review  demo.py --strategy fast  # 单 Agent（编辑器实时检查基线）
    python cli.py review  demo.py --strategy full  # 四领域 + Critic + Verifier
    python cli.py fix     demo.py --yes            # 审查 → 用户确认 → 修复管线
    python cli.py optimize demo.py                 # 兼容旧入口（等价于 fix 且自动确认）

兼容调用（``ci_integrate.sh`` 使用，输出字段与退出码保持稳定）::

    python cli.py <file> [--optimize] [--format text|json] [--output out.txt] [--fail-on-high-risk]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from agent.trace import TraceStore, iter_run_lines  # noqa: E402

SUBCOMMANDS = {
    "review",
    "fix",
    "optimize",
    "multi",
    "trace",
    "traces",
    "stats",
    "memory",
    "candidates",
}

STRATEGY_CHOICES = ("fast", "auto", "full")

#: 裁决状态 -> 中文标签（CLI 文本输出）
STATUS_LABELS = {
    "confirmed": "已确认",
    "needs_review": "待复核",
    "memory_suspected": "记忆存疑",
    "rejected": "已驳回",
}


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


def _build_pipeline(
    codebase_root: Optional[str] = None,
    use_memory: bool = True,
    project: str = "default",
    persist_traces: bool = True,
):
    from agent.pipelines import ReviewPipeline

    memory = None
    if use_memory:
        from agent.memory import MemoryStore

        memory = MemoryStore()
    return ReviewPipeline(
        codebase_root=codebase_root,
        memory=memory,
        project=project,
        persist_traces=persist_traces,
    )


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _pipeline_trace(result: Any) -> Dict[str, Any]:
    """把多角色轨迹压成 CI / 前端可消费的单一结构。"""
    runs = result.runs
    if not runs:
        return {"roles": {}, "steps": 0, "usage": {"total_tokens": 0}, "tool_calls": {}}
    if len(runs) == 1:
        return next(iter(runs.values())).to_trace_dict()
    total_tokens = sum(run.usage.total_tokens for run in runs.values())
    tool_calls: Dict[str, int] = {}
    stop_reasons: Dict[str, str] = {}
    for role, run in runs.items():
        stop_reasons[role] = run.stop_reason
        for name, count in run.tool_call_stats().items():
            tool_calls[name] = tool_calls.get(name, 0) + count
    return {
        "multi_agent": True,
        "strategy": result.strategy.value,
        "roles": {role: run.to_trace_dict() for role, run in runs.items()},
        "steps": sum(len(run.steps) for run in runs.values()),
        "tool_calls": dict(sorted(tool_calls.items(), key=lambda item: -item[1])),
        "usage": {"total_tokens": total_tokens},
        "stop_reasons": stop_reasons,
        "duration_ms": round(result.elapsed_ms, 2),
    }


def _render_pipeline_text(name: str, result: Any) -> str:
    """渲染多 Agent 审查结果（文本）。"""
    report = result.report
    stats = result.stats
    lines = [
        f"=== Code Review Report: {name} ===",
        f"Strategy: {result.strategy.value}   ({result.orchestration.plan.reason})",
        f"Summary: {report.review_summary}",
        "",
        "Issues Found:",
    ]
    for index, issue in enumerate(report.issues, start=1):
        status = issue.adjudication.value if issue.adjudication else "unadjudicated"
        lines.append(
            f"{index}. [{issue.dimension}] Line {issue.line_number}: {issue.risk_description}"
        )
        lines.append(
            f"   Severity: {issue.severity.value}  Confidence: {issue.confidence:.2f}  "
            f"Status: {STATUS_LABELS.get(status, status)} ({status})"
        )
        if issue.sources:
            lines.append(f"   Sources: {'/'.join(issue.sources)}  candidate_id={issue.candidate_id}")
        if not issue.is_subtype_valid():
            lines.append(
                f"   规范外候选: {issue.suggested_subtype or issue.subtype}"
                f"（不在 {issue.dimension} 白名单，确认后可执行 candidates promote）"
            )
        lines.append(f"   Suggestion: {issue.fix_suggestion}")
        for evidence in issue.evidence:
            source = evidence.tool or evidence.kind
            lines.append(f"   Evidence[{source}]: {evidence.detail or evidence.snippet}")
        if issue.verify_note:
            lines.append(f"   Verify: {issue.verify_note}")
        if issue.adjudication_reason:
            lines.append(f"   Adjudication: {issue.adjudication_reason}")
    if not report.issues:
        lines.append("  （无问题）")

    lines.append("")
    lines.append("--- Coverage（部分成功可见）---")
    for dimension, item in result.coverage.dimensions.items():
        lines.append(
            f"  {dimension:<12} {item.status:<10} issues={item.issues:<3} "
            f"elapsed={item.elapsed_ms:.0f}ms {item.detail}"
        )
    lines.append(f"  partial={result.coverage.partial}")
    lines.append("")
    lines.append("--- 裁决统计 ---")
    lines.append(f"  {json.dumps(stats.get('adjudication') or {}, ensure_ascii=False)}")
    verifier = stats.get("verifier") or {}
    lines.append(
        f"  verifier: status={stats.get('verifier_status')} "
        f"engines={verifier.get('engines_used')} corroborated={verifier.get('corroborated')}"
    )
    lines.append(f"  critic: {json.dumps(stats.get('critic') or {}, ensure_ascii=False)}")
    lines.append(f"  tokens={stats.get('tokens')} elapsed={stats.get('elapsed_ms')}ms")

    lines.append("")
    lines.append("--- Agent Trace ---")
    for role, run in result.runs.items():
        lines.append(
            f"  [{role:<12}] stop={run.stop_reason:<14} steps={len(run.steps):<3} "
            f"tools={sum(run.tool_call_stats().values()):<3} tokens={run.usage.total_tokens:<6} "
            f"duration={run.duration_ms / 1000:.2f}s"
        )
    if len(result.runs) == 1:
        only = next(iter(result.runs.values()))
        for step in only.steps:
            lines.append(f"      [{step.index:02d}] {step.kind:<7} {step.brief(160)}")
    return "\n".join(lines)


def _has_high_risk(report: Any) -> bool:
    return any(issue.risk_level == "高" for issue in report.issues)


def _write_or_print(content: str, output: Optional[str]) -> None:
    if output:
        Path(output).write_text(content, encoding="utf-8")
        print(f"Result saved to {output}")
    else:
        print(content)


# ---------------------------------------------------------------------------
# 无子命令调用（CI 兼容入口）
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
    parser.add_argument(
        "--strategy",
        choices=STRATEGY_CHOICES,
        default="auto",
        help="审查策略（默认 auto：按证据选择领域 Agent）",
    )

    args = parser.parse_args(argv)
    code = _read_file(args.file)
    pipeline = _build_pipeline(codebase_root=args.codebase_root, persist_traces=True)
    result = pipeline.review(code, file_path=str(args.file), strategy=args.strategy)

    output: Dict[str, Any] = {
        "file": str(args.file),
        "review": result.report.to_legacy(),
        "trace": _pipeline_trace(result),
        "strategy": result.strategy.value,
        "coverage": result.coverage.to_dict(),
        "stats": result.stats,
        "optimization": None,
    }

    optimization = None
    if args.optimize:
        from agent.pipelines import FixPipeline

        fixer = FixPipeline(codebase_root=args.codebase_root)
        plan = fixer.plan(code, result.report)
        optimization, opt_outcome = fixer.fix(code, plan)
        output["optimization"] = optimization.to_legacy()
        output["optimization_trace"] = opt_outcome.run.to_trace_dict()

    if args.format == "json":
        content = json.dumps(output, ensure_ascii=False, indent=2)
    else:
        content = _render_pipeline_text(Path(args.file).name, result)
        if optimization is not None:
            content += "\n\n=== Code Optimization ===\n"
            content += f"Summary: {optimization.summary}\n"
            content += f"Signature Match: {optimization.signature_match}\n"
            content += f"Verification: {json.dumps(optimization.verification.to_dict(), ensure_ascii=False)}\n"
            content += "\nOptimized Code:\n------------------\n"
            content += optimization.optimized_code
            content += "\n------------------\n"

    _write_or_print(content, args.output)

    if args.fail_on_high_risk and _has_high_risk(result.report):
        print("\n[CI/CD ERROR] High risk issues found! Blocking pipeline.", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _print_subcommands() -> None:
    print("可用子命令：")
    print("  review     <file>   多 Agent 审查（--strategy fast|auto|full，默认 auto）")
    print("  fix        <file>   审查 → 用户确认 → 修复管线（语法/接口/测试验证）")
    print("  optimize   <file>   兼容入口：审查后自动选择问题并修复")
    print("  multi      <file>   [已废弃] 等价于 review --strategy full")
    print("  trace      <run_id> 查看某次角色运行的完整轨迹")
    print("  traces              列出历史运行")
    print("  stats               聚合统计")
    print("  memory     （add|list）长期记忆管理")
    print("  candidates （list|promote）规范外候选子类型管理")


def cmd_review(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py review", description="多 Agent 代码审查")
    parser.add_argument("file")
    parser.add_argument("--strategy", choices=STRATEGY_CHOICES, default="auto")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--codebase-root")
    parser.add_argument("--project", default="default")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--no-memory", action="store_true", help="禁用历史误报记忆")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    code = _read_file(args.file)
    pipeline = _build_pipeline(
        codebase_root=args.codebase_root,
        use_memory=not args.no_memory,
        project=args.project,
        persist_traces=True,
    )
    result = pipeline.review(
        code, file_path=str(args.file), strategy=args.strategy, session_id=args.session_id
    )

    if args.format == "json":
        content = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    else:
        content = _render_pipeline_text(Path(args.file).name, result)
    _write_or_print(content, args.output)
    return 0


def _ask_confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes", "是"}


def cmd_fix(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py fix", description="审查后修复选中问题")
    parser.add_argument("file")
    parser.add_argument("--strategy", choices=STRATEGY_CHOICES, default="auto")
    parser.add_argument("--select", default="", help="逗号分隔的 candidate_id；缺省为全部已确认/待复核")
    parser.add_argument("--include-memory-suspected", action="store_true")
    parser.add_argument("--test-file", help="验收测试文件（用于沙箱验证）")
    parser.add_argument("--yes", action="store_true", help="跳过人工确认（CI 使用）")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--codebase-root")
    parser.add_argument("--project", default="default")
    args = parser.parse_args(argv)

    from agent.pipelines import FixPipeline

    code = _read_file(args.file)
    test_code = _read_file(args.test_file) if args.test_file else ""
    pipeline = _build_pipeline(codebase_root=args.codebase_root, use_memory=False, project=args.project)
    review = pipeline.review(code, file_path=str(args.file), strategy=args.strategy)
    selected = [item.strip() for item in args.select.split(",") if item.strip()]
    fixer = FixPipeline(codebase_root=args.codebase_root)
    plan = fixer.plan(
        code,
        review.report,
        candidate_ids=selected or None,
        include_memory_suspected=args.include_memory_suspected,
    )

    if args.format != "json":
        print("=== 修复计划 ===")
        print(f"审查策略: {review.strategy.value}  候选: {len(review.report.issues)} 条")
        for issue in plan.issues:
            status = issue.adjudication.value if issue.adjudication else "unadjudicated"
            print(
                f"  [{issue.candidate_id}] {STATUS_LABELS.get(status, status)} "
                f"{issue.category.value} / {issue.subtype} @行 {issue.line_start}"
            )
        for item in plan.excluded:
            print(f"  [跳过] {item['candidate_id']}: {item['reason']}")
        if plan.empty:
            print("没有可修复的问题。")
            return 0
        if not args.yes and not _ask_confirm(f"确认修复以上 {len(plan.issues)} 个问题？"):
            print("已取消，未执行 Fix Agent。")
            return 0

    result, outcome = fixer.fix(code, plan, test_code=test_code)

    if args.format == "json":
        print(json.dumps(
            {
                "plan": plan.to_dict(),
                "result": result.to_legacy(),
                "trace": outcome.run.to_trace_dict(),
                "review_strategy": review.strategy.value,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    print("\n=== Verification ===")
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


def cmd_optimize(argv: List[str]) -> int:
    """兼容入口：审查后自动选择问题并修复。"""
    parser = argparse.ArgumentParser(prog="cli.py optimize", description="Agent 代码优化（兼容入口）")
    parser.add_argument("file")
    parser.add_argument("--test-file", help="验收测试文件（用于沙箱验证）")
    parser.add_argument("--codebase-root")
    parser.add_argument("--strategy", choices=STRATEGY_CHOICES, default="auto")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    from agent.pipelines import FixPipeline

    code = _read_file(args.file)
    test_code = _read_file(args.test_file) if args.test_file else ""
    pipeline = _build_pipeline(codebase_root=args.codebase_root, use_memory=False)
    review = pipeline.review(code, file_path=str(args.file), strategy=args.strategy)

    fixer = FixPipeline(codebase_root=args.codebase_root)
    plan = fixer.plan(code, review.report, include_memory_suspected=True)
    result, outcome = fixer.fix(code, plan, test_code=test_code)

    if args.format == "json":
        print(json.dumps(
            {
                "result": result.to_legacy(),
                "trace": outcome.run.to_trace_dict(),
                "selected": [issue.candidate_id for issue in plan.issues],
            },
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
    """[已废弃] 等价于 ``review --strategy full``。"""
    print("[deprecated] `multi` 子命令已并入 `review --strategy full`，请改用统一入口。", file=sys.stderr)
    return cmd_review([*argv, "--strategy", "full"])


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


def cmd_candidates(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cli.py candidates", description="规范外候选子类型管理")
    parser.add_argument("action", choices=["list", "promote"])
    parser.add_argument("extra", nargs="*", help="promote 时依次为 <category> <subtype>")
    parser.add_argument("--project", default="default")
    parser.add_argument("--min-count", type=int, default=1, help="list 时只显示出现次数不少于该值的候选")
    args = parser.parse_args(argv)

    from agent.memory import MemoryStore
    from agent.schema import Category, allowed_subtypes

    store = MemoryStore()
    if args.action == "list":
        candidates = store.subtype_candidates(args.project, args.min_count)
        if not candidates:
            print("暂无规范外候选子类型。")
            return 0
        print(f"规范外候选子类型（共 {len(candidates)} 项）：")
        for item in candidates:
            print(
                f"  {item['count']} 次  [{item['category']}] {item['subtype']}"
                f"  最近 {item['last_seen']}"
            )
            for sample in item["samples"]:
                print(f"      例：{sample[:100]}")
        print(
            "\n确认是真实问题后，可执行：\n"
            '  python cli.py candidates promote "<分类>" "<子类型>"'
        )
        return 0

    if len(args.extra) < 2:
        print('用法：python cli.py candidates promote "<分类>" "<子类型>"')
        return 1
    category, subtype = args.extra[0], args.extra[1]
    names = {item.value for item in Category}
    if category not in names:
        print(f"分类必须为以下之一：{', '.join(sorted(names))}")
        return 1
    record = store.promote_subtype(args.project, category, subtype)
    print(f"已晋升白名单：{category} / {subtype}（记忆 {record.id}）")
    print(f"当前允许集合：{', '.join(allowed_subtypes(Category(category)))}")
    return 0


HANDLERS = {
    "review": cmd_review,
    "fix": cmd_fix,
    "optimize": cmd_optimize,
    "multi": cmd_multi,
    "trace": cmd_trace,
    "traces": cmd_traces,
    "stats": cmd_stats,
    "memory": cmd_memory,
    "candidates": cmd_candidates,
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
