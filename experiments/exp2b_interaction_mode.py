"""
实验 2B：多轮审查 vs 单轮审查对比实验（全组件）

研究目标：验证“多轮审查”相较“单轮审查”的提升（其它 Prompt 组件保持完备）

固定启用的 Prompt 组件：
1. 角色强化 (REVIEWER_ROLE)
2. 链式推理 (REVIEW_COT)
3. 代码规则库 (REVIEW_RULES)
4. Few-shot 示例 (FEW_SHOT_EXAMPLES)

对比的交互模式：
1. single_turn：单轮审查（一次性输出完整 Prompt）
2. multi_turn：多轮审查（三轮交互逐步深化）
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import argparse
import concurrent.futures
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from tqdm import tqdm

from core import prompts
from experiments.dataset_loader import GtIssue, Sample, load_dataset
from experiments.llm_client import chat
from experiments.metrics import compute_prf


def normalize_gt_category(cat: str) -> str:
    if cat == "Security Vulnerability":
        return "security"
    if cat == "Performance Issue":
        return "performance"
    if cat == "Code Smell":
        return "smell"
    return "bug"


def normalize_pred_category(issue: Dict[str, Any]) -> str:
    text = " ".join(
        [
            str(issue.get("dimension") or ""),
            str(issue.get("category") or ""),
            str(issue.get("violated_rule") or ""),
            str(issue.get("risk_description") or ""),
            str(issue.get("fix_suggestion") or ""),
        ]
    ).lower()
    
    if any(k in text for k in ["security", "vulnerability", "注入", "xss", "反序列化", "敏感", "漏洞", "sql", "命令注入"]):
        return "security"
    
    if any(k in text for k in ["performance", "性能", "低效", "复杂度", "循环", "冗余", "内存泄漏", "数据结构"]):
        return "performance"
    
    if any(k in text for k in ["pep8", "命名", "unused", "未使用", "规范", "smell", "资源泄漏", "magic", "异常处理", "参数过多"]):
        return "smell"
    
    return "bug"


def parse_ints(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    s = str(value)
    nums = re.findall(r"\d+", s)
    out: List[int] = []
    for n in nums:
        try:
            out.append(int(n))
        except Exception:
            continue
    return out


def structured_ok(review: Any) -> bool:
    return isinstance(review, dict) and isinstance(review.get("issues"), list) and "review_summary" in review


def parse_confidence(value: Any) -> float:
    if value is None:
        return 1.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    s = str(value).strip()
    if not s:
        return 1.0
    try:
        return float(s)
    except Exception:
        return 0.0


def filter_predictions(preds: List[Dict[str, Any]], *, min_confidence: float, max_issues: int) -> List[Dict[str, Any]]:
    out = [p for p in preds if parse_confidence(p.get("confidence")) >= float(min_confidence)]
    out.sort(key=lambda p: parse_confidence(p.get("confidence")), reverse=True)
    if int(max_issues or 0) > 0:
        out = out[: int(max_issues)]
    return out


def match_issue(gt: GtIssue, pred: Dict[str, Any], require_line: bool) -> bool:
    gt_cat = normalize_gt_category(gt.category)
    pred_cat = normalize_pred_category(pred)
    if gt_cat != pred_cat:
        return False
    if not require_line:
        return True
    lns = parse_ints(pred.get("line_number"))
    if not lns:
        return False
    return any(gt.line_start <= ln <= gt.line_end for ln in lns)


def score_sample(
    gtissues: List[GtIssue], preds: List[Dict[str, Any]], require_line: bool
) -> Tuple[int, int, int, Dict[str, Dict[str, int]]]:
    matched_pred: set[int] = set()
    tp = 0
    fn = 0
    by_type: Dict[str, Dict[str, int]] = {}

    for gt in gtissues:
        gt_type = normalize_gt_category(gt.category)
        by_type.setdefault(gt_type, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
        by_type[gt_type]["count"] += 1

        hit = False
        for pi, pred in enumerate(preds):
            if pi in matched_pred:
                continue
            if match_issue(gt, pred, require_line=require_line):
                matched_pred.add(pi)
                hit = True
                tp += 1
                by_type[gt_type]["tp"] += 1
                break

        if not hit:
            fn += 1
            by_type[gt_type]["fn"] += 1

    fp = len(preds) - len(matched_pred)
    for pi, pred in enumerate(preds):
        if pi not in matched_pred:
            pred_type = normalize_pred_category(pred)
            by_type.setdefault(pred_type, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
            by_type[pred_type]["fp"] += 1
    
    return tp, fp, fn, by_type


def build_single_turn_prompt(code: str) -> List[Dict[str, str]]:
    parts: List[str] = [
        prompts.REVIEWER_ROLE,
        prompts.REVIEW_COT,
        prompts.REVIEW_RULES,
        prompts.FEW_SHOT_EXAMPLES,
        prompts.REVIEW_OUTPUT_FORMAT,
        f"""请审查下面的 Python 代码：
```python
{code}
```""",
    ]
    return [{"role": "user", "content": "\n\n".join(parts)}]


def build_multi_turn_prompt(code: str) -> List[Dict[str, str]]:
    parts: List[str] = [
        prompts.REVIEWER_ROLE,
        prompts.REVIEW_COT,
        prompts.REVIEW_RULES,
        prompts.REVIEW_MULTI_ROUND,
        prompts.FEW_SHOT_EXAMPLES,
        prompts.REVIEW_OUTPUT_FORMAT,
        f"""请严格按照上面的“多轮审查流程（Self-Correction）”在内部完成三轮审查，但只输出最终一份 JSON 结果（不要输出推理过程、不要输出中间过程）。
额外约束（降低误报）：除非变量来源明确来自外部/不可信输入（如 input/sys.argv/网络请求/文件解析/反序列化/DB 查询/dict.get 等），否则不要仅因缺少 None/类型校验就输出 "Null Pointer/None Check Missing" 或 "Type Error"。
```python
{code}
```""",
    ]
    return [{"role": "user", "content": "\n\n".join(parts)}]


def evaluate_single_sample(
    sample: Sample,
    require_line: bool,
    modes: List[str],
    *,
    min_confidence: float,
    max_issues: int,
) -> List[Dict[str, Any]]:
    """评估单个样本的多个交互模式"""
    results = []
    
    for mode in modes:
        t0 = time.time()
        
        if mode == "single_turn":
            messages = build_single_turn_prompt(sample.code)
            desc = "单轮审查（全组件）"
            review = chat(messages, temperature=0.2)
        elif mode == "multi_turn":
            messages = build_multi_turn_prompt(sample.code)
            desc = "多轮审查（全组件）"
            review = chat(messages, temperature=0.2)
        else:
            raise ValueError(f"未知模式：{mode}")
        elapsed = time.time() - t0
        
        preds = review.get("issues") if isinstance(review, dict) else []
        if preds is None:
            preds = []
        preds_list = [p for p in preds if isinstance(p, dict)]
        preds_list = filter_predictions(preds_list, min_confidence=min_confidence, max_issues=max_issues)
        tp, fp, fn, by_type = score_sample(sample.issues, preds_list, require_line=require_line)
        
        results.append({
            "mode": mode,
            "mode_desc": desc,
            "id": sample.sample_id,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "time_s": elapsed,
            "review": review,
            "ground_truth": [gt.__dict__ for gt in sample.issues],
            "by_type": by_type,
            "structured_ok": 1 if structured_ok(review) else 0,
        })
    
    return results


def evaluate(
    samples: List[Sample],
    require_line: bool,
    modes: List[str],
    *,
    workers: int = 1,
    min_confidence: float,
    max_issues: int,
) -> Dict[str, Any]:
    """评估多种交互模式（支持多线程）"""
    totals: Dict[str, Dict[str, Any]] = {}
    for m in modes:
        totals[m] = {
            "tp": 0, "fp": 0, "fn": 0, 
            "samples": 0, "structured_ok": 0, 
            "total_time": 0.0,
            "by_type": {}
        }

    details: List[Dict[str, Any]] = []
    mode_descs: Dict[str, str] = {}

    valid_samples = [s for s in samples if s.language.lower() == "python" and s.issues]
    total_tasks = len(valid_samples)
    completed = 0
    interrupted = False
    
    if workers > 1:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        future_to_sample: Dict[concurrent.futures.Future, Sample] = {}
        try:
            future_to_sample = {
                executor.submit(
                    evaluate_single_sample,
                    sample,
                    require_line,
                    modes,
                    min_confidence=min_confidence,
                    max_issues=max_issues,
                ): sample
                for sample in valid_samples
            }
            for future in tqdm(
                concurrent.futures.as_completed(future_to_sample),
                total=total_tasks,
                desc="Processing samples",
                unit="sample",
            ):
                try:
                    sample_results = future.result()
                    details.extend(sample_results)
                    
                    for result in sample_results:
                        mode = result["mode"]
                        totals[mode]["tp"] += result["tp"]
                        totals[mode]["fp"] += result["fp"]
                        totals[mode]["fn"] += result["fn"]
                        totals[mode]["samples"] += 1
                        totals[mode]["structured_ok"] += result["structured_ok"]
                        totals[mode]["total_time"] += result["time_s"]
                        mode_descs[mode] = result["mode_desc"]
                        
                        for t, m in result["by_type"].items():
                            totals[mode]["by_type"].setdefault(t, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
                            totals[mode]["by_type"][t]["tp"] += int(m.get("tp", 0))
                            totals[mode]["by_type"][t]["fp"] += int(m.get("fp", 0))
                            totals[mode]["by_type"][t]["fn"] += int(m.get("fn", 0))
                            totals[mode]["by_type"][t]["count"] += int(m.get("count", 0))
                    
                    completed += 1
                    if completed % 10 == 0:
                        tqdm.write(f"已完成 {completed}/{total_tasks} 个样本")
                        
                except Exception as e:
                    print(f"样本处理错误：{e}")
                    import traceback
                    traceback.print_exc()
        except KeyboardInterrupt:
            interrupted = True
            for f in future_to_sample:
                f.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        finally:
            if not interrupted:
                executor.shutdown(wait=True, cancel_futures=True)
    else:
        for sample in tqdm(valid_samples, desc="Processing samples", unit="sample"):
            sample_results = evaluate_single_sample(
                sample,
                require_line,
                modes,
                min_confidence=min_confidence,
                max_issues=max_issues,
            )
            details.extend(sample_results)
            
            for result in sample_results:
                mode = result["mode"]
                totals[mode]["tp"] += result["tp"]
                totals[mode]["fp"] += result["fp"]
                totals[mode]["fn"] += result["fn"]
                totals[mode]["samples"] += 1
                totals[mode]["structured_ok"] += result["structured_ok"]
                totals[mode]["total_time"] += result["time_s"]
                mode_descs[mode] = result["mode_desc"]
                
                for t, m in result["by_type"].items():
                    totals[mode]["by_type"].setdefault(t, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
                    totals[mode]["by_type"][t]["tp"] += int(m.get("tp", 0))
                    totals[mode]["by_type"][t]["fp"] += int(m.get("fp", 0))
                    totals[mode]["by_type"][t]["fn"] += int(m.get("fn", 0))
                    totals[mode]["by_type"][t]["count"] += int(m.get("count", 0))
            completed += 1

    summary: Dict[str, Any] = {}
    for mode, agg in totals.items():
        prf = compute_prf(agg["tp"], agg["fp"], agg["fn"])
        by_type_metrics: Dict[str, Any] = {}
        for t, m in agg["by_type"].items():
            by_type_metrics[t] = {**compute_prf(m["tp"], m["fp"], m["fn"]), "count": m["count"]}
        
        avg_time = agg["total_time"] / agg["samples"] if agg["samples"] else 0.0
        
        summary[mode] = {
            "description": mode_descs.get(mode, ""),
            "samples": agg["samples"],
            "tp": agg["tp"],
            "fp": agg["fp"],
            "fn": agg["fn"],
            **prf,
            "structured_output_rate": (agg["structured_ok"] / agg["samples"]) if agg["samples"] else 0.0,
            "avg_time_per_sample": round(avg_time, 3),
            "total_time_s": round(agg["total_time"], 2),
            "by_type": by_type_metrics,
        }

    return {
        "require_line": require_line,
        "summary": summary,
        "details": details,
        "scoring": {"min_confidence": float(min_confidence), "max_issues": int(max_issues)},
        "progress": {"completed_samples": int(completed), "total_samples": int(total_tasks), "interrupted": bool(interrupted)},
    }


def _bootstrap_delta_f1(
    details: List[Dict[str, Any]],
    base_mode: str,
    compare_mode: str,
    n_bootstrap: int,
    seed: int,
) -> Dict[str, float]:
    per_mode: Dict[str, Dict[str, Tuple[int, int, int]]] = {}
    sample_ids: set[str] = set()
    for d in details:
        sid = str(d.get("id") or "")
        mode = str(d.get("mode") or "")
        if not sid or not mode:
            continue
        per_mode.setdefault(mode, {})[sid] = (int(d.get("tp", 0)), int(d.get("fp", 0)), int(d.get("fn", 0)))
        sample_ids.add(sid)

    ids = sorted(sample_ids)
    if not ids:
        return {"delta_f1_mean": 0.0, "delta_f1_ci_low": 0.0, "delta_f1_ci_high": 0.0}

    base_map = per_mode.get(base_mode, {})
    cmp_map = per_mode.get(compare_mode, {})
    if len(base_map) != len(ids) or len(cmp_map) != len(ids):
        return {"delta_f1_mean": 0.0, "delta_f1_ci_low": 0.0, "delta_f1_ci_high": 0.0}

    def f1_from_counts(tp: int, fp: int, fn: int) -> float:
        prf = compute_prf(tp, fp, fn)
        return float(prf.get("f1", 0.0))

    rng = random.Random(seed)
    deltas: List[float] = []
    for _ in range(n_bootstrap):
        boot_ids = [ids[rng.randrange(0, len(ids))] for _ in range(len(ids))]
        b_tp = b_fp = b_fn = 0
        c_tp = c_fp = c_fn = 0
        for sid in boot_ids:
            t, f, n = base_map[sid]
            b_tp += t
            b_fp += f
            b_fn += n
            t, f, n = cmp_map[sid]
            c_tp += t
            c_fp += f
            c_fn += n
        deltas.append(f1_from_counts(c_tp, c_fp, c_fn) - f1_from_counts(b_tp, b_fp, b_fn))

    deltas.sort()
    mean = sum(deltas) / len(deltas)
    lo = deltas[int(0.025 * (len(deltas) - 1))]
    hi = deltas[int(0.975 * (len(deltas) - 1))]
    return {"delta_f1_mean": mean, "delta_f1_ci_low": lo, "delta_f1_ci_high": hi}


def main() -> int:
    parser = argparse.ArgumentParser(description="实验 2B：多轮审查 vs 单轮审查对比实验（全组件）")
    parser.add_argument("--dataset", default="dataset/eval.json", help="评估数据集路径")
    parser.add_argument("--out-dir", default="reports/exp2b_interaction", help="输出目录")
    parser.add_argument("--require-line", action="store_true", help="是否要求行号匹配")
    parser.add_argument("--max-samples", type=int, default=0, help="最大样本数（0 表示全部）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（用于数据集抽样/Bootstrap）")
    parser.add_argument("--modes", default="single_turn,multi_turn", help="交互模式列表，逗号分隔")
    parser.add_argument("--workers", type=int, default=None, help="工作线程数（默认：4 或 CPU 核心数）")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="评测过滤：最低置信度阈值")
    parser.add_argument("--max-issues", type=int, default=6, help="评测过滤：每个样本最多计入的预测条数（0 表示不限制）")
    parser.add_argument("--bootstrap", type=int, default=0, help="Bootstrap 次数（0 表示不做置信区间）")
    parser.add_argument("--details-level", choices=["full", "lite", "none"], default="full", help="输出细节级别")
    args = parser.parse_args()

    print("="*60)
    print("实验 2B：多轮审查 vs 单轮审查对比实验（全组件）")
    print("="*60)
    print(f"数据集：{args.dataset}")
    print(f"交互模式：{args.modes}")
    print(f"固定组件：REVIEWER_ROLE + REVIEW_COT + REVIEW_RULES + FEW_SHOT_EXAMPLES")
    print("="*60)
    print(f"评分过滤：min_confidence={float(args.min_confidence):.2f}, max_issues={int(args.max_issues)}")
    print("="*60)
    
    workers = int(args.workers or 0)
    if workers <= 0:
        workers = min(4, os.cpu_count() or 4)
    print(f"使用 {workers} 个线程进行并发处理")
    print("="*60)

    samples, dataset_stats = load_dataset(Path(args.dataset), max_samples=args.max_samples, seed=args.seed)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    report = evaluate(
        samples,
        require_line=args.require_line,
        modes=modes,
        workers=workers,
        min_confidence=float(args.min_confidence),
        max_issues=int(args.max_issues),
    )
    report["dataset"] = dataset_stats
    report["seed"] = args.seed

    if int(args.bootstrap or 0) > 0 and "single_turn" in report.get("summary", {}) and "multi_turn" in report.get("summary", {}):
        report["bootstrap"] = {
            "n_bootstrap": int(args.bootstrap),
            "base_mode": "single_turn",
            "compare_mode": "multi_turn",
            "delta_f1": _bootstrap_delta_f1(
                details=report.get("details", []),
                base_mode="single_turn",
                compare_mode="multi_turn",
                n_bootstrap=int(args.bootstrap),
                seed=args.seed,
            ),
        }

    if args.details_level == "lite":
        lite_details: List[Dict[str, Any]] = []
        for d in report.get("details", []):
            lite_details.append(
                {
                    "mode": d.get("mode"),
                    "id": d.get("id"),
                    "tp": d.get("tp"),
                    "fp": d.get("fp"),
                    "fn": d.get("fn"),
                    "time_s": d.get("time_s"),
                    "structured_ok": d.get("structured_ok"),
                    "by_type": d.get("by_type"),
                }
            )
        report["details"] = lite_details
    elif args.details_level == "none":
        report["details"] = []

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    
    print("\n" + "="*60)
    print("【实验结果汇总】")
    print("="*60)
    print(f"{'模式':<20} {'Precision':<10} {'Recall':<10} {'F1':<10} {'时间':<10}")
    print("-"*60)
    for mode, metrics in report["summary"].items():
        print(f"{mode:<20} {metrics['precision']:<10.4f} {metrics['recall']:<10.4f} {metrics['f1']:<10.4f} "
              f"{metrics['avg_time_per_sample']:<10.3f}s")
    
    print("\n【模式对比】")
    print("-"*60)
    if "single_turn" in report.get("summary", {}) and "multi_turn" in report.get("summary", {}):
        single_f1 = float(report["summary"]["single_turn"]["f1"])
        multi_f1 = float(report["summary"]["multi_turn"]["f1"])
        improvement = multi_f1 - single_f1
        improvement_pct = (improvement / single_f1) * 100 if single_f1 > 0 else 0.0
        single_time = float(report["summary"]["single_turn"]["avg_time_per_sample"])
        multi_time = float(report["summary"]["multi_turn"]["avg_time_per_sample"])
        time_increase = multi_time - single_time
        time_increase_pct = (time_increase / single_time) * 100 if single_time > 0 else 0.0
        print(f"多轮审查 vs 单轮审查:")
        print(f"  F1 提升：+{improvement:.4f} (+{improvement_pct:.1f}%)")
        print(f"  时间增加：+{time_increase:.3f}s (+{time_increase_pct:.1f}%)")
    
    print("\n详细报告已保存至:", out_dir / "report.json")
    print("="*60)
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
