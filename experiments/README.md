# 实验目录说明

三组对比实验 + 一套 Agent 评测，输入统一来自 `dataset/`，产物统一落到 `reports/`。

## 脚本一览

| 脚本 | 作用 | 主要输入 | 产物 |
|---|---|---|---|
| `run_all.py` | 一键跑完 exp1/2a/2b/3 并出图 | `dataset/eval.json` | `reports/paper/**` |
| `exp1_recognition.py` | 实验 1：问题识别能力（Pylint / 原生 LLM / 本系统 三方对比） | 数据集 | `report.json` |
| `exp1_plot.py` | 实验 1 出图（整体 PRF 柱状图 + 分类雷达图） | `report.json` | `overall_prf.png`、`prf_radar.png` |
| `exp1_comparison_plot.py` | 实验 1 跨模型对比（读多个模型各自的报告） | `reports/exp1-*/report.json` | `reports/exp1_comparison_f1.png` |
| `exp2a_component_ablation.py` | 实验 2A：Prompt 组件消融（角色/推理/规则库/示例/置信度） | 数据集 | `report.json` |
| `exp2a_plot.py` | 实验 2A 出图（贡献度、PRF、分类 F1、时间权衡、雷达） | `report.json` | 5 张图 |
| `exp2b_interaction_mode.py` | 实验 2B：单轮 vs 多轮交互模式对比 | 数据集 | `report.json` |
| `exp2b_plot.py` | 实验 2B 出图（PRF、时间、性价比、分类、召回-精确权衡） | `report.json` | 5 张图 |
| `exp3_optimize.py` | 实验 3：代码修复能力（修复率 / AST 匹配 / 精确修复 + 自助抽样） | 数据集 | `report.json` |
| `exp3_plot.py` | 实验 3 出图（汇总、雷达、时间-质量、问题分布） | `report.json` | 4 张图 |
| `exp3_comparison_plot.py` | 实验 3 跨模型对比 | `reports/exp3*/report.json` | `reports/exp3_comparison_core.png` |
| `agent_eval.py` / `agent_metrics.py` | Agent 专项评测（走 `agent/` 那套 Agent） | 数据集 | 评测 JSON |
| `dataset_loader.py` | 数据集加载（带截断修复、语言过滤、抽样） | `dataset/*.json` | `Sample` 列表 |
| `plot_style.py` | 统一绘图样式与公共组件（字体/配色/坐标轴/数值标注/雷达刻度） | — | — |

## 快速开始

```bash
# ① 零成本跑通全流程（本地规则引擎，不调用模型）
python experiments/run_all.py --mode local

# ② 接真实模型（需要 .env 里的 API_KEY / MODEL_NAME）
python experiments/run_all.py --mode remote --max-samples 50

# ③ 只跑某几个实验
python experiments/run_all.py --only exp2a exp2b --max-samples 30

# ④ 单独重跑某个实验的图（不用重新跑实验）
python experiments/exp2a_plot.py --report reports/paper/exp2/exp2a_ablation/report.json \
                                 --out-dir reports/paper/exp2/exp2a_ablation/plots
```

产物目录：

```
reports/paper/
├── exp1/                     report.json + overall_prf.png + prf_radar.png
├── exp2/
│   ├── exp2a_ablation/       report.json + plots/01..05.png
│   └── exp2b_interaction/    report.json + plots/01..05.png
└── exp3/                     report.json + 4 张图
```

## 数据集

| 目录 | 内容 | 说明 |
|---|---|---|
| `dataset/` | 500 条纯 Python 样本，含 `dataset.json`（全量）、`train.json`、`eval.json`、`test.json`、`test_remaining.json` | **正式实验输入**，默认用 `dataset/eval.json` |
| `dataset/legacy/` | 早期迭代产出的混合语言样本 | 历史遗留，默认不参与实验；需要时 `--dataset dataset/legacy/test.json` |

格式：`[{id, language, code, fixed_code, issues[{category, subtype, severity, location{line_start,line_end}}]}]`。
数据集文件曾出现「手工拼接导致尾部截断」的问题，`dataset_loader.py` 会先尝试标准解析，
失败且只要 Python 样本时截到第一个非 Python 样本之前再解析，并在 `stats.parse.strategy` 里记录实际用了哪种策略。

## 出图约定

所有绘图脚本共用 `plot_style.py`：

- `use_style()`：统一字体（中文优先）、字号、网格、导出 300 DPI；
  需要更大字号时传 `font_scale=1.1`
- `save_figure(fig, path)`：自动建目录 + 统一 DPI + 关闭画布
- `annotate_bars(ax, bars, "{:.3f}")`：数值标签，偏移量按坐标轴范围计算，
  不会出现「大字压柱子、小字贴天花板」
- `radar_scale(values)`：雷达图径向刻度，保证同一指标在不同实验里刻度一致
- `COLORS` / `SERIES_COLORS`：论文配色（原生模型=蓝、本系统=绿、强调=橙）

新增图表时请直接复用这些组件，不要再各写一份 `plt.rcParams`。

## 跨模型对比

实验 1 / 3 的对比图要读取**多个模型各自跑出来的报告**，因此是独立步骤：

```bash
# 先让每个模型各跑一遍（切 .env 里的 MODEL_NAME，或直接改 --out-dir 分组）
python experiments/exp1_recognition.py --dataset dataset/eval.json --out-dir reports/exp1-doubao
python experiments/exp1_recognition.py --dataset dataset/eval.json --out-dir reports/exp1-deepseek

# 再画对比图（找不到的模型目录会自动跳过并提示）
python experiments/exp1_comparison_plot.py
python experiments/exp3_comparison_plot.py --metric fix_rate
```

也可以显式指定：`--model "Doubao=reports/exp1-doubao" --model "GLM=reports/exp1-GLM"`。

运行环境与整体流程见 [../start.md](../start.md)。
