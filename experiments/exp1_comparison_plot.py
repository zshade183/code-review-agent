"""实验 1 跨模型对比图：原生大模型 vs 本系统（F1）。

读取多个模型各自跑出来的 ``reports/exp1-*/report.json``，画一张分组柱状图。

用法::

    python experiments/exp1_comparison_plot.py
    python experiments/exp1_comparison_plot.py --metric f1 --out reports/exp1_comparison_f1.png
    python experiments/exp1_comparison_plot.py --model "GPT=reports/exp1-gpt" --model "GLM=reports/exp1-GLM"

默认取这三个目录（不存在的会自动跳过并提示，不会因为少一个模型就崩）。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Tuple

try:  # 直接运行脚本时
    from plot_style import COLORS, SERIES_COLORS, annotate_bars, load_report, save_figure, use_style
except ImportError:  # 作为包导入时
    from experiments.plot_style import (  # type: ignore
        COLORS,
        SERIES_COLORS,
        annotate_bars,
        load_report,
        save_figure,
        use_style,
    )

import matplotlib.pyplot as plt
import numpy as np

DEFAULT_MODELS: List[Tuple[str, str]] = [
    ("Doubao", "reports/exp1-doubao"),
    ("DeepSeek", "reports/exp1-deepseek"),
    ("GLM", "reports/exp1-GLM"),
]

#: 报告里可直接对比的指标（key 为 report.json 中的字段名）
METRICS: Dict[str, Tuple[str, str]] = {
    "f1": ("F1 Score", "{:.3f}"),
    "precision": ("Precision", "{:.3f}"),
    "recall": ("Recall", "{:.3f}"),
}


def parse_models(items: List[str] | None) -> List[Tuple[str, Path]]:
    """把 ``名称=目录`` 解析成列表；不传就用默认模型目录。"""
    if not items:
        return [(name, Path(path)) for name, path in DEFAULT_MODELS]
    parsed: List[Tuple[str, Path]] = []
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--model 需要写成 名称=目录 的形式，收到：{item}")
        name, path = item.split("=", 1)
        parsed.append((name.strip(), Path(path.strip())))
    return parsed


def collect(models: List[Tuple[str, Path]], metric: str) -> Tuple[List[str], List[float], List[float]]:
    names: List[str] = []
    baseline: List[float] = []
    system: List[float] = []
    for name, folder in models:
        report_path = folder / "report.json"
        if not report_path.exists():
            print(f"[跳过] {name}：找不到 {report_path}")
            continue
        summary = load_report(report_path).get("summary") or {}
        base = (summary.get("llm_base") or {}).get(metric)
        ours = (summary.get("system") or {}).get(metric)
        if base is None or ours is None:
            print(f"[跳过] {name}：{report_path} 里没有 {metric} 字段")
            continue
        names.append(name)
        baseline.append(float(base))
        system.append(float(ours))
    return names, baseline, system


def main() -> int:
    parser = argparse.ArgumentParser(description="实验 1 跨模型 F1 对比图")
    parser.add_argument("--metric", choices=sorted(METRICS), default="f1")
    parser.add_argument("--model", action="append", help="名称=目录，可重复；默认 Doubao/DeepSeek/GLM")
    parser.add_argument("--out", default="reports/exp1_comparison_f1.png", help="输出图片路径")
    parser.add_argument("--title", default="Experiment 1: Issue Recognition", help="图标题")
    parser.add_argument("--ylim", type=float, default=1.0, help="纵轴上限")
    args = parser.parse_args()

    ylabel, fmt = METRICS[args.metric]
    names, baseline, system = collect(parse_models(args.model), args.metric)
    if not names:
        raise SystemExit("没有可用的模型报告，先用 experiments/run_all.py 跑出 reports/exp1-*/report.json")

    use_style(figsize=(8, 5))
    fig, ax = plt.subplots()

    x = np.arange(len(names))
    width = 0.35
    bars_base = ax.bar(x - width / 2, baseline, width, label="Native LLM",
                       color=SERIES_COLORS[0], edgecolor="black", linewidth=0.8, alpha=0.95)
    bars_sys = ax.bar(x + width / 2, system, width, label="Our System",
                      color=SERIES_COLORS[1], edgecolor="black", linewidth=0.8, alpha=0.95)

    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.set_ylabel(ylabel)
    ax.set_ylim(0, args.ylim)
    ax.set_title(args.title, pad=15, fontweight="bold")
    ax.legend(loc="upper left")
    annotate_bars(ax, bars_base, fmt, ymax=args.ylim, fontsize=10)
    annotate_bars(ax, bars_sys, fmt, ymax=args.ylim, fontsize=10)

    save_figure(fig, args.out)
    print(f"对比模型：{', '.join(names)}  指标：{args.metric}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
