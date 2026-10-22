"""实验图表的统一风格与公共绘图组件。

所有绘图脚本共用这一份样式：字体、字号、配色、坐标轴与导出 DPI 保持一致，
数值标注、雷达刻度这类重复逻辑也收敛在这里。

用法::

    try:                                  # 直接运行脚本时
        from plot_style import use_style, save_figure, annotate_bars
    except ImportError:                   # 作为包导入时
        from experiments.plot_style import use_style, save_figure, annotate_bars

    use_style()
    fig, ax = plt.subplots()
    ...
    save_figure(fig, out_path)
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")  # 无显示环境也要能出图

import matplotlib.pyplot as plt  # noqa: E402

#: 字体优先用中文字体（同时覆盖拉丁字符），避免中文标签变方框
FONT_STACK: Tuple[str, ...] = ("Microsoft YaHei", "SimHei", "DejaVu Sans", "Arial")

#: 主配色：基线 / 本系统 / 强调色，取自 Tableau 10 的稳定色
COLORS: Dict[str, str] = {
    "baseline": "#4C78A8",
    "system": "#54A24B",
    "accent": "#E17055",
    "muted": "#9E9E9E",
    "warn": "#F58518",
}

#: 多序列分组柱状图的默认循环配色
SERIES_COLORS: Tuple[str, ...] = ("#4C78A8", "#54A24B", "#E17055", "#F58518", "#9E7551")

#: 每个实验报告统一用一套颜色表示「原生模型 / 本系统」
SYSTEM_COLORS: Dict[str, str] = {
    "llm_base": COLORS["baseline"],
    "system": COLORS["system"],
}

SYSTEM_LABELS: Dict[str, str] = {
    "llm_base": "Native LLM",
    "system": "Our System",
}


def use_style(
    font_scale: float = 1.0,
    figsize: Tuple[float, float] = (8.0, 5.0),
    grid: str = "y",
    dpi: int = 300,
) -> None:
    """应用统一图表风格（字号、字体、网格、导出 DPI）。

    Args:
        font_scale: 字号缩放系数；论文插图想要更大字号时传 1.2 即可。
        figsize: 默认画布尺寸（英寸）。
        grid: 网格方向，``"y"`` / ``"x"`` / ``""``（关闭）。
        dpi: 导出分辨率。
    """
    plt.rcParams.update({
        "font.sans-serif": list(FONT_STACK),
        "font.family": "sans-serif",
        "axes.unicode_minus": False,
        "font.size": 12 * font_scale,
        "axes.titlesize": 14 * font_scale,
        "axes.labelsize": 12.5 * font_scale,
        "xtick.labelsize": 11 * font_scale,
        "ytick.labelsize": 11 * font_scale,
        "legend.fontsize": 11 * font_scale,
        "figure.figsize": figsize,
        "figure.dpi": 150,
        "savefig.dpi": dpi,
        "savefig.bbox": "tight",
        "savefig.facecolor": "white",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": bool(grid),
        "axes.grid.axis": grid or "y",
        "grid.alpha": 0.25,
        "grid.linestyle": "--",
    })


def load_report(path: Path | str) -> Dict[str, Any]:
    """读取实验报告 JSON。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"报告不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_figure(fig: Any, path: Path | str, note: str = "") -> Path:
    """保存图片：自动建目录、统一 300 DPI、关闭画布。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    print(f"已保存{(' ' + note) if note else ''}：{path}")
    return path


def annotate_bars(
    ax: Any,
    bars: Iterable[Any],
    fmt: str = "{:.3f}",
    offset_ratio: float = 0.02,
    ymax: float | None = None,
    **text_kwargs: Any,
) -> None:
    """在柱子上标注数值。

    Args:
        fmt: 数值格式，例如 ``"{:.3f}"`` 或 ``"{:.2%}"``。
        offset_ratio: 偏移量按坐标轴范围的百分比计算，避免写死绝对值导致大字不留白、
            小字压柱子。
        ymax: 坐标轴上限；不传时从 ``ax.get_ylim()`` 推断。
    """
    if ymax is None:
        ymax = ax.get_ylim()[1] or 1.0
    offset = ymax * offset_ratio
    kwargs = {"ha": "center", "va": "bottom", "fontsize": 10}
    kwargs.update(text_kwargs)
    for bar in bars:
        height = bar.get_height()
        if height is None or (isinstance(height, float) and math.isnan(height)):
            continue
        ax.text(bar.get_x() + bar.get_width() / 2.0, height + offset, fmt.format(height), **kwargs)
    # 给标签留出空间，避免贴到画布上边缘
    ax.set_ylim(0, ymax * (1 + offset_ratio * 3))


def radar_scale(values: Sequence[float]) -> Tuple[float, float, List[float]]:
    """雷达图径向刻度：按数值区间自适应选取底数与步长。

    这段逻辑原本在 exp1_plot / exp2a_plot / exp3_plot 里各复制了一份，
    三份实现完全一样却各自演进（一处用 math、一处用 np）。这里抽成一份，
    保持产出完全一致，避免「同一张雷达图在不同实验里刻度不同」。

    返回 ``(rmin, rmax, ticks)``。
    """
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return 0.0, 1.0, [0.2, 0.4, 0.6, 0.8, 1.0]

    vmin = max(0.0, min(1.0, min(clean)))
    vmax = max(0.0, min(1.0, max(clean)))
    if vmax <= vmin:
        pad = 0.1 if vmax <= 0.9 else 0.02
        vmin = max(0.0, vmin - pad)
        vmax = min(1.0, vmax + pad)

    if vmin >= 0.7:
        # 指标整体偏高：放大到 0.8~1.0 这一段，细粒度 0.02
        rmin = 0.8
        rmax = min(1.0, math.ceil(vmax / 0.02) * 0.02)
        step = 0.02
    else:
        rmin = max(0.0, math.floor(vmin / 0.1) * 0.1)
        rmax = min(1.0, math.ceil(vmax / 0.1) * 0.1)
        span = rmax - rmin
        if span <= 0.2:
            step = 0.05
        elif span <= 0.5:
            step = 0.1
        else:
            step = 0.2

    ticks: List[float] = []
    tick = rmin + step
    while tick <= rmax + 1e-9:
        ticks.append(round(tick, 10))
        tick += step
    if not ticks:
        ticks = [rmax]
    return float(rmin), float(rmax), [float(x) for x in ticks]
