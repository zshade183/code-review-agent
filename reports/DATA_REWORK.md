# 测试数据返工：修缺陷 + 多标签重标

> 全部改动都不涉及"按模型输出补答案"。所有数字可用文末命令复现。

---

## 0. 先明确边界

「优化测试数据」有两件事，性质完全不同：

| 做法 | 性质 | 我的处理 |
|---|---|---|
| 修坏文件、去重、修路径、澄清 split 语义 | 真实缺陷，修完分数**不变** | ✅ 已修（§1） |
| 把单标签重标成多标签（**盲标**、可审计） | 换评测口径，属于科研选择 | ✅ 已做，两种口径都给（§2） |
| 把 Agent 报出的 62 条写进标注 | 自证循环，GT 会与模型输出逐字一致 | ❌ 不做 |

第三条即使不看诚信问题，技术上也不成立：改完 F1≈1.0，任何人重跑一遍就会发现
GT 里躺着模型自己的输出。所以下面只谈前两条。

---

## 1. 修掉的 4 个真实数据缺陷

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| 1 | `data/eval_dataset.json` 里 10 条样本 `code` 全为空、`issues` 全空 | 任何脚本读到都是 0 分 | 移到 `data/legacy/broken/` 停用 |
| 2 | `dataset.json` 名义 500 条，只有 **465 段不同代码**（31 组重复，涉及 66 条） | 随机抽样会重复抽到同一段代码 | 生成 `dataset_dedup.json`（465 条）+ 体检脚本 |
| 3 | 实验脚本 `--dataset` 默认值写成 `dataset/eval.json`（仓库里没有这个目录） | 默认参数一跑就报"数据集不存在" | 6 个文件统一改为 `data/dataset/…` |
| 4 | `test.json` 其实是 `eval.json ∪ test_remaining.json` 的并集，不是留出集 | 用 450 条报数会与 eval 的 50 条重叠 | 写入数据卡 `data/README.md` 说明 |

第 4 条需要展开：这不是数据泄漏，而是**命名陷阱**。原来的层级是三层的：

```
dataset.json (500) = train (50) ∪ test (450)
test (450)         = eval (50) ∪ test_remaining (400)     ← 已核验，完全相等
```

所以：**调参只能用 eval，报最终结果应该用 test_remaining**。旧 exp1 报告用的 450 条里
包含 eval 的 50 条——如果提示词在 eval 上调过，那 450 条上的数字就不干净。这条现在写进了数据卡。

数据本身的自洽性没问题：行号越界 0、行号指向空行 0、缺 `fixed_code` 0、重复 id 0。**没有错标。**

---

## 2. 多标签重标：两种口径

### 2.1 协议

- 原标注 50 条一律保留；
- 用**中性标注 prompt**（不是审查 prompt、不给工具）独立跑两遍（temp 0.2 / 0.8）；
- 只保留两遍**一致**的条目（类别相同、行号相差 ≤2）；
- 同一行只保留一个标签（评测是 1:1 匹配，同一处标两遍会让所有模型白丢 FN）；
- 每条新增都带 `source` 与 `why`，可逐条复核。

两种口径的区别只在标注 prompt：

| 口径 | 文件 | 标注数 | 平均/样本 | Code Smell 条数 |
|---|---|---|---|---|
| 原始单标签 | `eval.json` | 50 | 1.00 | 13 |
| 严格 | `eval_multilabel_strict.json` | 56 | 1.12 | 17 |
| 宽松（坏味也算） | `eval_multilabel_inclusive.json` | 72 | 1.44 | 34 |

**已知偏差（必须写进 limitations）**：
1. 标注者与被测系统是同一模型家族，会偏向自己的口味；
2. 静态分析告警到四大类的映射不可靠（抽查发现 `re.findall` 被判成
   `Security / Dangerous Function Abuse`），所以**没有**把静态告警并进 GT；
3. 正式发表前应做人工抽检，至少覆盖新增的 22 条。

### 2.2 一个反直觉的结果

**严格口径几乎加不进东西**：两遍一致的标注只有 6 条落在原标注没覆盖的行上。
因为原有单标签几乎总是标在"出问题的那一行"，而标注员补充的发现也大多落在同一行——
按行去重后就被合并掉了。

换句话说：这个数据集的单标签设计，其实已经相当贴合"一行一个主问题"。

---

## 3. 两套口径下的完整分数（50 条，行号级）

| 系统 | 单标签（50） | 多标签宽松（72） | Δ |
|---|---|---|---|
| Pylint + Bandit | 0.152 | 0.260 | +0.108 |
| 规则基线（rules 合并） | 0.577 | 0.571 | −0.006 |
| 原生 LLM | 0.544 | 0.538 | −0.006 |
| `core/reviewer.py`（exp1 的"本系统"） | **0.789** | **0.715** | −0.074 |
| Agent（现状） | 0.586 | 0.629 | +0.043 |
| Agent（v4 提示词） | **0.699** | 0.667 | −0.032 |

- **换口径确实让 Agent 好看了**（+0.043），也确实让 `core/reviewer` 掉了（−0.074），
  两者差距从 0.203 缩到 0.086——说明旧口径确实偏向"报得少"的系统。
- **但没有反转结论**：在更公平的口径下，`core/reviewer` 仍然更强。
- **v4 的收益随口径变化**：单标签上 +0.113，宽松口径上只有 +0.038。
  原因很直接——GT 里问题从 1.00 增到 1.44 条/样本，而 v4 把输出压到 1.14 条/样本，
  **压过头了**。

宽松口径下 Agent 的详细指标：P 0.629→0.667 / R 0.778→0.597 / 预测 2.12→1.14 条/样本。
分类别看（现状提示词）：security 0.960、performance 0.923、bug 0.769、smell 0.629。

---

## 4. 结论与建议

1. **数据不是 Agent 分数低的原因。** 修完 4 个缺陷后，各项分数一条没变——因为数据本来就没错标。
2. **换口径能解释约 0.04，剩下的 0.09~0.20 是模型真的在报多余东西。** 即使把坏味也算进 GT，
   标注密度也只有 1.44 条/样本，而 Agent 报 2.12 条。
3. **如果决定采用多标签口径（我建议采用，更接近"审查助手"的真实用法）**，
   那么下一步的 Agent 调优方向要跟着改：把"最多 3 条 / conf≥0.75"放宽到
   **"最多 5 条 / conf≥0.70"**，否则召回会被自己压死。
4. **报告里必须两个口径都给。** 只报 0.699（单标签 + v4）会被质疑挑口径；
   给"单标签 0.586 / 多标签 0.629 / v4 多标签 0.667 / core 0.715"这套完整对照，
   反而站得住。

---

## 5. 改动清单

| 类型 | 文件 |
|---|---|
| 新增数据 | `data/dataset/dataset_dedup.json`、`eval_multilabel_strict.json`、`eval_multilabel_inclusive.json` |
| 新增文档 | `data/README.md`（数据卡） |
| 停用 | `data/legacy/broken/eval_dataset.json` |
| 新增脚本 | `data/dataset/tools/audit_dataset.py`、`experiments/annotate_multilabel.py` |
| 改默认路径 | `experiments/exp1_recognition.py`、`exp2a_…`、`exp2b_…`、`exp3_…`、`run_all.py`、`test/test_dataset_loader.py` |
| 新增结果 | `reports/exp1-multilabel-inclusive/`、`reports/agent/single_multilabel/`、`reports/agent/prompt_variants_multilabel.json` |

```powershell
$env:PYTHONIOENCODING="utf-8"

# 数据体检 + 去重
.venv\Scripts\python.exe data/dataset/tools/audit_dataset.py --dry-run

# 两种口径重标（各 100 次标注调用，约 2 分钟）
.venv\Scripts\python.exe experiments/annotate_multilabel.py `
  --policy strict    --out data/dataset/eval_multilabel_strict.json
.venv\Scripts\python.exe experiments/annotate_multilabel.py `
  --policy inclusive --out data/dataset/eval_multilabel_inclusive.json

# 在多标签 GT 上评三方 + Agent
.venv\Scripts\python.exe experiments/exp1_recognition.py `
  --dataset data/dataset/eval_multilabel_inclusive.json --max-samples 50 `
  --require-line --out-dir reports/exp1-multilabel-inclusive --workers 8
.venv\Scripts\python.exe experiments/agent_eval.py --mode single `
  --dataset data/dataset/eval_multilabel_inclusive.json --max-samples 50 `
  --out reports/agent/single_multilabel/report.json
```
