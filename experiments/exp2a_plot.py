"""
实验 2A：Prompt 组件消融实验可视化

生成图表：
1. 组件贡献对比图（柱状图）
2. PRF 指标对比图
3. 按类别 F1 对比图
4. 时间 - 性能权衡图
5. 雷达图：多维度对比
"""

import json
from pathlib import Path

try:  # 直接运行脚本时
    from plot_style import radar_scale, use_style
except ImportError:  # 作为包导入时
    from experiments.plot_style import radar_scale, use_style  # type: ignore

import matplotlib.pyplot as plt
import numpy as np


def load_report(report_path: str) -> dict:
    """加载实验报告"""
    with open(report_path, 'r', encoding='utf-8') as f:
        return json.load(f)


#: 径向刻度逻辑统一收敛到 plot_style.py
_compute_radar_scale = radar_scale


def plot_component_contribution(summary: dict, out_path: str, bootstrap: dict | None = None):
    """
    图 1：F1 对比图（包含 baseline）
    """
    strategies = ["baseline", "full", "w/o_role", "w/o_cot", "w/o_rules", "w/o_examples", "w/o_confidence"]
    strategies = [s for s in strategies if s in summary]

    labels = []
    f1_scores = []
    for strat in strategies:
        labels.append(strat.replace("w/o_", ""))
        f1_scores.append(float(summary[strat].get("f1", 0.0)))

    fig, ax = plt.subplots(figsize=(14.8, 7.6))

    x = np.arange(len(labels))
    width = 0.5
    colors = []
    for strat in strategies:
        if strat == "baseline":
            colors.append("#94C6CD")
        elif strat == "full":
            colors.append("#72B063")
        else:
            colors.append("#719AAC")

    bars = ax.bar(x, f1_scores, width, color=colors, alpha=0.9, edgecolor="black", linewidth=0.9)

    ax.set_xlabel("Strategy", fontsize=18)
    ax.set_ylabel("F1 Score", fontsize=18)
    ax.set_title("Experiment 2A: F1 Comparison (Baseline + Ablations)", fontsize=18)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15, ha="right", fontsize=18)
    ax.set_yticks([i * 0.05 for i in range(22)])
    ax.tick_params(axis="y", labelsize=18)
    ax.grid(axis="y", alpha=0.3, linestyle="--")

    for bar, v in zip(bars, f1_scores):
        ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            bar.get_height(),
            f"{v:.3f}",
            ha="center",
            va="bottom",
            fontsize=18,
        )

    plt.tight_layout()
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存 F1 对比图：{out_path}")


def plot_prf_comparison(summary: dict, out_path: str):
    """
    图 2：PRF 指标对比图
    
    展示所有策略的 Precision/Recall/F1
    """
    strategies = list(summary.keys())
    n = len(strategies)
    
    precision = [summary[s]['precision'] for s in strategies]
    recall = [summary[s]['recall'] for s in strategies]
    f1 = [summary[s]['f1'] for s in strategies]
    
    x = np.arange(n)
    width = 0.18
    
    fig, ax = plt.subplots(figsize=(16.8, 8.6))
    
    bars1 = ax.bar(x - width, precision, width, label='Precision', alpha=0.9, color="#719AAC", edgecolor='black', linewidth=0.9)
    bars2 = ax.bar(x, recall, width, label='Recall', alpha=0.9, color="#E29135", edgecolor='black', linewidth=0.9)
    bars3 = ax.bar(x + width, f1, width, label='F1', alpha=0.9, color="#72B063", edgecolor='black', linewidth=0.9)
    
    ax.set_xlabel('Strategy', fontsize=16)
    ax.set_ylabel('Score', fontsize=16)
    ax.set_title('Precision/Recall/F1 Comparison', fontsize=18)
    ax.set_xticks(x)
    ax.set_xticklabels(strategies, rotation=15, ha='right', fontsize=14)
    ax.set_yticks([i * 0.05 for i in range(22)])
    ax.tick_params(axis="y", labelsize=14)
    ax.legend(fontsize=14)
    ax.grid(axis='y', alpha=0.3, linestyle='--')
    
    # 添加数值标签
    for bars in [bars1, bars2, bars3]:
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}', ha='center', va='bottom', fontsize=13)
    
    plt.tight_layout()
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存 PRF 对比图：{out_path}")


def plot_category_f1(summary: dict, out_path: str):
    """
    图 3：按类别 F1 对比图
    
    展示每个类别在不同策略下的 F1
    优化设计：使用分组柱状图，清晰对比各策略在不同类别上的表现
    """
    categories = ['security', 'bug', 'smell', 'performance']
    category_names = ['Security', 'Bug', 'Smell', 'Performance']
    
    strategies = list(summary.keys())
    n_strategies = len(strategies)
    
    # 创建 2x2 子图
    fig, axes = plt.subplots(2, 2, figsize=(19.5, 14.5))

    palette = ["#4A5F7E", "#719AAC", "#94C6CD", "#B8DBB3", "#72B063", "#E29135"]
    colors = [palette[i % len(palette)] for i in range(n_strategies)]
    
    for idx, cat in enumerate(categories):
        ax = axes[idx // 2, idx % 2]
        
        # 获取每个策略在该类别下的 F1
        f1_scores = []
        for strat in strategies:
            if cat in summary[strat]['by_type']:
                f1_scores.append(summary[strat]['by_type'][cat]['f1'])
            else:
                f1_scores.append(0.0)
        
        # 设置柱状图位置：每个策略一个独立的 x 位置
        x = np.arange(n_strategies)
        width = 0.4
        
        # 绘制柱状图
        bars = ax.bar(x, f1_scores, width, color=colors, alpha=0.9, edgecolor='black', linewidth=0.9)
        
        # 添加数值标签
        for bar, f1 in zip(bars, f1_scores):
            if f1 > 0:
                ax.text(bar.get_x() + bar.get_width()/2., f1 + 0.02,
                       f'{f1:.3f}', ha='center', va='bottom', fontsize=13, fontweight='bold')
        
        # 设置标题和标签
        ax.set_xlabel('Strategy', fontsize=14)
        ax.set_ylabel('F1 Score', fontsize=14)
        ax.set_title(f'{category_names[idx]} F1 Comparison', fontsize=16, fontweight='bold')
        ax.set_xticks(x)
        ax.set_xticklabels(strategies, rotation=20, ha='right', fontsize=13)
        ax.tick_params(axis="y", labelsize=13)
        ax.set_ylim(0, 1.1)
        ax.set_yticks([i * 0.1 for i in range(12)])
        ax.grid(axis='y', alpha=0.3, linestyle='--')
        
        # 添加最佳策略标注
        best_idx = np.argmax(f1_scores)
        best_f1 = f1_scores[best_idx]
        ax.annotate(f'Best: {strategies[best_idx]}\n({best_f1:.3f})',
                   xy=(best_idx, best_f1),
                   xytext=(best_idx, best_f1 + 0.15),
                   fontsize=13, color='#4A5F7E', fontweight='bold',
                   ha='center',
                   bbox=dict(boxstyle='round', facecolor='white', edgecolor='#4A5F7E', alpha=0.9),
                   arrowprops=dict(arrowstyle='->', color='#4A5F7E', lw=1.2))
    
    plt.suptitle('Category-wise F1 Comparison - Component Ablation Study', 
                fontsize=18, fontweight='bold', y=0.98)
    plt.tight_layout()
    plt.subplots_adjust(top=0.93)
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存类别 F1 对比图：{out_path}")


def plot_time_performance_tradeoff(summary: dict, out_path: str):
    """
    图 4：时间 - 性能权衡图
    
    展示每个策略的时间 vs F1，帮助选择最优策略
    """
    strategies = list(summary.keys())
    
    times = [summary[s]['avg_time_per_sample'] for s in strategies]
    f1_scores = [summary[s]['f1'] for s in strategies]
    
    fig, ax = plt.subplots(figsize=(12, 8))
    
    # 散点图
    scatter = ax.scatter(times, f1_scores, s=200, alpha=0.7, 
                        c=np.arange(len(strategies)), cmap='tab10',
                        edgecolors='black', linewidth=2)
    
    # 添加标签
    for i, strat in enumerate(strategies):
        ax.annotate(strat, 
                   (times[i], f1_scores[i]),
                   xytext=(5, 5), textcoords='offset points',
                   fontsize=10, fontweight='bold',
                   bbox=dict(boxstyle='round', facecolor='white', alpha=0.7))
    
    # 添加趋势线（数据退化时跳过）
    if len(times) >= 2 and (len(set(times)) >= 2 or len(set(f1_scores)) >= 2):
        try:
            z = np.polyfit(times, f1_scores, 1)
            p = np.poly1d(z)
            ax.plot(times, p(times), "r--", alpha=0.8, linewidth=2, label='Trend line')
        except Exception:
            pass
    
    # 标注最优策略（F1 最高）
    best_idx = np.argmax(f1_scores)
    ax.scatter(times[best_idx], f1_scores[best_idx], s=300, alpha=1.0,
              facecolors='none', edgecolors='red', linewidth=3,
              label=f'Best F1: {strategies[best_idx]}')
    
    # 标注最快策略
    fastest_idx = np.argmin(times)
    ax.scatter(times[fastest_idx], f1_scores[fastest_idx], s=300, alpha=1.0,
              facecolors='none', edgecolors='blue', linewidth=3,
              label=f'Fastest: {strategies[fastest_idx]}')
    
    ax.set_xlabel('Average Time per Sample (s)', fontsize=12)
    ax.set_ylabel('F1 Score', fontsize=12)
    ax.set_title('Time-Performance Tradeoff Analysis', fontsize=14)
    ax.legend(fontsize=10)
    ax.grid(alpha=0.3, linestyle='--')
    
    plt.tight_layout()
    plt.savefig(out_path, bbox_inches='tight')
    plt.close()
    print(f"已保存时间 - 性能权衡图：{out_path}")


def plot_radar_chart(summary: dict, out_path: str):
    """
    图 5：雷达图 - 多维度对比
    
    展示每个策略在多个维度的综合表现
    """
    strategies = list(summary.keys())
    n_strategies = len(strategies)
    
    # 定义维度
    dimensions = ['Precision', 'Recall', 'F1', 'Structured Rate']
    n_dims = len(dimensions)
    
    # 计算每个策略在各维度的得分
    angles = np.linspace(0, 2 * np.pi, n_dims, endpoint=False).tolist()
    angles += angles[:1]  # 闭合
    
    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw=dict(polar=True))
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    
    colors = plt.cm.tab10(np.linspace(0, 1, n_strategies))

    all_vals: list[float] = []
    series: list[tuple[str, np.ndarray, list[float]]] = []
    for i, strat in enumerate(strategies):
        values = [
            summary[strat]['precision'],
            summary[strat]['recall'],
            summary[strat]['f1'],
            summary[strat]['structured_output_rate']
        ]
        values = [max(0.0, min(1.0, float(v))) for v in values]
        all_vals.extend(values)
        values += values[:1]  # 闭合

        c = colors[i]
        series.append((strat, c, values))

    rmin, rmax, rticks = _compute_radar_scale(all_vals)
    for strat, c, values in series:
        ax.plot(
            angles,
            values,
            linewidth=2.0,
            label=strat,
            color=c,
            marker="s",
            markersize=5,
            markerfacecolor=c,
            markeredgecolor=c,
        )
    
    # 设置标签
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(dimensions, fontsize=12)
    ax.tick_params(axis="x", pad=14)
    ax.set_ylim(rmin, rmax)
    ax.set_yticks(rticks)
    ax.set_yticklabels([f"{t:.2f}".rstrip("0").rstrip(".") for t in rticks], fontsize=10, color="0.35")
    ax.set_rlabel_position(0)
    ax.yaxis.grid(False)
    ax.xaxis.grid(True, color="0.7", linewidth=1.0)
    ax.spines["polar"].set_color("0.7")
    ax.set_title('Multi-dimensional Comparison (Radar Chart)', fontsize=14, pad=20)
    ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=10)
    
    plt.tight_layout()
    plt.savefig(out_path, bbox_inches='tight')
    plt.close()
    print(f"已保存雷达图：{out_path}")


def plot_all(report_path: str, out_dir: str):
    """生成所有图表"""
    use_style(figsize=(9, 6))
    report = load_report(report_path)
    summary = report['summary']
    bootstrap = report.get("bootstrap")
    
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print("="*60)
    print("实验 2A：生成可视化图表")
    print("="*60)
    
    # 生成 5 个图表
    plot_component_contribution(summary, out_dir / '01_component_contribution.png', bootstrap=bootstrap)
    plot_prf_comparison(summary, out_dir / '02_prf_comparison.png')
    plot_category_f1(summary, out_dir / '03_category_f1.png')
    plot_time_performance_tradeoff(summary, out_dir / '04_time_performance_tradeoff.png')
    plot_radar_chart(summary, out_dir / '05_radar_chart.png')
    
    print("="*60)
    print(f"所有图表已保存至：{out_dir}")
    print("="*60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="实验 2A 可视化工具")
    parser.add_argument("--report", default="reports/exp2a_ablation/report.json", help="实验报告路径")
    parser.add_argument("--out-dir", default="reports/exp2a_ablation/plots", help="输出目录")
    args = parser.parse_args()
    
    plot_all(args.report, args.out_dir)


if __name__ == "__main__":
    main()
