"""Evaluate fast/auto/full and component ablations.

The script is intentionally dry-run by default. Pass ``--execute`` to make model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import LLMClient
from agent.pipelines import ReviewPipeline
from experiments.agent_metrics import aggregate, evaluate_report
from experiments.dataset_loader import Sample, load_dataset

ARMS = (
    "fast",
    "auto",
    "full",
    "full_no_critic",
    "full_no_verifier",
    "full_no_memory",
)


def ground_truth(sample: Sample) -> list[dict[str, Any]]:
    return [
        {
            "category": issue.category,
            "subtype": issue.subtype,
            "severity": issue.severity,
            "description": issue.description,
            "line_start": issue.line_start,
            "line_end": issue.line_end,
        }
        for issue in sample.issues
    ]


def build_pipeline(arm: str, client: LLMClient) -> tuple[ReviewPipeline, str, bool]:
    strategy = arm if arm in {"fast", "auto", "full"} else "full"
    use_memory = arm != "full_no_memory"
    pipeline = ReviewPipeline(client=client, use_memory=use_memory)
    if arm in {"full_no_critic", "full_no_verifier"}:
        original: Callable[..., Any] = pipeline.orchestrator.plan

        def plan(code: str, bundle: Any, selected: Any) -> Any:
            result = original(code, bundle, selected)
            return replace(
                result,
                critic_enabled=False if arm == "full_no_critic" else result.critic_enabled,
                verifier_enabled=False if arm == "full_no_verifier" else result.verifier_enabled,
            )

        pipeline.orchestrator.plan = plan  # type: ignore[method-assign]
    return pipeline, strategy, use_memory


def run_arm(arm: str, samples: list[Sample], client: LLMClient) -> dict[str, Any]:
    pipeline, strategy, use_memory = build_pipeline(arm, client)
    rows: list[dict[str, Any]] = []
    for sample in samples:
        started = time.perf_counter()
        try:
            result = pipeline.review(
                sample.code,
                file_path=f"{sample.sample_id}.py",
                strategy=strategy,
                use_memory=use_memory,
            )
            row = evaluate_report(
                result.report,
                ground_truth(sample),
                sample_id=sample.sample_id,
                required_tools=(),
            )
            row.update(
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                tokens=int(result.stats.get("tokens") or 0),
                partial=bool(result.coverage.partial),
                confirmed=int((result.stats.get("adjudication") or {}).get("confirmed", 0)),
                error="",
            )
        except Exception as exc:  # noqa: BLE001 - experiment must retain failed rows
            row = {
                "id": sample.sample_id, "tp": 0, "fp": 0, "fn": len(sample.issues),
                "precision": 0.0, "recall": 0.0, "f1": 0.0,
                "task_success": False, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "tokens": 0, "error": f"{type(exc).__name__}: {exc}",
            }
        rows.append(row)
    summary = aggregate(rows)
    summary["arm"] = arm
    summary["runtime"] = {
        "avg_duration_ms": round(statistics.fmean(float(row.get("duration_ms") or 0) for row in rows), 2),
        "avg_tokens": round(statistics.fmean(float(row.get("tokens") or 0) for row in rows), 2),
        "failures": sum(bool(row.get("error")) for row in rows),
    }
    return {"summary": summary, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/eval.json"))
    parser.add_argument("--out", type=Path, default=Path("reports/multi_agent_ablation.json"))
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--max-samples", type=int, default=50)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--execute", action="store_true", help="required safety switch; without it no experiment runs")
    args = parser.parse_args()
    protocol = {
        "dataset": str(args.dataset), "arms": args.arms, "max_samples": args.max_samples,
        "seed": args.seed, "metrics": ["precision", "recall", "f1", "tokens", "duration_ms", "partial"],
    }
    if not args.execute:
        print(json.dumps({"dry_run": True, "protocol": protocol}, ensure_ascii=False, indent=2))
        return
    samples, stats = load_dataset(args.dataset, language="python", max_samples=args.max_samples, seed=args.seed)
    client = LLMClient(mock=True) if args.mock else LLMClient()
    results = {arm: run_arm(arm, samples, client) for arm in args.arms}
    payload = {
        "protocol": {
            **protocol,
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "dataset_stats": stats,
            "sample_ids": [sample.sample_id for sample in samples],
            "mock": args.mock,
        },
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
