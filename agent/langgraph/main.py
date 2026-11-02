"""LangGraph 正式审查管线的独立命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.pipelines import ReviewPipeline


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.langgraph.main")
    parser.add_argument("--file", required=True)
    parser.add_argument("--strategy", choices=("fast", "auto", "full"), default="auto")
    parser.add_argument("--codebase-root")
    parser.add_argument("--project", default="default")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    path = Path(args.file)
    code = path.read_text(encoding="utf-8")
    result = ReviewPipeline(
        codebase_root=args.codebase_root,
        project=args.project,
        use_memory=not args.no_memory,
        provider="mock" if args.mock else None,
    ).review(code, file_path=str(path), strategy=args.strategy)
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.report.review_summary)
        for issue in result.report.issues:
            print(f"[{issue.adjudication.value}] {issue.dimension} 行 {issue.line_number}: {issue.risk_description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
