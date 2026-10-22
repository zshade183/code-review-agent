"""实验 3 跨模型对比图：原生大模型 vs 本系统的代码修复能力。

读取多个模型各自跑出来的 ``reports/exp3*/report.json``（顶层直接是 ``llm_base`` / ``system``）。

用法::

    python experiments/exp3_comparison_plot.py
    python experiments/exp3_comparison_plot.py --metric fix_rate
    python experiments/exp3_comparison_plot.py --metric average --out reports/exp3_comparison_core.png
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Tuple

try:  # 直接运行脚本时
    from plot_style import SERIES_COLORS, annotate_bars, load_report, save_figure, use_style
except ImportError:  # 作为包导入时
    from experiments.plot_style import (  # type: ignore
        SERIES_COLORS,
        annotate_bars,
        load_report,
        save_figure,
        use_style,
    )

import matplotlib.pyplot as plt
import numpy as np

DEFAULT_MODELS: List[Tuple[str, str]] = [
    ("Doubao", "reports/exp3-doubao"),
    ("DeepSeek", "reports/exp3-deepseek"),
    ("GLM", "reports/exp3"),
]

#: 指标名 -> (中文说明, 数值格式)。average 为三项修复率的均值。
METRICS: Dict[str, Tuple[str, str]] = {
    "fix_rate": ("Fix Rate", "{:.1%}"),
    "ast_fix_rate": ("AST Fix Rate", "{:.1%}"),
    "exact_fix_rate": ("Exact Fix Rate", "{:.1%}"),
    "average": ("Average Fix Rate", "{:.1%}"),
}


def parse_models(items: List[str] | None) -> List[Tuple[str, Path]]:
    if not items:
        return [(name, Path(path)) for name, path in DEFAULT_MODELS]
    parsed: List[Tuple[str, Path]] = []
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--model 需要写成 名称=目录 的形式，收到：{item}")
        name, path = item.split("=", 1)
        parsed.append((name.strip(), Path(path.strip())))
    return parsed


def metric_value(block: Dict[str, float], metric: str) -> float | None:
    if metric == "average":
        keys = ("fix_rate", "ast_fix_rate", "exact_fix_rate")
        values = [block.get(key) for key in keys]
        if any(v is None for v in values):
            return None
        return float(sum(values) / len(values))
    value = block.get(metric)
    return None if value is None else float(value)


def collect(models: List[Tuple[str, Path]], metric: str) -> Tuple[List[str], List[float], List[float]]:
    names: List[str] = []
    baseline: List[float] = []
    system: List[float] = []
    for name, folder in models:
        report_path = folder / "report.json"
        if not report_path.exists():
            print(f"[跳过] {name}：找不到 {report_path}")
            continue
        report = load_report(report_path)
        base = metric_value(report.get("llm_base") or {}, metric)
        ours = metric_value(report.get("system") or {}, metric)
        if base is None or ours is None:
            print(f"[跳过] {name}：{report_path} 缺少 {metric} 所需字段")
            continue
        names.append(name)
        baseline.append(base)
        system.append(ours)
    return names, baseline, system


def main() -> int:
    parser = argparse.ArgumentParser(description="实验 3 跨模型修复能力对比图")
    parser.add_argument("--metric", choices=sorted(METRICS), default="average")
    parser.add_argument("--model", action="append", help="名称=目录，可重复；默认 Doubao/DeepSeek/GLM")
    parser.add_argument("--out", default="reports/exp3_comparison_core.png", help="输出图片路径")
    parser.add_argument("--title", default="Experiment 3: Code Optimization Performance", help="图标题")
    args = parser.parse_args()

    ylabel, fmt = METRICS[args.metric]
    names, baseline, system = collect(parse_models(args.model), args.metric)
    if not names:
        raise SystemExit("没有可用的模型报告，先用 experiments/run_all.py 跑出 reports/exp3*/report.json")

    use_style(figsize=(9, 6))
    fig, ax = plt.subplots()

    x = np.arange(len(names))
    width = 0.35
    bars_base = ax.bar(x - width / 2, baseline, width, label="Native LLM",
                       color=SERIES_COLORS[0], edgecolor="black", linewidth=0.8, alpha=0.95)
    bars_sys = ax.bar(x + width / 2, system, width, label="Our System",
                      color=SERIES_COLORS[1], edgecolor="black", linewidth=0.8, alpha=0.95)

    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.set_ylabel(ylabel, fontweight="bold")
    ax.set_title(args.title, fontweight="bold", pad=15)
    ax.legend(loc="upper left")
    annotate_bars(ax, bars_base, fmt, ymax=1.0, fontsize=11, fontweight="bold")
    annotate_bars(ax, bars_sys, fmt, ymax=1.0, fontsize=11, fontweight="bold")

    save_figure(fig, args.out)
    print(f"对比模型：{', '.join(names)}  指标：{args.metric}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
