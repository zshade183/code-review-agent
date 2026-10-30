# 三方案对比实验

本目录只保留当前有效实验：在同一批 50 条数据上公平比较：

1. **基线工具**：规则引擎 + Pylint + Bandit，不调用模型；
2. **单次模型调用**：固定提示词、禁止工具、每个样本恰好调用一次；
3. **完整 Agent**：允许多步推理、工具取证与证据校验，关闭长期记忆避免泄漏。

## 运行

```powershell
# 全量正式实验（读取 .env 中的模型配置）
python experiments/exp1_recognition.py

# 离线冒烟测试；mock 结果不能用于效果结论
python experiments/exp1_recognition.py --mock --max-samples 3

# 只跑不产生模型费用的基线
python experiments/exp1_recognition.py --arms baseline
```

输入固定为 `data/eval.json`。输出为 `reports/exp1/report.json`（逐样本原始结果）
和 `reports/exp1/summary.md`（汇总表）。三组共享样本顺序、标注、随机种子与
“类别 + 行号 ±1”匹配口径。

## 多 Agent 主线消融（已编写，默认不运行）

`multi_agent_ablation.py` 比较 `fast / auto / full`，并提供去掉 Critic、Verifier、记忆的
组件消融。为避免误调用真实模型，默认仅输出 dry-run 协议：

```powershell
python experiments/multi_agent_ablation.py
```

确认实验计划后，显式加入 `--execute` 才会运行；`--mock` 可做无成本流程冒烟。

保留的辅助模块：

- `dataset_loader.py`：严格加载与确定性抽样；
- `agent_metrics.py`：统一匹配和聚合；
- `exp3_optimize_training.py`：仍被 `core/optimizer.py` 作为运行时兼容模块引用。
