from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List

try:  # 直接运行脚本时
    from plot_style import radar_scale, use_style
except ImportError:  # 作为包导入时
    from experiments.plot_style import radar_scale, use_style  # type: ignore

import matplotlib.pyplot as plt


def load_report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


#: 径向刻度逻辑统一收敛到 plot_style.py；保留旧函数名，既有调用点无需改动
_compute_radar_scale = radar_scale


def bar_chart(summary: Dict[str, Any], out_path: Path) -> None:
    systems = ["pylint", "llm_base", "system"]
    labels = ["Pylint", "Native LLM", "Our System"]
    metrics = ["precision", "recall", "f1"]
    colors = ["#719AAC", "#E29135", "#72B063"]

    values: Dict[str, List[float]] = {m: [] for m in metrics}
    for s in systems:
        for m in metrics:
            values[m].append(float(summary[s].get(m, 0.0)))

    x = list(range(len(systems)))
    width = 0.16

    fig, ax = plt.subplots(figsize=(10.5, 5.6), dpi=200)
    for i, m in enumerate(metrics):
        ax.bar(
            [v + (i - 1) * width for v in x],
            values[m],
            width=width,
            label=m.upper(),
            color=colors[i],
            edgecolor="black",
            linewidth=0.8,
            alpha=0.95,
        )
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=14)
    ax.set_ylim(0, 1.05)
    ax.set_yticks([i * 0.05 for i in range(22)])  # 0, 0.05, 0.10, ..., 1.00, 1.05
    ax.tick_params(axis="y", labelsize=13)
    ax.set_ylabel("Score", fontsize=14)
    ax.set_title("Experiment 1: Issue Recognition", fontsize=16)
    ax.legend(fontsize=13)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)


def radar_chart(summary: Dict[str, Any], out_path: Path, metric: str = "recall", title_suffix: str = "Recall") -> None:
    systems = ["pylint", "llm_base", "system"]
    labels = ["Pylint", "Native LLM", "Our System"]
    types = ["security", "performance", "bug", "smell"]
    type_labels = ["Security", "Performance", "Bug", "Smell"]
    colors = ["#4C78A8", "#F58518", "#54A24B"]

    angles = [n / float(len(types)) * 2 * math.pi for n in range(len(types))]
    angles += angles[:1]

    fig = plt.figure(figsize=(8.8, 7.6), dpi=300)
    ax = plt.subplot(111, polar=True)

    ax.set_theta_offset(math.pi / 2)
    ax.set_theta_direction(-1)

    all_vals: List[float] = []
    series: List[tuple[int, List[float]]] = []
    for idx, s in enumerate(systems):
        by_type = summary[s].get("by_type", {}) or {}
        vals = [max(0.0, min(1.0, float((by_type.get(t, {}) or {}).get(metric, 0.0)))) for t in types]
        all_vals.extend(vals)
        vals += vals[:1]
        series.append((idx, vals))

    rmin, rmax, rticks = _compute_radar_scale(all_vals)
    for idx, vals in series:
        ax.plot(
            angles,
            vals,
            linewidth=2.6,
            label=labels[idx],
            color=colors[idx],
            marker="s",
            markersize=6.0,
            markerfacecolor=colors[idx],
            markeredgecolor=colors[idx],
        )

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(type_labels, fontsize=16)
    ax.tick_params(axis="x", pad=12)
    ax.set_ylim(rmin, rmax)
    ax.set_yticks(rticks)
    ax.set_yticklabels([f"{t:.2f}".rstrip("0").rstrip(".") for t in rticks], fontsize=16, color="0.35")
    ax.set_rlabel_position(0)
    ax.yaxis.grid(False)
    ax.xaxis.grid(True, color="0.7", linewidth=1.0)
    ax.spines["polar"].set_color("0.7")
    ax.set_title(f"Experiment 1: {title_suffix} by Dimension ", fontsize=20, fontweight="bold", pad=18)
    ax.legend(loc="upper right", bbox_to_anchor=(1.25, 1.08), fontsize=14)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)


def radar_chart_prf_combined(summary: Dict[str, Any], out_path: Path) -> None:
    systems = ["pylint", "llm_base", "system"]
    labels = ["Pylint", "Native LLM", "Our System"]
    types = ["security", "performance", "bug", "smell"]
    type_labels = ["Security", "Performance", "Bug", "Smell"]
    colors = ["#4C78A8", "#F58518", "#54A24B"]
    metrics = [("recall", "Recall"), ("precision", "Precision"), ("f1", "F1-Score")]

    angles = [n / float(len(types)) * 2 * math.pi for n in range(len(types))]
    angles += angles[:1]

    fig = plt.figure(figsize=(12.6, 9.2), dpi=300)
    gs = fig.add_gridspec(2, 2)
    axes = [
        fig.add_subplot(gs[0, :], polar=True),
        fig.add_subplot(gs[1, 0], polar=True),
        fig.add_subplot(gs[1, 1], polar=True),
    ]

    for i, (ax, (metric, metric_name)) in enumerate(zip(axes, metrics)):
        ax.set_theta_offset(math.pi / 2)
        ax.set_theta_direction(-1)

        for idx, s in enumerate(systems):
            by_type = summary[s].get("by_type", {}) or {}
            vals = [max(0.0, min(1.0, float((by_type.get(t, {}) or {}).get(metric, 0.0)))) for t in types]
            vals += vals[:1]
            ax.plot(
                angles,
                vals,
                linewidth=2.6,
                label=labels[idx],
                color=colors[idx],
                marker="s",
                markersize=6.0,
                markerfacecolor=colors[idx],
                markeredgecolor=colors[idx],
            )

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(type_labels, fontsize=14)
        ax.tick_params(axis="x", pad=12)
        ax.set_ylim(0.0, 1.0)
        rticks = [0.2, 0.4, 0.6, 0.8, 1.0]
        ax.set_yticks(rticks)
        ax.set_yticklabels([f"{t:.1f}".rstrip("0").rstrip(".") for t in rticks], fontsize=14, color="0.35")
        ax.set_rlabel_position(0)
        ax.yaxis.grid(False)
        ax.xaxis.grid(True, color="0.7", linewidth=1.0)
        ax.spines["polar"].set_color("0.7")
        pad = 8 if ax is axes[0] else 16
        ax.set_title(metric_name, fontsize=16, fontweight="bold", pad=pad)
        
        # 添加子图序号 (a), (b), (c)
        ax.text(0.5, -0.2, f"({chr(97 + i)})", ha='center', va='center', transform=ax.transAxes, fontsize=14, fontweight='bold')

    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc="upper right", bbox_to_anchor=(0.98, 0.985), fontsize=14)
    fig.suptitle("Experiment 1: PRF by Dimension ", fontsize=20, fontweight="bold", y=0.995)
    fig.subplots_adjust(left=0.05, right=0.92, top=0.84, bottom=0.18, wspace=0.20, hspace=0.30)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", dpi=300)
    plt.close(fig)


def radar_chart_all_metrics(summary: Dict[str, Any], out_dir: Path) -> None:
    """绘制所有指标的雷达图（Recall, Precision, F1）"""
    systems = ["pylint", "llm_base", "system"]
    labels = ["Pylint", "Native LLM", "Our System"]
    types = ["security", "performance", "bug", "smell"]
    type_labels = ["Security", "Performance", "Bug", "Smell"]
    colors = ["#4C78A8", "#F58518", "#54A24B"]
    metrics = [
        ("recall", "Recall"),
        ("precision", "Precision"),
        ("f1", "F1-Score")
    ]

    for metric, metric_name in metrics:
        angles = [n / float(len(types)) * 2 * math.pi for n in range(len(types))]
        angles += angles[:1]

        fig = plt.figure(figsize=(6.4, 5.6), dpi=140)
        ax = plt.subplot(111, polar=True)

        ax.set_theta_offset(math.pi / 2)
        ax.set_theta_direction(-1)

        all_vals: List[float] = []
        series: List[tuple[int, List[float]]] = []
        for idx, s in enumerate(systems):
            by_type = summary[s].get("by_type", {}) or {}
            vals = [max(0.0, min(1.0, float((by_type.get(t, {}) or {}).get(metric, 0.0)))) for t in types]
            all_vals.extend(vals)
            vals += vals[:1]
            series.append((idx, vals))

        rmin, rmax, rticks = _compute_radar_scale(all_vals)
        for idx, vals in series:
            ax.plot(
                angles,
                vals,
                linewidth=2.0,
                label=labels[idx],
                color=colors[idx],
                marker="s",
                markersize=4.5,
                markerfacecolor=colors[idx],
                markeredgecolor=colors[idx],
            )

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(type_labels)
        ax.tick_params(axis="x", pad=12)
        ax.set_ylim(rmin, rmax)
        ax.set_yticks(rticks)
        ax.set_yticklabels([f"{t:.2f}".rstrip("0").rstrip(".") for t in rticks], fontsize=9, color="0.35")
        ax.set_rlabel_position(0)
        ax.yaxis.grid(False)
        ax.xaxis.grid(True, color="0.7", linewidth=1.0)
        ax.spines["polar"].set_color("0.7")
        ax.set_title(f"Experiment 1: {metric_name} by Dimension")
        ax.legend(loc="upper right", bbox_to_anchor=(1.25, 1.08))
        fig.tight_layout()
        out_path = out_dir / f"{metric_name.lower()}_radar.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path)
        plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="reports/exp1/report.json")
    parser.add_argument("--out-dir", default="reports/exp1")
    args = parser.parse_args()

    use_style(figsize=(10, 6))
    report = load_report(Path(args.report))
    summary = report.get("summary", {})

    out_dir = Path(args.out_dir)
    bar_chart(summary, out_dir / "overall_prf.png")
    radar_chart_prf_combined(summary, out_dir / "prf_radar.png")
    
    # 可选：生成所有指标的雷达图
    # radar_chart_all_metrics(summary, out_dir)
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
