# review_assistant · LangGraph 版代码审查 Agent

一个用 **LangGraph 图编排**的代码审查多智能体系统：主助手按维度委派、
子助手只做只读取证、敏感操作在执行前暂停等待确认。它与 `agent/`（手写 ReAct 循环）
并存，共用同一批工具、数据契约、规则库与长期记忆。

## 1. 模块一览

| 文件 | 作用 |
|---|---|
| `workflow.py` | 建图：主助手、子助手、收集节点、路由、人工确认 |
| `state.py` | 状态与 reducer（消息、对话栈、结论、已完成维度） |
| `handoffs.py` | 交接用的 Pydantic 契约 |
| `llm.py` | openai / deepseek / mock provider 工厂 |
| `toolset.py` + `tool_adapter.py` | 把项目内 14 个工具按角色裁剪并适配成 LangChain 工具 |
| `policy_tools.py` | `lookup_rule`：规则库检索 |
| `retriever.py` | 规则库 + 项目约定的向量检索（可退化离线） |
| `main.py` | CLI 入口 + 敏感操作人工确认循环 |
| `dimensions.py` | 审查维度的注册表 |
| `findings.py` | 子助手结论的解析与证据校验 |
| `mock_llm.py` | 离线假模型，让整张图无密钥可跑 |

## 2. 角色划分

审查按四大类问题拆分子助手，与 `agent/schema.py` 的 `Category` 枚举、数据集标签完全一致：

| 子助手 | 维度 | 可用工具 |
|---|---|---|
| `security_assistant` 安全审查助手 | Security Vulnerability | 只读工具 + `scan_dangerous_code` |
| `bug_assistant` 缺陷审查助手 | Potential Bug | 只读工具 |
| `smell_assistant` 规范与坏味审查助手 | Code Smell | 只读工具 |
| `performance_assistant` 性能审查助手 | Performance Issue | 只读工具 |
| `fix_assistant` 修复助手 | 修复（不是审查维度） | 只读工具 + 补丁 + **敏感工具** |

只读工具 = `run_static_analysis` / `get_ast_outline` / `find_symbol` / `check_syntax`
/ `lookup_rule` /（配置了代码库根目录后的）`search_codebase`、`read_project_file`、
`find_symbol_in_project`。

主助手只有 `lookup_rule` + 代码库检索 + 5 个交接工具：**它不下结论、不改代码**。

## 3. 一次审查的节点序列

```
load_target                     读入待审查文件，缓存进状态
  ↓
primary_assistant               决定先委派哪个维度
  ↓ ToSecurityAssistant（handoff 工具调用）
enter_security_assistant        把交接调用变成一条 ToolMessage，助手入栈
  ↓
security_assistant              调 run_static_analysis 取证
  ↓
security_assistant_safe_tools   执行只读工具
  ↓
load_target → security_assistant  回到栈顶助手，基于观察继续
  ↓ 无工具调用，输出结论 JSON
collect_security_assistant      解析 → 证据校验 → 合并进 state.issues → 出栈
  ↓
primary_assistant              看到进度行，继续委派下一个维度……
  ↓ 四个维度都完成
finalize_report                汇总去重、记忆压制、产出结构化报告 → END
```

修复链路额外多一层：

```
fix_assistant → fix_assistant_safe_tools（check_syntax）
              → fix_assistant_sensitive_tools  ← interrupt_before，暂停
                  用户批准 → 执行 → 回到助手
                  用户拒绝 → 注入 rejected 的 ToolMessage，工具不运行
              → collect_fix_assistant（harness 强制语法 + 接口兼容性校验）
```

## 4. 四个关键设计

**1）状态用 reducer 合并，而不是靠 Python 变量传递。**
`issues` 的 reducer 按 `(分类, 二级分类, 起始行)` 去重，保留置信度更高的一条；
`reviewed_dimensions` 只追加不重复；`dialog_state` 是一个显式的栈。
多个子助手先后写入同一字段不会互相覆盖，这是「多智能体共享记忆」在 LangGraph 里的标准做法。

**2）进度显式注入主助手提示词。**
主助手每轮都能看到「已完成 / 未完成」的维度列表，因此知道该继续委派还是该收口，
不依赖它自己去翻聊天记录。

**3）安全边界由图的拓扑决定，不由提示词决定。**
`mutating=True` 的工具被单独挑出来放进 `*_sensitive_tools` 节点，并在 `compile()` 时
用 `interrupt_before` 拦住；四个审查助手根本拿不到这些工具。提示词可以被绕过，
图的结构不行。

**4）证据闭环下沉到收集节点。**
每个子助手的结论都要过同一道闸门：JSON 契约 → 行号范围 → 是否带证据 →
声明的证据工具本轮是否真的调用过 → 二级分类是否在白名单内。无证据的结论**直接丢弃**
（不是降权），分类越界会被强制归位并记录说明。

## 5. 与 `agent/` 的关系

| | `agent/`（手写循环） | `review_assistant/`（图编排） |
|---|---|---|
| 编排 | 手写 ReAct 循环 + 线性流水线 | LangGraph `StateGraph` |
| 终止 | 八类停止条件（步数/工具数/token/墙钟/…） | 图自然收敛 + `recursion_limit` |
| 多智能体 | Reviewer / Critic / Verifier 三次串行调用 | 主助手 + 5 子助手，共享状态 |
| 人工确认 | 无（靠沙箱违规分级） | `interrupt_before` + CLI 确认 |
| 上下文 | 每个角色独立组装 | 共享一张消息列表 + 状态注入 |

两者复用同一批 `agent/tools/*`、`agent/schema.py`、`agent/memory.py`、`core/prompts.py`，
可以对照着读：同一件事在「手写循环」和「图编排」两种范式下分别怎么写。

## 6. 已知取舍

- **代码要进每次调用的系统消息**：助手必须拿到源码
  才能把它作为参数传给工具。因此长文件建议把 `--codebase-root` 配上，让助手改用
  `read_project_file` / `search_codebase` 按需读取（`prompts.numbered_code` 默认在
  20000 字符处截断）。
- **一次只允许一个工具调用**：路由节点会拒绝「一轮多个调用」，让交接与状态流转可预测，
  代价是长任务需要更多轮次。
- **mock 模型不做真实修复**：它只验证链路（路由、合并、校验、中断、汇总）是否工作，
  不产生真实结论，正式使用请配置真实模型。
- **沙箱执行必须人工确认**：修复助手要跑 `execute_python_code` / `run_tests` 时会暂停等待；
  `collect_fix_assistant` 只做无副作用的语法与接口校验，不会绕过这道闸门。
