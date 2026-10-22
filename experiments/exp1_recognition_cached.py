from __future__ import annotations

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import argparse
import json
from typing import Any, Dict, List
from tqdm import tqdm

from core.dataset_processor import DatasetProcessor
from experiments.dataset_loader import GtIssue, Sample, load_dataset
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
            str(issue.get("violated_rule") or ""),
            str(issue.get("risk_description") or ""),
            str(issue.get("fix_suggestion") or ""),
        ]
    ).lower()
    if any(k in text for k in ["security", "vulnerability", "注入", "xss", "反序列化", "敏感", "漏洞"]):
        return "security"
    if any(k in text for k in ["performance", "性能", "低效", "复杂度"]):
        return "performance"
    if any(k in text for k in ["pep8", "命名", "unused", "未使用", "规范", "code smell", "smell", "资源泄漏", "magic"]):
        return "smell"
    return "bug"


def parse_ints(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    s = str(value)
    import re
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
    gt_issues: List[GtIssue], preds: List[Dict[str, Any]], require_line: bool
) -> tuple[int, int, int, Dict[str, Dict[str, int]]]:
    matched_pred: set[int] = set()
    tp = 0
    fn = 0
    by_type: Dict[str, Dict[str, int]] = {}

    for gt in gt_issues:
        gt_type = normalize_gt_category(gt.category)
        if gt_type not in by_type:
            by_type[gt_type] = {"tp": 0, "fp": 0, "fn": 0, "count": 0}
        by_type[gt_type]["count"] += 1

        matched = False
        for i, pred in enumerate(preds):
            if i in matched_pred:
                continue
            if match_issue(gt, pred, require_line):
                tp += 1
                matched_pred.add(i)
                by_type[gt_type]["tp"] += 1
                matched = True
                break
        if not matched:
            fn += 1
            by_type[gt_type]["fn"] += 1

    fp = len(preds) - len(matched_pred)
    for i, pred in enumerate(preds):
        if i not in matched_pred:
            pred_type = normalize_pred_category(pred)
            if pred_type not in by_type:
                by_type[pred_type] = {"tp": 0, "fp": 0, "fn": 0, "count": 0}
            by_type[pred_type]["fp"] += 1

    return tp, fp, fn, by_type


def process_sample_with_reviewer(sample: Sample, params: Dict) -> Dict:
    """使用审查器处理样本"""
    from core.reviewer import code_review
    
    review = code_review(sample.code)
    
    return {
        "sample_id": sample.sample_id,
        "review": review,
        "gt_issues": sample.issues
    }


def run_exp1_cached(
    dataset_path: str,
    max_workers: int = 4,
    enable_cache: bool = True,
    max_samples: int = None
) -> Dict[str, Any]:
    """
    运行实验1（带缓存和并发）
    
    Args:
        dataset_path: 数据集路径
        max_workers: 最大并发数
        enable_cache: 是否启用缓存
        max_samples: 最大样本数（用于测试）
    
    Returns:
        实验结果
    """
    print(f"加载数据集: {dataset_path}")
    samples = load_dataset(Path(dataset_path))
    
    if max_samples:
        samples = samples[:max_samples]
    
    print(f"加载了 {len(samples)} 个样本")
    
    # 创建处理器
    processor = DatasetProcessor(
        max_workers=max_workers,
        enable_cache=enable_cache,
        cache_ttl=7200  # 2小时
    )
    
    # 批量处理
    print("开始处理样本...")
    results = processor.process_batch(
        samples=samples,
        process_func=process_sample_with_reviewer,
        task_type="review",
        params={},
        show_progress=True
    )
    
    # 打印统计信息
    processor.print_stats()
    
    # 计算评估指标
    tp_total = 0
    fp_total = 0
    fn_total = 0
    by_type_total: Dict[str, Dict[str, int]] = {}
    
    for result in results:
        if result.success:
            review = result.result["review"]
            gt_issues = result.result["gt_issues"]
            
            if structured_ok(review):
                preds = review.get("issues", [])
                tp, fp, fn, by_type = score_sample(gt_issues, preds, require_line=False)
                
                tp_total += tp
                fp_total += fp
                fn_total += fn
                
                for t, metrics in by_type.items():
                    if t not in by_type_total:
                        by_type_total[t] = {"tp": 0, "fp": 0, "fn": 0, "count": 0}
                    by_type_total[t]["tp"] += metrics["tp"]
                    by_type_total[t]["fp"] += metrics["fp"]
                    by_type_total[t]["fn"] += metrics["fn"]
                    by_type_total[t]["count"] += metrics["count"]
    
    # 计算总体指标
    precision, recall, f1 = compute_prf(tp_total, fp_total, fn_total)
    
    # 计算各类型指标
    by_type_metrics = {}
    for t, metrics in by_type_total.items():
        t_precision, t_recall, t_f1 = compute_prf(
            metrics["tp"], metrics["fp"], metrics["fn"]
        )
        by_type_metrics[t] = {
            "precision": t_precision,
            "recall": t_recall,
            "f1": t_f1,
            "count": metrics["count"]
        }
    
    # 构建结果
    summary = {
        "samples": len(results),
        "tp": tp_total,
        "fp": fp_total,
        "fn": fn_total,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "by_type": by_type_metrics,
        "processor_stats": processor.get_stats()
    }
    
    # 保存结果
    output_path = Path("reports/exp1/report_cached.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    print(f"\n结果已保存到: {output_path}")
    
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True, help="数据集路径")
    parser.add_argument("--max-workers", type=int, default=4, help="最大并发数")
    parser.add_argument("--no-cache", action="store_true", help="禁用缓存")
    parser.add_argument("--max-samples", type=int, default=None, help="最大样本数")
    
    args = parser.parse_args()
    
    result = run_exp1_cached(
        dataset_path=args.dataset,
        max_workers=args.max_workers,
        enable_cache=not args.no_cache,
        max_samples=args.max_samples
    )
    
    print("\n实验结果:")
    print(f"Precision: {result['precision']:.4f}")
    print(f"Recall: {result['recall']:.4f}")
    print(f"F1 Score: {result['f1']:.4f}")
