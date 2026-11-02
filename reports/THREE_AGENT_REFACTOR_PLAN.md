# 三角色 Agent 改造方案

状态：核心主线已实施（2026-10-30）。Review → 条件 Verification → Fix 的角色划分、
请求内证据缓存、证据 ID、策略路由、覆盖状态和代码快照冲突保护已经落地；真实模型效果
评估与更完整的请求总 Token 预算仍属于后续工作。

## 1. 目标与范围

面向当前以 Python 单文件审查、可选代码库上下文为主的业务，将默认按领域拆分的审查流程，调整为 Review、Verification、Fix 三角色协作。

- Review Agent：自主使用只读工具发现问题，覆盖安全、缺陷、质量、性能四类检查。
- Verification Agent：在独立上下文中检查候选成立条件、寻找反证、按需补充证据。
- Fix Agent：在用户确认后修复选定候选，根据确定性验证结果有限重试。
- LangGraph：负责阶段调度、条件路由、状态汇合和终止边界。
- 普通 Python 节点：负责基础上下文、契约校验、去重、预算、报告及验证工具执行。

本轮不增加 Coordinator、Context Agent、Judge Agent、Test Agent，不引入自由讨论或跨 Agent 无限回路。不实现仓库级自动修改、PR 发布或完整数据流分析。

## 2. 改造前实现与改造原因

改造前，`agent/langgraph/workflow.py` 通过 Send 分发领域 Agent；`agent/agents/domain.py` 提供短工具循环，`configs.py` 定义领域权限。Cross Review 仍是一次模型调用，reviewed_by 是提示信息，并未实际调用对应领域 Agent。FixPipeline 已独立存在。

主要需要解决：

1. 四个领域重复读取同一源码，任务边界重叠，多 Agent 收益尚未量化。
2. 领域扫描与后置 Verifier 可能重复运行同一分析引擎，不能当作两份独立证据。
3. 复核缺少自主查证能力，不能充分检查调用方约束或反例。
4. 当前工具证据校验主要依据工具名，仍不足以证明具体结论由某条结果支持。
5. 当前图采用内存 checkpoint；业务快照持久化不等于跨进程图恢复。

## 3. 目标流程

```text
输入代码、项目标识、审查策略
    → prepare_context（语法、AST 摘要、项目约定）
    → review_agent（只读工具循环）
    → normalize_findings（契约校验、证据引用校验、去重）
    → plan_verification（按策略和风险筛选候选）
        ├─ 无待复核项 → adjudicate
        └─ verification_agent（独立上下文、一次有预算的查证过程）
                              → adjudicate
    → finalize_report
    → 保存 review_id

用户选择 candidate_id
    → 保存 FixPlan / fix_id
    → 用户确认
    → Fix Agent
    → 语法、接口兼容、可选行为测试
        ├─ 通过 → 输出结果与验证范围
        └─ 失败 → 将具体失败信息返回 Fix Agent
                    → 有限重试，最终失败返回原代码
```

审查图和修复管线保持分离。首轮复用现有 FixPipeline 及其运行时，不为统一技术栈而重写已工作的修复代码。

## 4. 角色与权限

| 角色 | 输入 | 允许工具 | 输出 |
|---|---|---|---|
| Review | 源码、基础上下文、用户要求 | 静态分析、符号查询、项目检索、范围受限的文件读取 | Finding 列表 |
| Verification | 源码、待复核候选、相关证据、项目约定 | 同类只读查询与分析工具 | Verification 列表 |
| Fix | 用户选定候选、原始代码、验收测试 | 现有补丁与受限执行工具 | FixResult 与验证报告 |

Review 和 Verification 不共享消息历史。Verification 只读取候选相关证据，不接收初审内部推理文本。两者可以使用同一底层模型，不能把上下文隔离描述为模型错误统计独立。

工具权限由注册表白名单强制执行，不能只写入提示词。工具结果是数据，不得覆盖系统指令。工具调用参数中目标源码由运行时绑定；文件读取始终限制在显式配置的项目根目录内。

## 5. 策略和预算

以下为初始工程默认值，需通过评估调整，不代表已验证的最佳配置。

| 策略 | Review 模型调用/工具调用上限 | 复核范围 | Verification 模型调用/工具调用上限 |
|---|---|---|---|
| fast | 3 / 2 | 跳过模型复核，报告明确注明 | 0 / 0 |
| auto | 6 / 4 | 高风险、证据不足、冲突、历史误报疑似、跨文件依赖 | 4 / 3 |
| full | 8 / 6 | 全部有效候选 | 每批 4 / 3，同时受请求总预算约束 |

full 按候选数量和上下文长度分批，首轮采用顺序批处理，避免引入额外并发复杂度。每批必须保留完整的候选与必要源码范围，不允许静默截断。总预算不足的候选标为未复核。

增加请求级总耗时、模型调用及工具调用预算。工具结果字符上限、输入规模上限明确配置。工具预算耗尽后预留一次模型总结机会；仍未产生合法结果则返回 incomplete，不标为 completed。

模型请求及子进程均设置实际超时。外层 Future 超时不能终止已运行线程，不应将其表述为已取消任务；同一超时任务不立即重试，以免产生后台重复调用。

## 6. 共享证据服务

基础上下文仅预先准备便宜的确定性事实。静态扫描、检索和文件读取由 Agent 按需调用。

每次成功工具调用生成 EvidenceRecord：

```text
evidence_id、source_hash、producer_agent、tool_name、engine
normalized_arguments、status、result、file_path、line_range
```

采用请求内缓存，键包含工具、规范化参数、目标内容 hash；项目文件读取还需校验当前文件内容或版本。相同输入的重复分析复用结果，记录消费者信息。

同一引擎、同一代码版本、同一告警仅算一个证据来源。工具别名不能增加证据独立性。失败或超时结果可以用于解释覆盖缺口，不能作为确认问题的正向证据。

Finding 通过 evidence_refs 引用真实记录。验证 ID 存在、状态成功、目标一致、位置合法；原始源码片段还需检查内容与行号一致。上述校验只证明引用有效，不证明语义结论必然正确。

## 7. 数据契约

保留现有 Issue / candidate_id 对外兼容，在内部增加以下契约：

- Finding：candidate_id、category、severity、位置、claim、fix_suggestion、evidence_refs、producer。
- Verification：candidate_id、verdict（supported/refuted/insufficient）、reason、evidence_refs、missing_context。
- AgentExecution：agent、status、stop_reason、模型/工具调用数、耗时、usage、errors。
- VerificationCoverage：selected、completed、failed、skipped、unreviewed_candidate_ids。

Token 统计区分 provider_reported、estimated、unavailable，不能把缺失 usage 当作零成本。避免在 API 响应中默认暴露完整工具原文和项目文件内容，详细证据按需查看。

## 8. 复核与裁决

Verification 必须围绕原候选检查成立条件与反证；需要额外上下文时自主查询。不扩展成第二次无边界全量审查。

| 情况 | 对外结果 |
|---|---|
| 未复核、复核失败或预算不足 | needs_review，记录原因 |
| Verification supported 且证据引用有效 | confirmed，展示为“复核支持”，不宣称形式化证明 |
| Verification refuted 且提供有效反证 | rejected |
| Verification insufficient | needs_review，附缺失信息 |
| 历史误报命中但尚未解决 | memory_suspected，保留候选 |

如果当前反证与工具结果相互矛盾，保持 needs_review；不能机械地将工具命中当作最终真值。历史记忆为辅助信号，不能覆盖当前有效证据。模型自报 confidence 仅用于排序和路由，不单独决定 confirmed。

驳回候选保留在审计结果中，默认报告可折叠；不能从所有可追踪数据中消失。

## 9. 文件改造映射

| 文件 | 改动 |
|---|---|
| agent/agents/domain.py | 提取共用只读工具循环，补齐最终总结、预算退出、真实调用与证据记录 |
| agent/agents/configs.py | 改为 Review / Verification 配置；四分类转为审查职责 |
| agent/agents/review.py（新增） | 综合审查提示词、Finding 输出解析 |
| agent/agents/verification.py（新增） | 独立复核提示词、补充取证、复核输出解析 |
| agent/evidence_service.py（新增） | 请求内缓存、EvidenceRecord、证据引用查验 |
| agent/langgraph/workflow.py | 移除默认四领域 fan-out，接入 Review → 条件 Verification 图 |
| agent/langgraph/state.py | 增加证据记录、复核覆盖和角色执行状态；移除过时字段 |
| agent/schema.py | 增加 Finding、Verification 和执行结果契约 |
| agent/pipelines/review.py | 保持入口参数，输出新角色覆盖与证据统计 |
| agent/pipelines/fix.py | 复用现有实现，校验候选归属和目标代码版本 |
| agent/fixing/verification.py | 明确区分检查通过、失败、未执行 |
| agent/workflow_store.py | 保存报告及修复计划，绑定代码 hash |
| agent/settings.py / .env.example | 增加角色与请求级预算，删除无效配置 |
| api/main.py / web/app.py / cli.py | 同步角色进度、复核状态、部分成功和策略说明 |
| experiments/agent_eval.py | 增加相同初审候选下的复核对比 |
| test/test_langgraph_agent.py | 替换四领域结构测试，验证真实协作行为 |

保留已有接口路径及 fast/auto/full 参数，但其语义发生变化，必须同步 README、API 字段说明和插件文案。coverage 的键由领域变成角色，属于响应结构语义变更，所有消费者需同步修改，不伪造旧的四领域覆盖。

## 10. 实施阶段

### 阶段一：综合 Review 与工具循环

实现共用循环和 Review Agent，接入请求内证据服务，将 fast 路径切换到新实现。先验证权限、缓存、预算和结构化输出，再替换 auto/full 的初审。

验收：模型能根据工具结果继续调用不同工具；白名单外工具不会执行；未调用工具的证据引用被拒绝；预算耗尽不会报告成功。

### 阶段二：Verification 与裁决

接入条件路由、复核批处理、独立上下文和裁决规则，移除旧 Critic / Cross Review 标签式实现。

验收：新增上下文能改变复核意见；存在保护条件的候选可被驳回；工具/模型失败保留待复核状态；重复引擎结果不算新增独立证据。

### 阶段三：交付闭环

同步 API、SSE、CLI、Web、插件和文档，复用修复计划持久化与用户确认流程，补充目标代码 hash 校验。

验收：重启后可按 fix_id 继续确认；源代码变化时旧计划不可直接应用；未提供行为测试时明确显示未验证；失败回退不覆盖源文件。

### 阶段四：效果评估

在现有 50 条样本上保存一次初审候选，比较直接报告与加入复核后的结果；另行统计完整系统端到端耗时和调用成本。

记录 Precision、Recall、F1、正确候选误驳回数、错误候选消除数、复核覆盖率、耗时、Token 来源和用量。Mock 仅验证流程，不用于效果结论。

使用同一模型版本、数据、初审结果和匹配口径，避免将提示词或工具预算差异误认为多 Agent 收益。50 条样本仅支持初步结论，不预设提升百分比。

## 11. 完成标准

1. 正式审查入口仅有 Review → 条件 Verification 主线，无并行旧实现残留。
2. Review 和 Verification 具有独立上下文、只读工具权限和预算。
3. 证据可追溯到具体调用；缓存结果不会被重复计为独立支持。
4. 候选始终保留来源、复核状态和失败原因，不能用静默跳过伪装完成。
5. 人工确认后才能进入既有修复流程，测试覆盖范围如实呈现。
6. 自动化测试、API/SSE 集成测试通过；真实模型评估结果单独报告。
7. 报告持久化与图恢复能力分别说明，不宣称尚未实现的跨进程节点恢复。
