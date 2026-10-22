"""
实验 2A：Prompt 组件消融实验（单轮模式）

研究目标：量化 Prompt 内容组件的贡献

核心组件（5 个）：
1. 角色强化 (REVIEWER_ROLE)
2. 链式推理 (REVIEW_COT)
3. 代码规则库 (REVIEW_RULES)
4. Few-shot 示例 (FEW_SHOT_EXAMPLES)
5. 置信度字段 (confidence in output)

实验设计：组件消融法 (Subtractive Ablation)
- 从完整 Prompt 开始（单轮模式）
- 逐一移除组件
- 量化每个组件的贡献

注意：本实验固定使用单轮交互模式；多轮审查（多轮对话）效果在实验 2B 单独对比
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
from typing import Any, Dict, List, Optional, Tuple
from tqdm import tqdm

from core import prompts
from experiments.dataset_loader import GtIssue, Sample, load_dataset
from experiments.llm_client import chat
from experiments.metrics import compute_prf


def normalize_gt_category(cat: str) -> str:
    """标准化真实标注类别"""
    if cat == "Security Vulnerability":
        return "security"
    if cat == "Performance Issue":
        return "performance"
    if cat == "Code Smell":
        return "smell"
    return "bug"


def normalize_pred_category(issue: Dict[str, Any]) -> str:
    """标准化预测类别"""
    text = " ".join(
        [
            str(issue.get("dimension") or ""),
            str(issue.get("category") or ""),
            str(issue.get("violated_rule") or ""),
            str(issue.get("risk_description") or ""),
            str(issue.get("fix_suggestion") or ""),
        ]
    ).lower()
    
    # 安全类
    if any(k in text for k in ["security", "vulnerability", "注入", "xss", "反序列化", "敏感", "漏洞", "sql", "命令注入"]):
        return "security"
    
    # 性能类
    if any(k in text for k in ["performance", "性能", "低效", "复杂度", "循环", "冗余", "内存泄漏", "数据结构"]):
        return "performance"
    
    # 代码坏味
    if any(k in text for k in ["pep8", "命名", "unused", "未使用", "规范", "smell", "资源泄漏", "magic", "异常处理", "参数过多"]):
        return "smell"
    
    # 默认归为 Bug
    return "bug"


def parse_ints(value: Any) -> List[int]:
    """解析行号"""
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
    """验证结构化输出"""
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
    """匹配预测问题和真实标注"""
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
    """计算 TP/FP/FN"""
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


def build_ablation_prompt(strategy: str, code: str) -> Tuple[List[Dict[str, str]], str]:
    """
    基于组件消融构建 Prompt（单轮模式）
    
    核心组件：
    1. REVIEWER_ROLE - 角色强化
    2. REVIEW_COT - 链式推理
    3. REVIEW_RULES - 代码规则库
    4. FEW_SHOT_EXAMPLES - Few-shot 示例
    
    消融策略：
    - full: 完整 Prompt（所有组件）
    - w/o_role: 移除角色强化
    - w/o_cot: 移除链式推理
    - w/o_rules: 移除代码规则库
    - w/o_examples: 移除 Few-shot 示例
    - w/o_confidence: 移除置信度字段
    - baseline: 仅基础指令
    """
    
    desc_map = {
        "full": "完整 Prompt（角色 + 链式推理 + 规则库 + 示例 + 置信度）",
        "w/o_role": "移除角色强化",
        "w/o_cot": "移除链式推理",
        "w/o_rules": "移除代码规则库",
        "w/o_examples": "移除 Few-shot 示例",
        "w/o_confidence": "移除置信度字段",
        "baseline": "基线（仅基础指令）",
    }
    
    # 构建基础 Prompt 组件
    def build_base_prompt(
        include_role: bool = True,
        include_cot: bool = True,
        include_rules: bool = True,
        include_examples: bool = True,
        include_confidence: bool = True,
    ) -> str:
        components = []
        
        if include_role:
            components.append(prompts.REVIEWER_ROLE)

        if include_cot:
            components.append(prompts.REVIEW_COT)

        if include_rules:
            components.append(prompts.REVIEW_RULES)
        
        if include_examples:
            components.append(prompts.FEW_SHOT_EXAMPLES)

        components.append(prompts.REVIEW_OUTPUT_FORMAT if include_confidence else prompts.REVIEW_OUTPUT_FORMAT_NO_CONFIDENCE)
        
        components.append(f"""
请审查下面的 Python 代码：
```python
{code}
```""")
        
        return "\n\n".join(components)
    
    if strategy == "full":
        # 完整 Prompt：所有组件
        prompt = build_base_prompt(
            include_role=True,
            include_cot=True,
            include_rules=True,
            include_examples=True,
            include_confidence=True,
        )
        messages = [{"role": "user", "content": prompt}]
        
    elif strategy == "w/o_role":
        # 移除角色强化
        prompt = build_base_prompt(
            include_role=False,
            include_cot=True,
            include_rules=True,
            include_examples=True,
            include_confidence=True,
        )
        messages = [{"role": "user", "content": prompt}]
        
    elif strategy == "w/o_cot":
        # 移除链式推理
        prompt = build_base_prompt(
            include_role=True,
            include_cot=False,
            include_rules=True,
            include_examples=True,
            include_confidence=True,
        )
        messages = [{"role": "user", "content": prompt}]
        
    elif strategy == "w/o_rules":
        # 移除代码规则库
        prompt = build_base_prompt(
            include_role=True,
            include_cot=True,
            include_rules=False,
            include_examples=True,
            include_confidence=True,
        )
        messages = [{"role": "user", "content": prompt}]
        
    elif strategy == "w/o_examples":
        # 移除 Few-shot 示例
        prompt = build_base_prompt(
            include_role=True,
            include_cot=True,
            include_rules=True,
            include_examples=False,
            include_confidence=True,
        )
        messages = [{"role": "user", "content": prompt}]

    elif strategy == "w/o_confidence":
        prompt = build_base_prompt(
            include_role=True,
            include_cot=True,
            include_rules=True,
            include_examples=True,
            include_confidence=False,
        )
        messages = [{"role": "user", "content": prompt}]
        
    elif strategy == "baseline":
        # 基线：仅基础指令
        prompt = f"""请审查下面的 Python 代码，指出你认为重要的问题并给出修复建议。

请输出 JSON 格式：
{{
  "review_summary": "...",
  "issues": [
    {{
      "category": "Security Vulnerability/Performance Issue/Code Smell/Potential Bug",
      "line_number": 12,
      "risk_description": "...",
      "fix_suggestion": "..."
    }}
  ]
}}

代码：
```python
{code}
```"""
        messages = [{"role": "user", "content": prompt}]
        
    else:
        raise ValueError(f"未知策略：{strategy}")
    
    return messages, desc_map[strategy]


def evaluate_single_sample(
    sample: Sample,
    require_line: bool,
    strategies: List[str],
    *,
    min_confidence: float,
    max_issues: int,
) -> List[Dict[str, Any]]:
    """评估单个样本的多个策略"""
    results = []
    
    for strat in strategies:
        t0 = time.time()
        messages, desc = build_ablation_prompt(strat, sample.code)
        
        review = chat(messages, temperature=0.2)
        elapsed = time.time() - t0
        
        preds = review.get("issues") if isinstance(review, dict) else []
        if preds is None:
            preds = []
        preds_list = [p for p in preds if isinstance(p, dict)]
        preds_list = filter_predictions(preds_list, min_confidence=min_confidence, max_issues=max_issues)
        tp, fp, fn, by_type = score_sample(sample.issues, preds_list, require_line=require_line)
        
        results.append({
            "strategy": strat,
            "strategy_desc": desc,
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


def _bootstrap_delta_f1(
    details: List[Dict[str, Any]],
    base_strategy: str,
    compare_strategy: str,
    n_bootstrap: int,
    seed: int,
) -> Dict[str, float]:
    per_strategy: Dict[str, Dict[str, Tuple[int, int, int]]] = {}
    sample_ids: set[str] = set()
    for d in details:
        sid = str(d.get("id") or "")
        strat = str(d.get("strategy") or "")
        if not sid or not strat:
            continue
        per_strategy.setdefault(strat, {})[sid] = (int(d.get("tp", 0)), int(d.get("fp", 0)), int(d.get("fn", 0)))
        sample_ids.add(sid)

    ids = sorted(sample_ids)
    if not ids:
        return {"delta_f1_mean": 0.0, "delta_f1_ci_low": 0.0, "delta_f1_ci_high": 0.0}

    base_map = per_strategy.get(base_strategy, {})
    cmp_map = per_strategy.get(compare_strategy, {})
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
        deltas.append(f1_from_counts(b_tp, b_fp, b_fn) - f1_from_counts(c_tp, c_fp, c_fn))

    deltas.sort()
    mean = sum(deltas) / len(deltas)
    lo = deltas[int(0.025 * (len(deltas) - 1))]
    hi = deltas[int(0.975 * (len(deltas) - 1))]
    return {"delta_f1_mean": mean, "delta_f1_ci_low": lo, "delta_f1_ci_high": hi}


def evaluate(
    samples: List[Sample],
    require_line: bool,
    strategies: List[str],
    *,
    workers: int = 1,
    min_confidence: float,
    max_issues: int,
) -> Dict[str, Any]:
    """评估多种策略（支持多线程）"""
    totals: Dict[str, Dict[str, Any]] = {}
    for s in strategies:
        totals[s] = {
            "tp": 0, "fp": 0, "fn": 0, 
            "samples": 0, "structured_ok": 0, 
            "total_time": 0.0,
            "by_type": {}
        }

    details: List[Dict[str, Any]] = []
    strategy_descs: Dict[str, str] = {}

    valid_samples = [s for s in samples if s.language.lower() == "python" and s.issues]
    total_tasks = len(valid_samples)
    
    if workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_sample = {
                executor.submit(
                    evaluate_single_sample,
                    sample,
                    require_line,
                    strategies,
                    min_confidence=min_confidence,
                    max_issues=max_issues,
                ): sample
                for sample in valid_samples
            }
            
            completed = 0
            for future in tqdm(concurrent.futures.as_completed(future_to_sample), 
                             total=total_tasks, desc="Processing samples", unit="sample"):
                try:
                    sample_results = future.result()
                    details.extend(sample_results)
                    
                    for result in sample_results:
                        strat = result["strategy"]
                        totals[strat]["tp"] += result["tp"]
                        totals[strat]["fp"] += result["fp"]
                        totals[strat]["fn"] += result["fn"]
                        totals[strat]["samples"] += 1
                        totals[strat]["structured_ok"] += result["structured_ok"]
                        totals[strat]["total_time"] += result["time_s"]
                        strategy_descs[strat] = result["strategy_desc"]
                        
                        for t, m in result["by_type"].items():
                            totals[strat]["by_type"].setdefault(t, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
                            totals[strat]["by_type"][t]["tp"] += int(m.get("tp", 0))
                            totals[strat]["by_type"][t]["fp"] += int(m.get("fp", 0))
                            totals[strat]["by_type"][t]["fn"] += int(m.get("fn", 0))
                            totals[strat]["by_type"][t]["count"] += int(m.get("count", 0))
                    
                    completed += 1
                    if completed % 10 == 0:
                        tqdm.write(f"已完成 {completed}/{total_tasks} 个样本")
                        
                except Exception as e:
                    print(f"样本处理错误：{e}")
                    import traceback
                    traceback.print_exc()
    else:
        for sample in tqdm(valid_samples, desc="Processing samples", unit="sample"):
            sample_results = evaluate_single_sample(
                sample,
                require_line,
                strategies,
                min_confidence=min_confidence,
                max_issues=max_issues,
            )
            details.extend(sample_results)
            
            for result in sample_results:
                strat = result["strategy"]
                totals[strat]["tp"] += result["tp"]
                totals[strat]["fp"] += result["fp"]
                totals[strat]["fn"] += result["fn"]
                totals[strat]["samples"] += 1
                totals[strat]["structured_ok"] += result["structured_ok"]
                totals[strat]["total_time"] += result["time_s"]
                strategy_descs[strat] = result["strategy_desc"]
                
                for t, m in result["by_type"].items():
                    totals[strat]["by_type"].setdefault(t, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
                    totals[strat]["by_type"][t]["tp"] += int(m.get("tp", 0))
                    totals[strat]["by_type"][t]["fp"] += int(m.get("fp", 0))
                    totals[strat]["by_type"][t]["fn"] += int(m.get("fn", 0))
                    totals[strat]["by_type"][t]["count"] += int(m.get("count", 0))

    summary: Dict[str, Any] = {}
    for strat, agg in totals.items():
        prf = compute_prf(agg["tp"], agg["fp"], agg["fn"])
        by_type_metrics: Dict[str, Any] = {}
        for t, m in agg["by_type"].items():
            by_type_metrics[t] = {**compute_prf(m["tp"], m["fp"], m["fn"]), "count": m["count"]}
        
        avg_time = agg["total_time"] / agg["samples"] if agg["samples"] else 0.0
        
        summary[strat] = {
            "description": strategy_descs.get(strat, ""),
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

    return {"require_line": require_line, "summary": summary, "details": details, "scoring": {"min_confidence": float(min_confidence), "max_issues": int(max_issues)}}


def main() -> int:
    parser = argparse.ArgumentParser(description="实验 2A：Prompt 组件消融实验（单轮模式）")
    parser.add_argument("--dataset", default="dataset/eval.json", help="评估数据集路径")
    parser.add_argument("--out-dir", default="reports/exp2a_ablation", help="输出目录")
    parser.add_argument("--require-line", action="store_true", help="是否要求行号匹配")
    parser.add_argument("--max-samples", type=int, default=0, help="最大样本数（0 表示全部）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（用于数据集抽样/Bootstrap）")
    parser.add_argument("--strategies", default="full,w/o_role,w/o_cot,w/o_rules,w/o_examples,w/o_confidence,baseline", help="策略列表，逗号分隔")
    parser.add_argument("--workers", type=int, default=None, help="工作线程数（默认：4 或 CPU 核心数）")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="评测过滤：最低置信度阈值")
    parser.add_argument("--max-issues", type=int, default=6, help="评测过滤：每个样本最多计入的预测条数（0 表示不限制）")
    parser.add_argument("--bootstrap", type=int, default=0, help="Bootstrap 次数（0 表示不做置信区间）")
    parser.add_argument("--details-level", choices=["full", "lite", "none"], default="full", help="输出细节级别")
    args = parser.parse_args()

    print("="*60)
    print("实验 2A：Prompt 组件消融实验（单轮模式）")
    print("="*60)
    print(f"数据集：{args.dataset}")
    print(f"策略：{args.strategies}")
    print(f"核心组件：")
    print(f"  1. 角色强化 (REVIEWER_ROLE)")
    print(f"  2. 链式推理 (REVIEW_COT)")
    print(f"  3. 代码规则库 (REVIEW_RULES)")
    print(f"  4. Few-shot 示例 (FEW_SHOT_EXAMPLES)")
    print(f"  5. 置信度字段 (confidence)")
    print("="*60)
    print(f"交互模式：单轮对话（固定）")
    print("="*60)
    print(f"评分过滤：min_confidence={float(args.min_confidence):.2f}, max_issues={int(args.max_issues)}")
    print("="*60)
    
    workers = int(args.workers or 0)
    if workers <= 0:
        workers = min(4, os.cpu_count() or 4)
    print(f"使用 {workers} 个线程进行并发处理")
    print("="*60)

    samples, dataset_stats = load_dataset(Path(args.dataset), max_samples=args.max_samples, seed=args.seed)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    report = evaluate(
        samples,
        require_line=args.require_line,
        strategies=strategies,
        workers=workers,
        min_confidence=float(args.min_confidence),
        max_issues=int(args.max_issues),
    )
    report["dataset"] = dataset_stats
    report["seed"] = args.seed

    if int(args.bootstrap or 0) > 0 and "full" in report.get("summary", {}):
        boot: Dict[str, Any] = {"n_bootstrap": int(args.bootstrap), "base_strategy": "full", "delta_f1": {}}
        for strat in strategies:
            if strat == "full":
                continue
            if strat not in report["summary"]:
                continue
            boot["delta_f1"][strat] = _bootstrap_delta_f1(
                details=report.get("details", []),
                base_strategy="full",
                compare_strategy=strat,
                n_bootstrap=int(args.bootstrap),
                seed=args.seed,
            )
        report["bootstrap"] = boot

    if args.details_level == "lite":
        lite_details: List[Dict[str, Any]] = []
        for d in report.get("details", []):
            lite_details.append(
                {
                    "strategy": d.get("strategy"),
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
    print(f"{'策略':<20} {'Precision':<10} {'Recall':<10} {'F1':<10} {'时间':<10}")
    print("-"*60)
    for strat, metrics in report["summary"].items():
        print(f"{strat:<20} {metrics['precision']:<10.4f} {metrics['recall']:<10.4f} {metrics['f1']:<10.4f} "
              f"{metrics['avg_time_per_sample']:<10.3f}s")
    
    print("\n【组件贡献分析】")
    print("-"*60)
    if "full" in report.get("summary", {}):
        full_f1 = float(report["summary"]["full"]["f1"])
        for strat in ["w/o_role", "w/o_cot", "w/o_rules", "w/o_examples", "w/o_confidence"]:
            if strat in report["summary"]:
                strat_f1 = float(report["summary"][strat]["f1"])
                contribution = full_f1 - strat_f1
                contribution_pct = (contribution / full_f1) * 100 if full_f1 > 0 else 0.0
                print(f"{strat:<20} F1={strat_f1:.4f}  贡献=+{contribution:.4f} (+{contribution_pct:.1f}%)")
    
    print("\n详细报告已保存至:", out_dir / "report.json")
    print("="*60)
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
