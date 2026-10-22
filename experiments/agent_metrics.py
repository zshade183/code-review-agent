"""Agent 专项评估指标。

问题识别的 P/R/F1 是**子任务指标**，回答不了：工具选对了吗？绕了多少步？
花了多少 token/成本？为什么停下来？结论有证据吗？

本模块在给出 P/R/F1（与 ``exp1_recognition.py`` 相同的匹配口径，保证可比）的同时，
补齐 Agent 层指标：

============ ==========================================================
指标          含义
============ ==========================================================
task_success 单样本是否「干净地」完成任务：至少命中 1 个真实问题且精确率 ≥ 0.5
tool_acc     是否调用了必需工具（默认 ``run_static_analysis``，即先取证再下结论）
avg_steps    平均 Agent 步数（越低说明规划越直接）
avg_tools    平均工具调用次数
avg_tokens   平均 token 消耗
avg_cost     平均估算成本（需在配置中填写单价）
avg_ms       平均端到端耗时
stop_reason  停止原因分布（final / max_steps / no_progress / llm_error ...）
tool_usage   工具使用分布（能看到 Agent 实际依赖哪些能力）
evid_cov     结论的证据覆盖率（有 evidence 的问题占比）
verified     通过证据校验的问题占比
============ ==========================================================
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from agent.schema import ReviewReport
from agent.trace import estimate_cost

#: 判断「Agent 是否先取证」的必需工具
REQUIRED_TOOLS_DEFAULT = ("run_static_analysis",)


def parse_ints(value: Any) -> List[int]:
    """从 "3" / "3,5" / "3-5" 中解析出行号列表。"""
    if value is None:
        return []
    return [int(m.group(0)) for m in re.finditer(r"\d+", str(value))]


@dataclass
class MatchResult:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    by_category: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0


def _normalize_category(value: Any) -> str:
    if hasattr(value, "value"):  # Category 枚举
        value = value.value
    text = str(value or "").strip()
    mapping = {
        "Security Vulnerability": "security",
        "Performance Issue": "performance",
        "Code Smell": "smell",
        "Potential Bug": "bug",
    }
    return mapping.get(text, text.lower())


def _pred_category(pred: Any) -> str:
    """统一取出预测项的类别（同时接受 Issue 对象与扁平字典）。"""
    if isinstance(pred, dict):
        return _normalize_category(pred.get("category") or pred.get("dimension"))
    return _normalize_category(getattr(pred, "category", None))


def _pred_lines(pred: Any) -> List[int]:
    """统一取出预测项的行号（同时接受 Issue 对象与扁平字典）。"""
    if isinstance(pred, dict):
        return parse_ints(pred.get("line_number") or pred.get("line_start"))
    start = int(getattr(pred, "line_start", 1) or 1)
    end = int(getattr(pred, "line_end", start) or start)
    return [start, end] if end != start else [start]


def gt_has_lines(ground_truth: Sequence[Dict[str, Any]]) -> bool:
    """判断标注是否带有效行号。

    项目里有三套数据 schema，其中 BugSinPy/CodeFuse 转换出的 JSONL 只有
    "问题类别" 而没有行号（``line: null``）。这种情况下若仍强行按行号匹配，
    指标会被人为压到 0，因此必须退化为类别级匹配。
    """
    if not ground_truth:
        return False
    return all(
        (gt.get("line_start") or 0) > 0 or (gt.get("line_number") or 0) > 0
        for gt in ground_truth
    )


def match_issues(
    predicted: Sequence[Any],
    ground_truth: Sequence[Dict[str, Any]],
    require_line: bool = True,
) -> MatchResult:
    """匹配预测问题与标注问题。

    - ``require_line=True``：类别相同 + 行号落在 GT 行区间 ±1 内
      （口径与 ``exp1_recognition.py`` 的 ``evaluate_sample`` 一致，保证新旧结果可比）
    - ``require_line=False``：只要求类别相同（用于无行号标注的数据集）
    """
    result = MatchResult()
    matched_preds: set[int] = set()
    matched_gt: set[int] = set()
    by_category: Dict[str, Dict[str, int]] = {}
    pred_categories = [_pred_category(pred) for pred in predicted]
    pred_lines = [_pred_lines(pred) for pred in predicted]

    for gt_index, gt in enumerate(ground_truth):
        gt_cat = _normalize_category(gt.get("category"))
        row = by_category.setdefault(gt_cat, {"tp": 0, "count": 0})
        row["count"] += 1

        gt_start = max(1, int(gt.get("line_start") or gt.get("line_number") or 1) - 1)
        gt_end = int(gt.get("line_end") or gt_start) + 1

        for pred_index, pred in enumerate(predicted):
            if pred_index in matched_preds:
                continue
            if pred_categories[pred_index] != gt_cat:
                continue
            if require_line:
                lines = pred_lines[pred_index]
                if not lines or not any(gt_start <= line <= gt_end for line in lines):
                    continue
            result.tp += 1
            row["tp"] += 1
            matched_preds.add(pred_index)
            matched_gt.add(gt_index)
            break

    result.fp = len(predicted) - len(matched_preds)
    result.fn = len(ground_truth) - len(matched_gt)

    for cat, row in by_category.items():
        pred_count = sum(1 for category in pred_categories if category == cat)
        row["fp"] = max(0, pred_count - row["tp"])
        row["fn"] = max(0, row["count"] - row["tp"])
    result.by_category = by_category
    return result


def evaluate_report(
    report: ReviewReport,
    ground_truth: Sequence[Dict[str, Any]],
    run: Any = None,
    required_tools: Sequence[str] = REQUIRED_TOOLS_DEFAULT,
    sample_id: str = "",
    match_level: str = "auto",
) -> Dict[str, Any]:
    """对单样本的 Agent 运行做完整评估。

    ``match_level``：``auto`` 依据标注是否含行号自动选择；也可强制 ``line`` / ``category``。
    """
    issues = list(report.issues)
    if match_level == "line":
        require_line = True
    elif match_level == "category":
        require_line = False
    else:
        require_line = gt_has_lines(ground_truth)
    match = match_issues(issues, ground_truth, require_line=require_line)

    evidence_count = sum(1 for i in issues if i.evidence)
    verified_count = sum(1 for i in issues if i.verified is True)
    flagged_count = sum(1 for i in issues if i.verified is False)

    tool_names = list(run.tool_names) if run is not None else []
    tool_set = set(tool_names)
    tool_acc = (
        all(tool in tool_set for tool in required_tools) if required_tools else True
    )

    row: Dict[str, Any] = {
        "id": sample_id,
        "match_level": "line" if require_line else "category",
        "tp": match.tp,
        "fp": match.fp,
        "fn": match.fn,
        "precision": round(match.precision, 4),
        "recall": round(match.recall, 4),
        "f1": round(match.f1, 4),
        "by_category": match.by_category,
        "predicted": len(issues),
        "ground_truth": len(ground_truth),
        "evidence_coverage": round(evidence_count / len(issues), 4) if issues else 0.0,
        "verified_rate": round(verified_count / len(issues), 4) if issues else 0.0,
        "flagged": flagged_count,
        "task_success": bool(match.tp > 0 and match.precision >= 0.5),
    }

    if run is not None:
        cost = estimate_cost(run.usage)
        row.update(
            {
                "steps": len(run.steps),
                "tool_calls": len(tool_names),
                "tool_accuracy": tool_acc,
                "tool_usage": run.tool_call_stats(),
                "stop_reason": run.stop_reason,
                "tokens": run.usage.total_tokens,
                "prompt_tokens": run.usage.prompt_tokens,
                "completion_tokens": run.usage.completion_tokens,
                "cost": cost["total_cost"],
                "duration_ms": round(run.duration_ms, 2),
                "error": run.error,
            }
        )
        if run.output:
            row["dropped_issues"] = run.output.get("dropped_issues", 0)
            row["suppressed_by_memory"] = len(run.output.get("suppressed_by_memory") or [])
    return row


def _mean(values: Sequence[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def _std(values: Sequence[float]) -> float:
    return round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0


def aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把单样本结果聚合成 Agent 层指标汇总。"""
    if not rows:
        return {"samples": 0}

    tp = sum(int(r["tp"]) for r in rows)
    fp = sum(int(r["fp"]) for r in rows)
    fn = sum(int(r["fn"]) for r in rows)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    stop_counter: Dict[str, int] = {}
    tool_counter: Dict[str, int] = {}
    for row in rows:
        reason = str(row.get("stop_reason") or "unknown")
        stop_counter[reason] = stop_counter.get(reason, 0) + 1
        for name, count in (row.get("tool_usage") or {}).items():
            tool_counter[name] = tool_counter.get(name, 0) + int(count)

    # 分类别 P/R/F1
    by_category: Dict[str, Dict[str, float]] = {}
    categories = {cat for row in rows for cat in (row.get("by_category") or {})}
    for cat in sorted(categories):
        cat_tp = sum(int((row.get("by_category") or {}).get(cat, {}).get("tp", 0)) for row in rows)
        cat_fp = sum(int((row.get("by_category") or {}).get(cat, {}).get("fp", 0)) for row in rows)
        cat_fn = sum(int((row.get("by_category") or {}).get(cat, {}).get("fn", 0)) for row in rows)
        cat_p = cat_tp / (cat_tp + cat_fp) if (cat_tp + cat_fp) else 0.0
        cat_r = cat_tp / (cat_tp + cat_fn) if (cat_tp + cat_fn) else 0.0
        by_category[cat] = {
            "tp": cat_tp,
            "fp": cat_fp,
            "fn": cat_fn,
            "precision": round(cat_p, 4),
            "recall": round(cat_r, 4),
            "f1": round(2 * cat_p * cat_r / (cat_p + cat_r), 4) if (cat_p + cat_r) else 0.0,
        }

    durations = [float(r.get("duration_ms") or 0) for r in rows]
    match_levels: Dict[str, int] = {}
    for row in rows:
        level = str(row.get("match_level") or "unknown")
        match_levels[level] = match_levels.get(level, 0) + 1
    return {
        "samples": len(rows),
        # ---- 匹配口径（务必核对：category 级匹配比 line 级宽松，两者不可直接比较）----
        "match_level": (
            next(iter(match_levels)) if len(match_levels) == 1 else match_levels
        ),
        # ---- 子任务指标（识别能力）----
        "detection": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "by_category": by_category,
        },
        # ---- Agent 层指标（执行过程）----
        "agent": {
            #: 是否真的跑过 Agent（rules 基线模式没有执行轨迹，此时下面的指标无意义）
            "has_execution": any("steps" in r for r in rows),
            "task_success_rate": _mean([1.0 if r.get("task_success") else 0.0 for r in rows]),
            "tool_accuracy": _mean([1.0 if r.get("tool_accuracy") else 0.0 for r in rows]),
            "structured_output_rate": _mean(
                [1.0 if r.get("stop_reason") == "final" else 0.0 for r in rows]
            ),
            "avg_steps": _mean([float(r.get("steps") or 0) for r in rows]),
            "std_steps": _std([float(r.get("steps") or 0) for r in rows]),
            "avg_tool_calls": _mean([float(r.get("tool_calls") or 0) for r in rows]),
            "avg_tokens": _mean([float(r.get("tokens") or 0) for r in rows]),
            "avg_cost": _mean([float(r.get("cost") or 0) for r in rows]),
            "avg_duration_ms": _mean(durations),
            "p95_duration_ms": round(
                sorted(durations)[max(0, int(len(durations) * 0.95) - 1)], 2
            )
            if durations
            else 0.0,
            "evidence_coverage": _mean([float(r.get("evidence_coverage") or 0) for r in rows]),
            "verified_rate": _mean([float(r.get("verified_rate") or 0) for r in rows]),
            "issues_dropped_by_evidence": sum(int(r.get("dropped_issues") or 0) for r in rows),
            "issues_suppressed_by_memory": sum(
                int(r.get("suppressed_by_memory") or 0) for r in rows
            ),
            "stop_reasons": stop_counter,
            "tool_usage": dict(sorted(tool_counter.items(), key=lambda kv: -kv[1])),
        },
    }


def format_summary(summary: Dict[str, Any]) -> str:
    """把汇总渲染成便于贴进报告/PPT 的文本。"""
    if not summary.get("samples"):
        return "无有效样本。"
    detection = summary["detection"]
    agent = summary["agent"]
    lines = [
        f"样本数: {summary['samples']}   匹配口径: {summary.get('match_level')}",
        "",
        "[识别能力（子任务）]",
        f"  Precision {detection['precision']:.4f}   Recall {detection['recall']:.4f}   "
        f"F1 {detection['f1']:.4f}   (TP={detection['tp']} FP={detection['fp']} FN={detection['fn']})",
    ]
    for cat, row in detection["by_category"].items():
        lines.append(
            f"  - {cat:<12} P={row['precision']:.3f} R={row['recall']:.3f} F1={row['f1']:.3f} "
            f"(tp={row['tp']} fp={row['fp']} fn={row['fn']})"
        )
    if not agent.get("has_execution", True):
        lines.append("")
        lines.append("[Agent 执行过程] 未执行（rules 基线模式不调用 LLM，无运行轨迹）")
        return "\n".join(lines)

    lines += [
        "",
        "[Agent 执行过程]",
        f"  任务成功率        {agent['task_success_rate']:.4f}",
        f"  工具选择准确率    {agent['tool_accuracy']:.4f}",
        f"  结构化输出成功率  {agent['structured_output_rate']:.4f}",
        f"  平均步数          {agent['avg_steps']:.2f} (±{agent['std_steps']:.2f})",
        f"  平均工具调用      {agent['avg_tool_calls']:.2f}",
        f"  平均 token        {agent['avg_tokens']:.1f}",
        f"  平均成本          ${agent['avg_cost']:.6f}",
        f"  平均耗时          {agent['avg_duration_ms']:.0f} ms (p95 {agent['p95_duration_ms']:.0f} ms)",
        f"  证据覆盖率        {agent['evidence_coverage']:.4f}",
        f"  证据校验通过率    {agent['verified_rate']:.4f}",
        f"  因缺证据被丢弃    {agent['issues_dropped_by_evidence']} 条",
        f"  被记忆压制        {agent['issues_suppressed_by_memory']} 条",
        "",
        f"  停止原因分布      {agent['stop_reasons']}",
        f"  工具使用分布      {agent['tool_usage']}",
    ]
    return "\n".join(lines)
