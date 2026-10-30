# 多 Agent 主线改进与实施文档

> 实施状态（2026-10-30）：主线重构已经落地。当前已具备共享证据、`fast/auto/full`
> 策略、四领域并行 Reviewer、确定性合并、选择性 Critic、独立 Verifier、裁决后记忆调整、
> 部分成功和独立 Fix Pipeline。SQLite 记忆、持久化人工确认、服务鉴权/限流、Docker
> 执行隔离选项、CI 工作流和多 Agent 消融实验协议也已实现；实验脚本暂未实际运行。

如果确定以多 Agent 为主线，这是可行的，而且更符合项目的差异化定位。但不建议直接把当前 LangGraph Supervisor 版本设为主线，而应该建设一套“确定性编排的多 Agent 审查管线”。

核心原则是：

> 多 Agent 是默认能力，但不是每次请求都无条件调用所有 Agent。

## 推荐定位

项目可以明确定位为：

> 基于共享证据、领域专家协作、独立验证和确定性裁决的多 Agent 代码审查系统。

架构只保留一条正式主线：

```text
Evidence Collector
→ 领域 Reviewer 并行审查
→ Candidate Merger
→ Critic / Verifier 并行复核
→ Deterministic Adjudicator
→ Memory Adjustment
→ Final Report
→ 用户确认
→ Fix Agent
```

现有模块的定位调整为：

- `agent/`：正式多 Agent 运行时和业务实现。
- `review_assistant/`：逐步合并进主线，或只保留 LangGraph 编排适配层。
- `core/`：纯规则、静态分析和验证能力，不再负责完整审查流程。
- 单 Agent：保留为快速模式和实验基线，不再作为默认入口。

## 主线角色设计

### 1. Orchestrator

Orchestrator 使用 Python 或固定 LangGraph 拓扑，不使用 LLM 决定固定流程。

负责：

- 创建任务状态
- 收集公共证据
- 选择需要启动的领域 Agent
- 并行调度
- 管理预算、超时和失败
- 汇总结果
- 调用最终裁决

Orchestrator 不分析代码，也不产生审查结论。

### 2. Evidence Collector

先执行一次公共取证：

- 语法检查
- AST 提取
- 规则扫描
- 符号与导入分析
- 代码库检索
- 项目约定和实体记忆召回

输出统一 `EvidenceBundle`。

这一步很重要，否则多 Agent 只会变成多个模型重复执行相同工具。

### 3. 领域 Reviewer

建议保留四个：

- Security Reviewer
- Bug Reviewer
- Quality Reviewer
- Performance Reviewer

每个 Reviewer：

- 只处理自己的类别
- 使用独立消息上下文
- 共享源码和原始证据
- 不提前读取其他 Reviewer 的结论
- 只拥有只读工具
- 输出统一的 `FindingCandidate`

如果某个维度不适用，可以不启动。例如纯数据模型文件通常不需要复杂性能 Agent。

### 4. Candidate Merger

Candidate Merger 使用确定性 Python 实现：

- 规范化分类
- 校验行号
- 校验证据
- 合并重复问题
- 检测跨 Agent 冲突
- 生成稳定 `candidate_id`

它不能依赖 LLM，否则相同输入可能产生不稳定的合并结果。

### 5. Independent Verifier

Verifier 不是 LLM Agent，而是独立验证角色：

- 查看 Reviewer 实际调用过的工具
- 选择未使用的静态分析器
- 对候选位置和类别进行交叉验证
- 输出佐证和能力限制

“未找到佐证”只能降低置信度，不能自动证明问题不存在。

### 6. Selective Critic

Critic 只处理需要复核的问题：

- 高危问题
- 低置信度问题
- 没有独立佐证的问题
- 多个 Reviewer 结论冲突的问题
- 命中历史误报的问题
- 修复风险较大的问题

这样既保留对抗式复核，又不会让成本翻倍。

### 7. Deterministic Adjudicator

最终裁决由 Python 规则完成，每条候选进入：

- `confirmed`
- `needs_review`
- `memory_suspected`
- `rejected`

不要增加 Judge Agent。否则最终结果又回到“由一个模型决定其他模型是否正确”。

### 8. Memory Agent 不需要独立存在

记忆更适合做服务或策略层，而不是一个自主 Agent。

它在裁决后提供：

- 历史误报概率
- 项目约定
- 文件和组件事实
- 反馈时间
- 相似代码形态

记忆默认只调整置信度和分组，不应在独立验证前删除候选。

### 9. Fix Agent 独立成第二条管线

审查和修复分离：

```text
Review Pipeline
→ 用户选择问题
→ Fix Pipeline
→ 语法/API/测试验证
```

只有 Fix Agent 可以获得：

- 补丁工具
- 代码执行工具
- 测试工具

领域 Reviewer、Critic 和 Verifier始终保持只读。

## 多 Agent 不应等于固定全量调用

建议提供三种策略：

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `fast` | 单 Reviewer + 基础验证 | 编辑器实时检查 |
| `auto` | 根据代码和风险选择领域 Agent | 默认模式 |
| `full` | 四领域 Reviewer + Critic + Verifier | PR、CI、重要代码 |

API 可以从当前的：

```json
{"mode": "single"}
```

改成：

```json
{"strategy": "auto"}
```

推荐默认使用 `auto`，而不是每次都跑完整多 Agent。

## 推荐目录结构

```text
agent/
├── runtime.py
├── schema.py
├── memory.py
├── trace.py
├── orchestrator.py
│
├── evidence/
│   ├── collector.py
│   ├── store.py
│   └── verifier.py
│
├── reviewers/
│   ├── base.py
│   ├── security.py
│   ├── bug.py
│   ├── quality.py
│   └── performance.py
│
├── adjudication/
│   ├── merger.py
│   ├── critic.py
│   ├── policy.py
│   └── memory_policy.py
│
├── pipelines/
│   ├── review.py
│   └── fix.py
│
└── fixing/
    ├── agent.py
    └── verification.py
```

## 是否继续使用 LangGraph

可以继续使用，但建议只把它作为编排基础设施，而不是让 LLM Supervisor 控制固定流程。

适合用 LangGraph 实现：

- 并行领域审查
- 条件 Critic
- 部分失败恢复
- 检查点
- 用户确认
- Fix Agent 中断和恢复

不建议使用 LLM 决定：

- 下一步调用哪个固定领域
- 是否已经完成四个维度
- 什么时候进入汇总
- 是否需要执行确定性验证

这些应该通过条件边和 Python 路由函数实现。

## 失败处理必须支持部分成功

多 Agent 主线不能因为一个角色失败而整次失败。

例如：

```text
Security：成功
Bug：成功
Quality：超时
Performance：成功
```

最终仍应返回其他三个维度的结果，并明确标记：

```json
{
  "coverage": {
    "security": "completed",
    "bug": "completed",
    "quality": "timeout",
    "performance": "completed"
  },
  "partial": true
}
```

每个 Agent 应有独立：

- 超时
- 重试次数
- Token 预算
- 错误记录
- 降级策略

## 入口调整

如果多 Agent 成为主线，建议统一入口：

```powershell
python cli.py review demo.py
python cli.py review demo.py --strategy fast
python cli.py review demo.py --strategy full
```

```http
POST /agent/review
```

```json
{
  "code": "...",
  "project": "demo",
  "session_id": "review-001",
  "strategy": "auto"
}
```

VS Code 默认使用 `fast` 或 `auto`，CI 和 PR 审查使用 `full`。

现有 `multi` 子命令可以逐步废弃，因为正式审查本身就是多 Agent。

## 实施顺序

第一阶段：

1. 将 `MultiAgentReviewPipeline` 设为默认入口。
2. 单 Agent 保留为 `fast` 策略。
3. 记忆压制移到最终裁决后。
4. Critic 和 Verifier并行。
5. 根据真实 trace 选择独立验证引擎。

第二阶段：

1. 建立 Evidence Collector。
2. 定义共享 `EvidenceBundle`。
3. 拆出四个领域 Reviewer。
4. 实现确定性 Candidate Merger。
5. 支持部分成功。

第三阶段：

1. 建立 `auto` 动态策略。
2. Critic 改为选择性调用。
3. Review Pipeline 与 Fix Pipeline 分离。
4. 将 LangGraph 的检查点和人工确认能力接入正式主线。

第四阶段：

1. 建立多 Agent 消融实验。
2. 比较 `fast`、`auto`、`full` 的准确率、成本和延迟。
3. 将记忆迁移至 SQLite。
4. 完成 CI、认证和执行隔离。

结论是：以多 Agent 为主线很适合这个项目，但主线应该是“确定性编排的专家协作系统”，而不是“由 Supervisor Agent 反复决定固定流程”。这样既能保留多 Agent 的项目特色，又能控制成本、延迟和不确定性。
