# 50 条样本的新一轮评测（2026-10-26）

> 数据源：`data/dataset/eval.json`（50 条纯 Python，**50/50 带行号标注**）
> 模型：`deepseek-flash`（`.env` → `https://api.deepseek.com/chat/completions`，`MOCK_MODE=false`）
> 匹配口径：类别相同 **且** 行号落在标注区间 ±1 内（行号级，严格）

---

## 0. 先说三个结论

1. **信号是稳的**：三方对比在新样本集上复现了旧结论——Pylint 0.152 → 原生 LLM 0.544 →
   本系统 0.789（F1，行号级）。
2. **但 exp1 表里的「本系统」不是 `agent/`**。它调的是 `core/reviewer.py`
   （规则引擎 + **单次** LLM 调用 + 去重合并，没有工具循环）。
   真正那个 ReAct Agent 在同样 50 条上的 F1 只有 **0.586**，和零成本规则基线（0.577）基本持平。
3. **Agent 的精确率问题不是"定位不准"，是"报得比标注多"**：61 个 FP 里 45 个是
   标注里根本没出现的类别，只有 16 个是类别对、行号错。

---

## 1. 表 1 · 问题识别三方对比（exp1，n=50）

### 1.1 行号级（严格，本次主口径）

| 系统 | P | R | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| Pylint + Bandit | 0.094 | 0.400 | 0.152 | 20 | 193 | 30 |
| 原生 LLM（单次调用） | 0.387 | 0.920 | 0.544 | 46 | 73 | 4 |
| 本系统（`core/reviewer.py`） | **0.703** | 0.900 | **0.789** | 45 | 19 | 5 |

### 1.2 类别级（宽松，与旧报告同口径）

| 系统 | P | R | F1 |
|---|---|---|---|
| Pylint + Bandit | 0.094 | 0.400 | 0.152 |
| 原生 LLM | 0.395 | 0.940 | 0.556 |
| 本系统 | 0.703 | 0.900 | 0.789 |

> 这批数据上两套口径几乎重合（system 完全相同，llm_base 差 0.012）——说明模型给的行号
> 基本贴住了标注，也说明旧报告（类别级）与新报告（行号级）在这里可以放心对比。

### 1.3 分类别 F1（行号级）

| 类别 | 样本数 | Pylint | 原生 LLM | 本系统 |
|---|---|---|---|---|
| security | 12 | 0.400 | 0.960 | **0.960** |
| performance | 13 | 0.000 | 0.741 | **0.786** |
| bug | 12 | 0.122 | 0.400 | **0.714** |
| smell | 13 | 0.144 | 0.421 | **0.727** |

本系统在 `performance` / `bug` / `smell` 三类明显领先，`security` 与原生 LLM 打平（0.960）；
其中 `bug` 类提升最大（0.400 → 0.714）。两种系统的 security 都接近满分，说明该类的
模式最容易被提示词覆盖，难点在 `bug` 与 `smell` 的定位。

---

## 2. 表 2 · Agent 执行过程指标（首次落盘，n=50）

以前 `reports/` 里一直没有这份产物，这次补上了。

| 指标 | 本系统 Agent（single） | 规则基线（rules） |
|---|---|---|
| 识别 P / R / F1（行号级） | 0.430 / 0.920 / 0.586 | 0.556 / 0.600 / 0.577 |
| 任务成功率（≥1 命中且 P≥0.5） | 0.680 | 0.480 |
| 工具选择准确率（先调静态分析） | **1.000** | — |
| 结构化输出成功率 | **1.000** | — |
| 平均步数（±std） | 6.28（±1.67） | — |
| 平均工具调用 | 3.46 | — |
| 平均 token / 样本 | 10,275 | 0 |
| 平均耗时 / P95 | 16.1s / 28.5s | 2.0s / — |
| 证据覆盖率 | 0.960 | 0.527 |
| 证据校验通过率 | 0.947 | 0.000 |
| 因缺证据被丢弃 | 0 条 | 0 条 |
| 停止原因 | `final` × 50（100%） | — |
| 工具使用分布 | find_symbol 56 / run_static_analysis 51 / get_ast_outline 50 / check_syntax 16 | — |

合计消耗 513,730 token、50 次运行 0 异常、墙钟 209.6s（4 线程）。
按 `.env` 里单价为 0，所以成本估算显示为 0（要真实成本需填 `AGENT_PRICE_*_PER_1K`）。

**分类别 F1（Agent）**：security 1.000 / performance 0.846 / bug 0.800 / smell 0.703
—— 分类别看 Agent 其实不差，是整体 F1 被"跨类别多报"拖下来的（见下节）。

---

## 3. 表 3 · 误报归因（关键诊断）

数据集每个样本只标 1~2 个问题，所以"多报出的真实问题"也会被记成 FP。把它拆开：

| 系统 | TP | FP 合计 | 类别对·行号错 | 类别不在标注 |
|---|---|---|---|---|
| Pylint + Bandit | 20 | 193 | 44 | 149 |
| 原生 LLM | 46 | 73 | 16 | 57 |
| 本系统（`core/reviewer.py`） | 45 | **19** | 10 | **9** |
| Agent（single） | 46 | 61 | 16 | **45** |
| 规则基线（rules） | 30 | 24 | 5 | 19 |

读法：

- `core/reviewer.py` 的高精确率来自**类别收敛**——它几乎不报标注之外的类别（9 个）。
- Agent 的召回更高（0.920）但把 45 个 FP 花在了"标注里没有的类别"上；
  定位能力本身（16 个行号错）和原生 LLM 完全一致。
- 换句话说：**Agent 现在的问题不是看不懂代码，是没有"该收手"的约束**。

---

## 4. 表 4 · 新旧数据对照（同一口径：类别级）

| 指标 | 旧（450 条，DeepSeek） | 新（50 条，deepseek-flash） |
|---|---|---|
| Pylint F1 | 0.141 | 0.152 |
| 原生 LLM F1 | 0.561 | 0.556 |
| 本系统 F1 | 0.736 | 0.789 |

三条都在 ±0.05 内，说明旧数据本身不是错的，只是样本集与模型版本不同；
新数据可以直接替换旧表，趋势不变。

---

## 5. 需要你决策的一点

exp1 表里被当作"本系统"的 `core/reviewer.py`（规则引擎 + 单次 LLM + 去重合并）
与 README 主打的 `agent/`（ReAct Agent，带工具循环与证据闭环）
**是两条不同的代码路径**，效果差 0.20 F1。三条路可选：

1. **改叙事**：论文/简历里 exp1 这张表就写"提示词强化的单次审查"，Agent 的效果单独用表 2 报告。
2. **修 Agent**：给 Agent 加"类别收敛"约束（提示词或 finalizer 里限制只报高置信类别），
   目标是把 45 个跨类别 FP 压下去——这是最可能把它拉到 0.7+ 的改动。
3. **改标注**：把评测集做成多标签（允许"额外发现"不计 FP），但工作量大且不可回改历史结果。

我的建议是 **2 先试一轮**，同时用 1 兜底：改动小、可验证（重跑本脚本即可对比）。

---

## 6. 产物与复现

| 产物 | 路径 |
|---|---|
| exp1 三方报告（含每样本 details） | `reports/exp1-2026/report.json` |
| 双口径重算 | `reports/exp1-2026/dual_level.json` |
| Agent 层指标 | `reports/agent/single/report.json` |
| 规则基线 | `reports/agent/rules/report.json` |
| 运行轨迹 | `traces/*.jsonl` |

```powershell
$env:PYTHONIOENCODING="utf-8"

# 1) 三方识别（50 条，行号级）
.venv\Scripts\python.exe experiments/exp1_recognition.py `
  --dataset data/dataset/eval.json --max-samples 50 --require-line `
  --out-dir reports/exp1-2026 --systems pylint,llm_base,system --workers 8

# 2) 同一批预测离线重算两套口径（零成本）
.venv\Scripts\python.exe experiments/report_dual_level.py reports/exp1-2026/report.json

# 3) Agent 层指标 + 零成本基线
.venv\Scripts\python.exe experiments/agent_eval.py --mode single `
  --dataset data/dataset/eval.json --max-samples 50 --max-workers 4 `
  --persist-traces --out reports/agent/single/report.json
.venv\Scripts\python.exe experiments/agent_eval.py --mode rules `
  --dataset data/dataset/eval.json --max-samples 50 --out reports/agent/rules/report.json

# 4) 误报归因
.venv\Scripts\python.exe experiments/fp_attribution.py reports/exp1-2026/report.json
```

新增两个可复用脚本：
[report_dual_level.py](../experiments/report_dual_level.py)（双口径重算，不用重跑模型）、
[fp_attribution.py](../experiments/fp_attribution.py)（FP 归因）。

**这次没跑**：exp2a 组件消融、exp2b 交互模式、exp3 修复能力。要补齐的话 exp3 最贵
（生成代码 + 沙箱跑测试，50 条约 30-40 分钟），exp2a 是 6 组 × 50 条 ≈ 40 分钟。
