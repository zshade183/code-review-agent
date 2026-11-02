"""Fair three-arm experiment: static baseline vs one LLM call vs full Agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import LLMClient
from agent.pipelines import ReviewPipeline
from agent.runtime import extract_json_block
from agent.schema import ReviewReport
from agent.tools.static_analysis import run_bandit, run_pylint, run_rules
from experiments.agent_metrics import aggregate, evaluate_report
from experiments.dataset_loader import Sample, _normalize_item, load_dataset

ARMS = ("baseline", "single_call", "full_agent")
SYSTEM_PROMPT = """你是代码审查器。只分析给定代码，不使用外部工具。
输出一个 JSON 对象，格式为 {"review_summary":"...","issues":[...]}。
每个问题必须包含 category、subtype、severity、line_start、line_end、
risk_description、fix_suggestion、confidence。category 只能是 Security Vulnerability、
Potential Bug、Code Smell、Performance Issue。不要输出 Markdown。"""


def ground_truth(sample: Sample) -> list[dict[str, Any]]:
    return [{"category": x.category, "subtype": x.subtype, "severity": x.severity,
             "description": x.description, "line_start": x.line_start,
             "line_end": x.line_end} for x in sample.issues]


def baseline_report(code: str) -> ReviewReport:
    issues = run_rules(code) + run_pylint(code) + run_bandit(code)
    best: dict[tuple[str, str, int], Any] = {}
    for item in issues:
        key = (item.category.value, item.subtype, item.line_start)
        if key not in best or item.confidence > best[key].confidence:
            best[key] = item
    return ReviewReport(review_summary="静态分析基线", issues=list(best.values()))


def single_call_report(client: LLMClient, code: str) -> tuple[ReviewReport, dict[str, Any]]:
    numbered = "\n".join(f"{i:>3}| {line}" for i, line in enumerate(code.splitlines(), 1))
    response = client.chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"审查以下 Python 代码：\n```python\n{numbered}\n```"},
    ], temperature=0.0)
    report = ReviewReport.model_validate(json.loads(extract_json_block(response.content))).dedupe()
    return report, {"tokens": response.usage.total_tokens,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "duration_ms": response.latency_ms, "model": response.model}


def run_arm(arm: str, samples: list[Sample], client: LLMClient) -> dict[str, Any]:
    reviewer = (
        ReviewPipeline(provider="mock" if getattr(client, "mock", False) else None, use_memory=False)
        if arm == "full_agent"
        else None
    )
    rows: list[dict[str, Any]] = []
    wall_start = time.perf_counter()
    for sample in samples:
        started = time.perf_counter()
        try:
            if arm == "baseline":
                report = baseline_report(sample.code)
                row = evaluate_report(report, ground_truth(sample), sample_id=sample.sample_id)
                row.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), tokens=0)
            elif arm == "single_call":
                report, usage = single_call_report(client, sample.code)
                row = evaluate_report(report, ground_truth(sample), sample_id=sample.sample_id)
                row.update(usage)
                row.update(steps=1, tool_calls=0)
            else:
                assert reviewer is not None
                result = reviewer.review(sample.code, file_path=f"{sample.sample_id}.py")
                report = result.report
                row = evaluate_report(report, ground_truth(sample), sample_id=sample.sample_id)
                row.update(duration_ms=result.elapsed_ms, steps=len(result.events), tool_calls=0)
            row["error"] = row.get("error") or ""
            row["predicted_issues"] = [item.model_dump(mode="json") for item in report.issues]
            row["sample_type"] = "clean" if not sample.issues else "single" if len(sample.issues) == 1 else "multi"
            row["exact_success"] = row["fp"] == 0 and row["fn"] == 0 and not row["error"]
            if not sample.issues:
                row["task_success"] = row["exact_success"]
        except Exception as exc:
            row = {"id": sample.sample_id, "tp": 0, "fp": 0, "fn": len(sample.issues),
                   "precision": 0.0, "recall": 0.0, "f1": 0.0, "match_level": "line",
                   "by_category": {}, "task_success": False,
                   "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                   "tokens": 0, "error": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
    summary = aggregate(rows)
    clean_rows = [r for r in rows if r.get("sample_type") == "clean"]
    summary["dataset_quality_metrics"] = {
        "exact_success_rate": sum(bool(r.get("exact_success")) for r in rows) / len(rows),
        "clean_samples": len(clean_rows),
        "clean_false_alarm_rate": (sum(r["fp"] > 0 for r in clean_rows) / len(clean_rows)
                                    if clean_rows else None),
    }
    summary["arm"] = arm
    summary["runtime"] = {
        "wall_seconds": round(time.perf_counter() - wall_start, 3),
        "successful_samples": sum(not row.get("error") for row in rows),
        "failed_samples": sum(bool(row.get("error")) for row in rows),
        "avg_tokens": round(statistics.fmean(float(row.get("tokens") or 0) for row in rows), 2),
        "avg_duration_ms": round(statistics.fmean(float(row.get("duration_ms") or 0)
                                                  for row in rows), 2),
    }
    return {"summary": summary, "rows": rows}


def write_markdown(path: Path, results: dict[str, Any], dataset: Path, seed: int) -> None:
    lines = ["# 三种代码审查方案对比", "",
             f"数据集：`{dataset.as_posix()}`；随机种子：`{seed}`；严格按类别与行号（±1）匹配。",
             "", "| 方案 | 样本 | Precision | Recall | F1 | 成功率 | 平均耗时(ms) | 平均Token | 失败 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    labels = {"baseline": "基线工具", "single_call": "单次模型调用", "full_agent": "完整 Agent"}
    for arm in ARMS:
        if arm not in results:
            continue
        summary = results[arm]["summary"]
        detection, agent, runtime = summary["detection"], summary["agent"], summary["runtime"]
        lines.append(f"| {labels[arm]} | {summary['samples']} | {detection['precision']:.4f} | "
                     f"{detection['recall']:.4f} | {detection['f1']:.4f} | "
                     f"{agent['task_success_rate']:.4f} | {runtime['avg_duration_ms']:.2f} | "
                     f"{runtime['avg_tokens']:.2f} | {runtime['failed_samples']} |")
    lines += ["", "## 实验控制", "",
              "- 三组使用完全相同的样本顺序、Ground Truth 和匹配函数。",
              "- 基线只运行规则、Pylint、Bandit；单次模型禁止工具且仅调用一次；完整 Agent 可多步调用工具并执行证据校验。",
              "- 默认关闭 Agent 长期记忆，避免历史信息造成数据泄漏。",
              "- JSON 报告保留逐样本结果、错误、耗时与 Token，便于复核而非只看均值。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/eval.json"))
    parser.add_argument("--out-dir", type=Path, default=Path("reports/exp1"))
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--max-samples", type=int, default=50)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--mock", action="store_true", help="使用离线 mock，仅用于流程冒烟测试")
    args = parser.parse_args()
    samples, dataset_stats = load_dataset(args.dataset, language="python",
                                          max_samples=args.max_samples, seed=args.seed)
    if not samples:
        raise SystemExit("数据集没有可评测样本")
    client = LLMClient(mock=True) if args.mock else LLMClient()
    results = {arm: run_arm(arm, samples, client) for arm in args.arms}
    payload = {"protocol": {"dataset": str(args.dataset), "seed": args.seed,
                            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                            "sample_ids": [s.sample_id for s in samples], "arms": args.arms,
                            "match": "category + line ±1", "dataset_stats": dataset_stats,
                            "mock": args.mock}, "results": results}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    write_markdown(args.out_dir / "summary.md", results, args.dataset, args.seed)
    print((args.out_dir / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
