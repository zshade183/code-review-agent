"""
实验 2B：交互模式对比实验可视化（全组件）

生成图表：
1. 单轮 vs 多轮 PRF 对比
2. 时间对比图
3. 性价比分析图
4. 按类别性能对比
5. 召回率 - 精确度权衡图
"""

import json
from pathlib import Path

try:  # 直接运行脚本时
    from plot_style import use_style
except ImportError:  # 作为包导入时
    from experiments.plot_style import use_style  # type: ignore

import matplotlib.pyplot as plt
import numpy as np


def load_report(report_path: str) -> dict:
    """加载实验报告"""
    with open(report_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def plot_prf_comparison(summary: dict, out_path: str, bootstrap: dict | None = None):
    """
    图 1：PRF 指标对比图
    
    对比单轮和多轮的 Precision/Recall/F1
    """
    modes = list(summary.keys())
    n = len(modes)
    
    precision = [summary[m]['precision'] for m in modes]
    recall = [summary[m]['recall'] for m in modes]
    f1 = [summary[m]['f1'] for m in modes]
    
    x = np.arange(n)*0.75
    width = 0.16
    
    fig, ax = plt.subplots(figsize=(10, 8.6))
    
    bars1 = ax.bar(x - width, precision, width, label='Precision', alpha=0.9, color="#719AAC", edgecolor='black', linewidth=0.9)
    bars2 = ax.bar(x, recall, width, label='Recall', alpha=0.9, color="#E29135", edgecolor='black', linewidth=0.9)
    bars3 = ax.bar(x + width, f1, width, label='F1', alpha=0.9, color="#72B063", edgecolor='black', linewidth=0.9)
    
    ax.set_xlabel('Interaction Mode', fontsize=16)
    ax.set_ylabel('Score', fontsize=16)
    ax.set_title('Single-turn vs Multi-turn: PRF Comparison', fontsize=18)
    ax.set_xticks(x)
    ax.set_xticklabels(modes, rotation=15, ha='right', fontsize=18)
    ax.set_yticks([i * 0.05 for i in range(22)])
    ax.tick_params(axis="y", labelsize=18)
    ax.legend(fontsize=18)
    ax.grid(axis='y', alpha=0.3, linestyle='--')
    
    # 添加数值标签
    for bars in [bars1, bars2, bars3]:
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}', ha='center', va='bottom', fontsize=18, fontweight='bold')
    
    delta_ci_low = None
    delta_ci_high = None
    if isinstance(bootstrap, dict):
        d = bootstrap.get("delta_f1")
        if isinstance(d, dict):
            delta_ci_low = d.get("delta_f1_ci_low")
            delta_ci_high = d.get("delta_f1_ci_high")

    if 'single_turn' in summary and 'multi_turn' in summary:
        f1_improve = summary['multi_turn']['f1'] - summary['single_turn']['f1']
        base_f1 = float(summary['single_turn']['f1'])
        f1_improve_pct = (f1_improve / base_f1) * 100 if base_f1 > 0 else 0.0

        ci_text = ""
        if isinstance(delta_ci_low, (int, float)) and isinstance(delta_ci_high, (int, float)):
            ci_text = f"\n95% CI: [{float(delta_ci_low):.3f}, {float(delta_ci_high):.3f}]"

        ax.annotate(
            f'F1 提升：+{f1_improve:.3f} (+{f1_improve_pct:.1f}%){ci_text}',
            xy=(1.5, max(f1)),
            xytext=(1.5, max(f1) + 0.05),
            fontsize=13,
            fontweight='bold',
            color='red',
            bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.7),
            ha='center'
        )
    
    plt.tight_layout()
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存 PRF 对比图：{out_path}")


def plot_time_comparison(summary: dict, out_path: str):
    """
    图 2：时间对比图
    
    对比单轮和多轮的时间消耗
    """
    modes = list(summary.keys())
    
    times = [summary[m]['avg_time_per_sample'] for m in modes]
    total_times = [summary[m]['total_time_s'] for m in modes]
    samples = [summary[m]['samples'] for m in modes]
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 6.8))
    
    # 图 1：平均时间对比
    x = np.arange(len(modes))
    width = 0.45
    
    colors = ["#719AAC", "#72B063"][:len(modes)]
    bars = ax1.bar(x, times, width, color=colors, alpha=0.9, edgecolor='black', linewidth=0.9)
    
    ax1.set_xlabel('Interaction Mode', fontsize=14)
    ax1.set_ylabel('Average Time per Sample (s)', fontsize=14)
    ax1.set_title('Time Consumption Comparison', fontsize=16)
    ax1.set_xticks(x)
    ax1.set_xticklabels(modes, rotation=15, ha='right', fontsize=13)
    ax1.tick_params(axis="y", labelsize=13)
    ax1.grid(axis='y', alpha=0.3, linestyle='--')
    
    # 添加数值标签
    for bar in bars:
        height = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.3f}s', ha='center', va='bottom', fontsize=13, fontweight='bold')
    
    if 'single_turn' in summary and 'multi_turn' in summary:
        time_increase = times[modes.index('multi_turn')] - times[modes.index('single_turn')]
        base_time = float(times[modes.index('single_turn')])
        time_increase_pct = (time_increase / base_time) * 100 if base_time > 0 else 0.0
        
        ax1.annotate(
            f'时间增加：+{time_increase:.2f}s (+{time_increase_pct:.1f}%)',
            xy=(1, times[modes.index('multi_turn')]),
            xytext=(1, times[modes.index('multi_turn')] + 2),
            fontsize=12,
            color='#4A5F7E',
            fontweight='bold',
            bbox=dict(boxstyle='round', facecolor='white', edgecolor='#4A5F7E', alpha=0.9),
            ha='center'
        )

    # 图 2：总时间对比
    bars2 = ax2.bar(x, total_times, width, color=colors, alpha=0.9, edgecolor='black', linewidth=0.9)
    
    ax2.set_xlabel('Interaction Mode', fontsize=14)
    ax2.set_ylabel('Total Time (s)', fontsize=14)
    ax2.set_title('Total Time Consumption', fontsize=16)
    ax2.set_xticks(x)
    ax2.set_xticklabels(modes, rotation=15, ha='right', fontsize=13)
    ax2.tick_params(axis="y", labelsize=13)
    ax2.grid(axis='y', alpha=0.3, linestyle='--')
    
    # 添加数值标签
    for bar in bars2:
        height = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.2f}s', ha='center', va='bottom', fontsize=13, fontweight='bold')
    
    # 添加样本数标注
    for i, (mode, sample) in enumerate(zip(modes, samples)):
        ax2.text(i, total_times[i] + 5, f'({sample} samples)',
                ha='center', va='bottom', fontsize=9, color='gray')
    
    plt.tight_layout()
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存时间对比图：{out_path}")


def plot_cost_benefit_analysis(summary: dict, out_path: str):
    """
    图 3：性价比分析图
    
    分析多轮对话的性价比（F1 提升 vs 时间增加）
    """
    if 'single_turn' not in summary or 'multi_turn' not in summary:
        print("警告：缺少单轮或多轮数据，跳过性价比分析图")
        return
    
    single_f1 = summary['single_turn']['f1']
    multi_f1 = summary['multi_turn']['f1']
    single_time = summary['single_turn']['avg_time_per_sample']
    multi_time = summary['multi_turn']['avg_time_per_sample']
    
    f1_improve = multi_f1 - single_f1
    f1_improve_pct = (f1_improve / single_f1) * 100 if single_f1 > 0 else 0
    time_increase = multi_time - single_time
    time_increase_pct = (time_increase / single_time) * 100 if single_time > 0 else 0
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 6.8))
    
    metrics = ['F1\n提升', '时间\n增加']
    values = [f1_improve_pct, time_increase_pct]
    colors = ["#72B063", "#E29135"]
    
    x = np.arange(len(metrics))
    bars = ax1.bar(x, values, width=0.45, color=colors, alpha=0.9, edgecolor='black', linewidth=0.9)
    
    ax1.set_ylabel('Percentage (%)', fontsize=14)
    ax1.set_title('Improvement vs Cost', fontsize=16)
    ax1.set_xticks(x)
    ax1.set_xticklabels(metrics, fontsize=13)
    ax1.tick_params(axis="y", labelsize=13)
    ax1.grid(axis='y', alpha=0.3, linestyle='--')
    
    for bar in bars:
        height = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.2f}%', ha='center', va='bottom', fontsize=14, fontweight='bold')
    
    ax1.axhline(y=0, color='black', linewidth=1)
    
    categories = ['单轮对话', '多轮对话']
    f1_scores = [single_f1, multi_f1]
    times = [single_time, multi_time]
    
    x = np.arange(len(categories))
    width = 0.28
    
    ax2_twin = ax2.twinx()
    
    bars1 = ax2.bar(x, f1_scores, width, label='F1 Score', alpha=0.9, color="#72B063", edgecolor='black', linewidth=0.9)
    bars2 = ax2_twin.bar(x, times, width, label='Time (s)', alpha=0.9, color="#719AAC", edgecolor='black', linewidth=0.9)
    
    ax2.set_xlabel('Interaction Mode', fontsize=14)
    ax2.set_ylabel('F1 Score', fontsize=14, color='black')
    ax2_twin.set_ylabel('Time (s)', fontsize=14, color='black')
    ax2.set_title('Cost-Benefit Analysis', fontsize=16)
    ax2.set_xticks(x)
    ax2.set_xticklabels(categories, fontsize=13)
    
    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2_twin.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=12)
    
    for bar in bars1:
        height = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.3f}', ha='center', va='bottom', fontsize=13, fontweight='bold', color='#4A5F7E')
    
    for bar in bars2:
        height = bar.get_height()
        ax2_twin.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.3f}s', ha='center', va='bottom', fontsize=13, fontweight='bold', color='#4A5F7E')
    
    conclusion = '值得' if f1_improve_pct > time_increase_pct else '不值得'
    ax2.annotate(f'性价比：{conclusion}',
                xy=(1.5, max(f1_scores)/2),
                xytext=(1.5, max(f1_scores)/2),
                fontsize=16, fontweight='bold', color='#4A5F7E',
                bbox=dict(boxstyle='round', facecolor='white', edgecolor='#4A5F7E', alpha=0.9),
                ha='center')
    
    plt.tight_layout()
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存性价比分析图：{out_path}")


def plot_category_comparison(summary: dict, out_path: str):
    """
    图 4：按类别性能对比图
    
    对比单轮和多轮在不同类别上的 F1
    """
    categories = ['security', 'bug', 'smell', 'performance']
    category_names = ['Security', 'Bug', 'Smell', 'Performance']
    
    modes = list(summary.keys())
    n_modes = len(modes)
    
    fig, axes = plt.subplots(2, 2, figsize=(15.8, 13.5))
    
    for idx, cat in enumerate(categories):
        ax = axes[idx // 2, idx % 2]
        
        x = np.arange(n_modes)
        width = 0.45
        
        f1_scores = []
        for mode in modes:
            if cat in summary[mode]['by_type']:
                f1_scores.append(summary[mode]['by_type'][cat]['f1'])
            else:
                f1_scores.append(0.0)
        
        colors = ["#719AAC", "#72B063"][:n_modes]
        bars = ax.bar(x, f1_scores, width, color=colors, alpha=0.9, edgecolor='black', linewidth=0.9)
        for bar, f1 in zip(bars, f1_scores):
            if float(f1) > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2.0,
                    bar.get_height(),
                    f'{float(f1):.3f}',
                    ha='center',
                    va='bottom',
                    fontsize=13,
                    fontweight='bold',
                )
        
        ax.set_xlabel('Interaction Mode', fontsize=14)
        ax.set_ylabel('F1 Score', fontsize=14)
        ax.set_title(f'{category_names[idx]} F1 Comparison', fontsize=16)
        ax.set_xticks(x)
        ax.set_xticklabels(modes, rotation=15, ha='right', fontsize=13)
        ax.set_yticks([i * 0.05 for i in range(22)])
        ax.tick_params(axis="y", labelsize=13)
        ax.grid(axis='y', alpha=0.3, linestyle='--')
    
    plt.tight_layout()
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"已保存类别对比图：{out_path}")


def plot_recall_precision_tradeoff(summary: dict, out_path: str):
    """
    图 5：召回率 - 精确度权衡图
    
    展示单轮和多轮在 Recall-Precision 空间的分布
    """
    modes = list(summary.keys())
    
    recalls = [summary[m]['recall'] for m in modes]
    precisions = [summary[m]['precision'] for m in modes]
    f1_scores = [summary[m]['f1'] for m in modes]
    
    fig, ax = plt.subplots(figsize=(10, 10))
    
    # 散点图
    scatter = ax.scatter(recalls, precisions, s=300, alpha=0.7,
                        c=np.arange(len(modes)), cmap='tab10',
                        edgecolors='black', linewidth=2)
    
    # 添加标签
    for i, mode in enumerate(modes):
        ax.annotate(mode,
                   (recalls[i], precisions[i]),
                   xytext=(5, 5), textcoords='offset points',
                   fontsize=12, fontweight='bold',
                   bbox=dict(boxstyle='round', facecolor='white', alpha=0.7))
    
    # 添加 F1 等高线
    recall_grid = np.linspace(0, 1, 100)
    precision_grid = np.linspace(0, 1, 100)
    R, P = np.meshgrid(recall_grid, precision_grid)
    F1_grid = 2 * (P * R) / (P + R + 1e-8)
    
    contour = ax.contour(R, P, F1_grid, levels=[0.5, 0.6, 0.7, 0.8],
                        colors='gray', linestyles='--', linewidths=1.5, alpha=0.5)
    
    # 标注 F1 值
    ax.clabel(contour, inline=True, fontsize=9, fmt='F1=%.1f')

    if len(modes) >= 2 and 'single_turn' in summary and 'multi_turn' in summary:
        i0 = modes.index('single_turn')
        i1 = modes.index('multi_turn')
        ax.annotate('',
                   xy=(recalls[i1], precisions[i1]),
                   xytext=(recalls[i0], precisions[i0]),
                   arrowprops=dict(arrowstyle='->', color='red', lw=2.5),
                   fontsize=12)
        
        delta_recall = recalls[i1] - recalls[i0]
        delta_precision = precisions[i1] - precisions[i0]
        ax.text((recalls[i0] + recalls[i1])/2, (precisions[i0] + precisions[i1])/2,
               f'ΔR={delta_recall:+.3f}\nΔP={delta_precision:+.3f}',
               fontsize=9, color='red', fontweight='bold',
               bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    
    ax.set_xlabel('Recall', fontsize=12)
    ax.set_ylabel('Precision', fontsize=12)
    ax.set_title('Recall-Precision Tradeoff Analysis', fontsize=14)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.3, linestyle='--')
    
    plt.tight_layout()
    plt.savefig(out_path, bbox_inches='tight')
    plt.close()
    print(f"已保存召回率 - 精确度权衡图：{out_path}")


def plot_all(report_path: str, out_dir: str):
    """生成所有图表"""
    use_style(figsize=(9, 6))
    report = load_report(report_path)
    summary = report['summary']
    bootstrap = report.get("bootstrap")
    
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print("="*60)
    print("实验 2B：生成可视化图表")
    print("="*60)
    
    # 生成 5 个图表
    plot_prf_comparison(summary, out_dir / '01_prf_comparison.png', bootstrap=bootstrap)
    plot_time_comparison(summary, out_dir / '02_time_comparison.png')
    plot_cost_benefit_analysis(summary, out_dir / '03_cost_benefit_analysis.png')
    plot_category_comparison(summary, out_dir / '04_category_comparison.png')
    plot_recall_precision_tradeoff(summary, out_dir / '05_recall_precision_tradeoff.png')
    
    print("="*60)
    print(f"所有图表已保存至：{out_dir}")
    print("="*60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="实验 2B 可视化工具")
    parser.add_argument("--report", default="reports/exp2b_interaction/report.json", help="实验报告路径")
    parser.add_argument("--out-dir", default="reports/exp2b_interaction/plots", help="输出目录")
    args = parser.parse_args()
    
    plot_all(args.report, args.out_dir)


if __name__ == "__main__":
    main()
