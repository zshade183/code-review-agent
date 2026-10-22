from __future__ import annotations

import sys
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

matplotlib.use("Agg")

sys.path.append(str(Path(__file__).parent.parent))

try:  # 直接运行脚本时
    from plot_style import use_style
except ImportError:  # 作为包导入时
    from experiments.plot_style import use_style  # type: ignore


def _compute_radar_scale(all_values: List[float]) -> Tuple[float, float, List[float]]:
    clean = [float(v) for v in all_values if v is not None and np.isfinite(v)]
    if not clean:
        return 0.0, 1.0, [0.2, 0.4, 0.6, 0.8, 1.0]

    vmin = min(clean)
    vmax = max(clean)
    vmin = max(0.0, min(1.0, vmin))
    vmax = max(0.0, min(1.0, vmax))
    if vmax <= vmin:
        pad = 0.1 if vmax <= 0.9 else 0.02
        vmin = max(0.0, vmin - pad)
        vmax = min(1.0, vmax + pad)

    if vmin >= 0.7:
        rmin = 0.8
        rmax = min(1.0, float(np.ceil(vmax / 0.02) * 0.02))
        step = 0.02
    else:
        rmin = float(np.floor(vmin / 0.1) * 0.1)
        rmax = float(np.ceil(vmax / 0.1) * 0.1)
        rmin = max(0.0, rmin)
        rmax = min(1.0, rmax)
        span = rmax - rmin
        if span <= 0.2:
            step = 0.05
        elif span <= 0.5:
            step = 0.1
        else:
            step = 0.2

    rticks = np.arange(rmin + step, rmax + 1e-9, step).tolist()
    if not rticks:
        rticks = [rmax]
    return rmin, rmax, [float(t) for t in rticks]


def _load_report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="ignore"))


def _setup_style() -> None:
    # 统一走 plot_style，字号按原设置略微放大
    use_style(font_scale=1.1, figsize=(9, 6))


def _get_systems() -> List[Tuple[str, str, str]]:
    return [
        ("llm_base", "LLM Base", "#719AAC"),
        ("system", "Our System", "#72B063"),
    ]


def _metric(report: Dict[str, Any], sys_name: str, key: str, default: float = 0.0) -> float:
    v = (report.get(sys_name) or {}).get(key, default)
    try:
        return float(v)
    except Exception:
        return float(default)


def plot_comparison_summary(report: Dict[str, Any], out_dir: Path) -> None:
    systems = _get_systems()
    metrics: List[Tuple[str, str]] = [
        ("fix_rate", "Fix Rate"),
        ("ast_fix_rate", "AST Fix Rate"),
        ("exact_fix_rate", "Exact Fix Rate"),
        ("signature_match_original_rate", "Signature Match"),
        ("syntax_ok_rate", "Syntax OK Rate"),
        ("avg_time_s", "Avg Time (s)"),
    ]

    boot = report.get("bootstrap") or {}
    boot_metrics = (boot.get("metrics") or {}) if isinstance(boot, dict) else {}
    boot_map = {
        "fix_rate": "fix_success",
        "ast_fix_rate": "ast_match_fixed",
        "exact_fix_rate": "exact_fix",
    }

    fig, axes = plt.subplots(2, 3, figsize=(12.4, 6.6), dpi=300)
    axes = axes.flatten()
    for i, (key, title) in enumerate(metrics):
        ax = axes[i]
        values = [_metric(report, s[0], key) for s in systems]
        labels = [s[1] for s in systems]
        colors = [s[2] for s in systems]
        yerr = None
        if key in boot_map and isinstance(boot_metrics, dict) and boot_map[key] in boot_metrics:
            m = boot_metrics.get(boot_map[key]) or {}
            errs: List[List[float]] = [[], []]
            for sys_name, _, _ in systems:
                ci = ((m.get(sys_name) or {}).get("ci95") or [None, None])
                lo, hi = (ci[0], ci[1]) if isinstance(ci, list) and len(ci) == 2 else (None, None)
                mean = float(values[[s[0] for s in systems].index(sys_name)])
                if lo is None or hi is None:
                    errs[0].append(0.0)
                    errs[1].append(0.0)
                else:
                    errs[0].append(max(0.0, mean - float(lo)))
                    errs[1].append(max(0.0, float(hi) - mean))
            yerr = errs

        x = np.arange(len(labels), dtype=float) * 0.5
        bars = ax.bar(
            x,
            values,
            color=colors,
            edgecolor="black",
            linewidth=0.5,
            alpha=0.85,
            width=0.3,
            yerr=yerr,
            capsize=6 if yerr is not None else 0,
            ecolor="black",
        )
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=12)
        ax.tick_params(axis="y", labelsize=12)
        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.grid(axis="y", alpha=0.25, linestyle="--")
        if key.endswith("_rate") or key.endswith("_rate_api_ok") or key.endswith("_rate"):
            ax.set_ylim(0, 1.1)
            for b, v in zip(bars, values):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.03, f"{v:.2%}", ha="center", va="bottom", fontsize=14, fontweight="bold")
        elif key == "avg_time_s":
            ymax = max(values) if max(values) > 0 else 1.0
            ax.set_ylim(0, ymax * 1.35)
            for b, v in zip(bars, values):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + ymax * 0.03, f"{v:.3f}", ha="center", va="bottom", fontsize=14, fontweight="bold")
        else:
            for b, v in zip(bars, values):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.03, f"{v:.3f}", ha="center", va="bottom", fontsize=14, fontweight="bold")

    fig.suptitle("Experiment 3: Code Optimization Capability Comparison", fontsize=18, fontweight="bold", y=0.97)
    fig.subplots_adjust(left=0.06, right=0.99, bottom=0.08, top=0.90, wspace=0.28, hspace=0.40)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "comparison_summary.png", bbox_inches="tight", dpi=300)
    plt.close(fig)


def plot_radar(report: Dict[str, Any], out_dir: Path) -> None:
    systems = _get_systems()
    dims = [
        ("fix_rate", "Fix"),
        ("ast_fix_rate", "AST Fix"),
        ("signature_match_original_rate", "Signature"),
        ("syntax_ok_rate", "Syntax"),
        ("used_fallback_rate", "No Fallback"),
    ]

    angles = np.linspace(0, 2 * np.pi, len(dims), endpoint=False).tolist()
    angles += angles[:1]

    fig = plt.figure(figsize=(9, 9))
    ax = plt.subplot(111, polar=True)

    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)

    all_vals: List[float] = []
    series: List[Tuple[str, str, str, List[float]]] = []
    for sys_name, label, color in systems:
        vals: List[float] = []
        for key, _ in dims:
            v = _metric(report, sys_name, key)
            if key == "used_fallback_rate":
                v = 1.0 - v
            v = max(0.0, min(1.0, v))
            vals.append(v)
            all_vals.append(v)
        vals += vals[:1]
        series.append((sys_name, label, color, vals))

    rmin, rmax, rticks = _compute_radar_scale(all_vals)

    for _, label, color, vals in series:
        ax.plot(
            angles,
            vals,
            linewidth=2.2,
            label=label,
            color=color,
            marker="s",
            markersize=5,
            markerfacecolor=color,
            markeredgecolor=color,
        )

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels([d[1] for d in dims], fontsize=11)
    ax.tick_params(axis="x", pad=14)
    ax.set_ylim(rmin, rmax)
    ax.set_yticks(rticks)
    ax.set_yticklabels([f"{t:.2f}".rstrip("0").rstrip(".") for t in rticks], fontsize=9, color="0.35")
    ax.set_rlabel_position(0)
    ax.yaxis.grid(False)
    ax.xaxis.grid(True, color="0.7", linewidth=1.0)
    ax.spines["polar"].set_color("0.7")
    ax.set_title("Experiment 3: Multi-Dimensional Radar", fontsize=14, fontweight="bold", pad=20)
    ax.legend(loc="upper right", bbox_to_anchor=(1.25, 1.15))

    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "radar_chart.png", bbox_inches="tight")
    plt.close(fig)


def plot_time_vs_quality(report: Dict[str, Any], out_dir: Path) -> None:
    systems = _get_systems()
    fig, ax = plt.subplots(figsize=(10, 7))

    for sys_name, label, color in systems:
        t = _metric(report, sys_name, "avg_time_s")
        q = np.mean(
            [
                _metric(report, sys_name, "fix_rate"),
                _metric(report, sys_name, "ast_fix_rate"),
                _metric(report, sys_name, "signature_match_fixed_rate"),
            ]
        )
        ax.scatter([t], [q], s=260, color=color, edgecolor="black", linewidth=1.5, label=label, alpha=0.9)
        ax.annotate(f"{label}\n{t:.2f}s\n{q:.1%}", (t, q), textcoords="offset points", xytext=(10, 10), ha="left")

    ax.set_xlabel("Average Time (s)")
    ax.set_ylabel("Quality Score (mean of Fix/AST Fix/Signature)")
    ax.set_ylim(0, 1.05)
    ax.grid(alpha=0.25, linestyle="--")
    ax.legend(loc="lower right")
    ax.set_title("Experiment 3: Time vs Quality Trade-off", fontsize=14, fontweight="bold")

    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "time_vs_quality.png", bbox_inches="tight")
    plt.close(fig)


def plot_by_type(report: Dict[str, Any], out_dir: Path) -> None:
    systems = _get_systems()
    types: List[str] = []
    for sys_name, _, _ in systems:
        by = (report.get(sys_name) or {}).get("by_type") or {}
        for t in by.keys():
            if t not in types:
                types.append(t)
    types = sorted(types)
    if not types:
        return

    values = []
    for sys_name, _, _ in systems:
        by = (report.get(sys_name) or {}).get("by_type") or {}
        values.append([float((by.get(t) or {}).get("fix_rate", 0.0)) for t in types])

    x = np.arange(len(types))
    width = 0.24 if len(systems) <= 2 else 0.18
    offsets = (np.arange(len(systems)) - (len(systems) - 1) / 2.0) * width
    fig, ax = plt.subplots(figsize=(13.5, 7.8))
    for i, (sys_name, label, color) in enumerate(systems):
        ax.bar(x + offsets[i], values[i], width=width, label=label, color=color, edgecolor="black", linewidth=0.9, alpha=0.9)

    ax.set_xticks(x)
    ax.set_xticklabels([t.replace("_", " ").title() for t in types], rotation=15, ha="right", fontsize=12)
    ax.set_ylim(0, 1.05)
    ax.tick_params(axis="y", labelsize=12)
    ax.set_ylabel("Fix Rate", fontsize=13)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.set_title("Experiment 3: Fix Rate by Issue Type", fontsize=15, fontweight="bold")
    ax.legend(loc="upper right", fontsize=11)

    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "issue_distribution.png", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="reports/exp3/report.json")
    parser.add_argument("--out-dir", default="reports/exp3")
    args = parser.parse_args()

    _setup_style()
    report = _load_report(Path(args.report))
    out_dir = Path(args.out_dir)
    plot_comparison_summary(report, out_dir)
    plot_radar(report, out_dir)
    plot_time_vs_quality(report, out_dir)
    plot_by_type(report, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
