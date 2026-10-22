# Code Review Agent · 基于多工具协作与自我验证的自动化代码审查智能体

一个真正意义上的 **Agent**（而非「一次性调用大模型」的流水线）：由大模型自主决定调用哪些工具取证、
基于观察迭代推理、对自己的结论做证据校验，并在修复代码时强制跑通「语法 → 接口兼容 → 沙箱行为」三级验证。

支持 **CLI / REST API（含 SSE 流式）/ MCP Server / VSCode 插件 / Streamlit Web** 五种接入方式。

---

> ### 📘 想先跑起来？
> 从安装到跑通 CLI / API / Web / 实验的完整步骤见 **[start.md](start.md)**。
> 想深入了解设计细节，见 **[代码审查Agent项目说明.md](代码审查Agent项目说明.md)**。

---

> ### 🕸️ 想看 LangGraph 版本？
> `review_assistant/` 用 LangGraph supervisor + handoff 组织主助手、领域子助手与人工确认，
> 与下面的 `agent/` 手写 ReAct 循环并存。
> 见 **[第 11 节](#11-langgraph-多智能体版review_assistant)** 或
> **[review_assistant/README.md](review_assistant/README.md)**。

---

## 1. 为什么说它是 Agent，而不是 LLM 应用

| | 一次性 LLM 流水线 | 本项目的 Agent 实现 |
|---|---|---|
| 执行顺序 | 硬编码在 Python 里：先跑规则引擎 → 再拼提示词 → 再过滤 | **由模型决定**调用哪些工具、调几次、何时收敛 |
| 工具调用 | 无（工具是流水线的一环，模型看不到） | 原生 function calling，11 个内置工具（配置代码库根目录后共 14 个），不支持时自动降级为文本 ReAct |
| 出错处理 | 网络重试 | 步数/工具次数/token/墙钟四维预算 + 重复动作拦截 + 结构化输出校验重试 |
| 结论可信度 | 无从校验 | **每个结论必须挂证据**，无证据的自动丢弃；越界行号、未调用工具的证据都会被标记 |
| 修复正确性 | 只比一次 AST 签名 | 语法 → 接口兼容 → **真实沙箱运行**，失败则带错误反思重试，仍失败则回退原代码 |
| 可观测性 | 只有日志 | 单次运行全链路 trace（每步思考/工具/观察/耗时/token），落盘 JSONL 并可聚合统计 |
| 记忆 | 无状态 | 人工反馈沉淀为长期记忆，同类误报自动压制；项目约定会被召回注入提示词 |

设计原则：**模型负责编排工具，Python 负责约束与验证。**

---

## 2. 架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 接入层                                                                    │
│  CLI (cli.py)   REST API + SSE (api/main.py)   MCP Server (mcp_server.py) │
│  VSCode 插件 (vscode-plugin/)                 Streamlit (web/app.py)      │
└──────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌──────────────────────────────────────────────────────────────────────────┐
│ Agent 运行时（agent/runtime.py，手写 ReAct Loop）                          │
│   组装消息 → 调用 LLM → ┌ 有 tool_calls → 执行工具 → 回填观察 → 继续      │
│                        └ 无 tool_calls → finalizer 校验 → 通过/带错重试    │
│   预算控制：max_steps / max_tool_calls / token 预算 / 墙钟 / 重复动作拦截   │
└──────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌──────────────────────────────────────────────────────────────────────────┐
│ 角色层                                                                    │
│  ReviewerAgent(证据闭环)  OptimizerAgent(三级验证)  MultiAgentPipeline     │
│                                              (Reviewer/Critic/Verifier)   │
└──────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌──────────────────────────────────────────────────────────────────────────┐
│ 工具层（agent/tools/，按角色裁剪白名单）                                   │
│  静态分析：规则引擎 + pylint + bandit                                      │
│  AST：结构大纲 / 签名比对 / 符号查找 / 语法检查                            │
│  代码库：BM25 检索（标识符感知分词）/ 读文件 / 跨文件符号查找               │
│  补丁：unified diff 生成与应用（校验上下文，拒绝静默产出错误结果）          │
│  沙箱：受限子进程执行 / 跑测试（违规分级：阻断级 vs 警告级）                 │
└──────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌──────────────────────────────────────────────────────────────────────────┐
│ 支撑层                                                                    │
│  schema.py(Pydantic 契约)  trace.py(可观测性)  memory.py(长期记忆)         │
│  llm.py(Provider 抽象)     mock.py(离线后端)   settings.py(预算配置)       │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 四个关键设计

### 3.1 证据闭环：把「不要幻觉」变成可执行约束

提示词里写「请不要编造」是没有约束力的。因此每个 issue 都必须携带 `evidence`：

```json
{
  "category": "Security Vulnerability",
  "subtype": "SQL Injection",
  "line_start": 7,
  "confidence": 0.9,
  "evidence": [
    {"kind": "tool_output", "tool": "run_static_analysis", "line": 7,
     "snippet": "sql = \"SELECT * FROM users WHERE id = \" + uid",
     "detail": "规则引擎命中 SQL 拼接，且该变量来自函数参数（外部输入）"}
  ]
}
```

校验器（`reviewer_agent._ReportFinalizer`）会逐条核验：

- 没有 `evidence` → **直接丢弃**（可在报告里看到「移除了 N 条缺乏可核验证据的问题」）
- 证据声明来自某个工具，但本轮**从未调用过**该工具 → 标记为未验证并降低置信度
- 证据行号超出代码范围 → 标记并降低置信度

这样「幻觉率」就从一句口号变成了可量化的数字。

### 3.2 三级验证：修复代码必须真的能跑

`OptimizerAgent.verify()` 由 harness 强制执行（不依赖模型自觉）：

1. **语法**：`ast.parse`
2. **接口兼容**：对比改动前后的函数名/参数列表，破坏调用方兼容性即失败
3. **行为**：在沙箱里真实运行测试；没有测试时做冒烟执行，并**与原代码做基线对比**
   —— 原代码本身就跑不通（模块级副作用）时不会把账算到修复头上

任一环节失败 → 把失败详情回灌给模型让它反思重试 → 仍失败则**回退原代码**，绝不交付坏修复。

### 3.3 多智能体：三个上下文独立的角色

```
Reviewer ──初版报告──→ Verifier ──独立引擎佐证──→ Critic ──裁定──→ 合并
（取证+推理）          （不依赖 LLM）        （对抗式质疑）
```

- **Verifier** 用 Reviewer **没用过**的引擎交叉验证（规则引擎得出的结论就用 pylint/bandit 复核）
- **Critic** 是「反方律师」，只拿到代码 + 结论，看不到 Reviewer 的推理过程，避免自我确认偏差
- 合并规则刻意保守：**Critic 判误报 + 无独立佐证才丢弃**；若一方判误报但独立引擎佐证，则保留并标记分歧，防止单方误删真问题

### 3.4 可观测性：每次运行都是一条可复盘的轨迹

```
$ python cli.py review demo.py
--- Agent Trace ---
stop_reason=final  steps=5  tool_calls=2  tokens=4024  duration=3.21s
  [01] think  需要先获取客观证据，调用静态分析工具扫描这段代码。
  [02] act    call run_static_analysis({'code': 'import os\n...', 'engines': 'rules'})
  [03] think  获取代码结构，确认问题所在函数与作用域。
  [04] act    call get_ast_outline({'code': 'import os\n...'})
  [05] final  {"review_summary": "共发现 2 个问题", ...}
```

轨迹同时落盘为 JSONL，可聚合出平均步数、token、成本、工具使用分布、停止原因分布（`python cli.py stats`），
也能回答「Agent 为什么失败」这类问题。

---

## 4. 快速开始

### 4.1 安装

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
```

> Agent 层**没有引入任何新的第三方依赖**，全部用标准库 + 已有依赖实现。

### 4.2 先跑离线模式（无需 API Key，零成本）

```bash
# Windows PowerShell
$env:MOCK_MODE="true"

python cli.py review examples/review_target.py     # 完整跑通 Agent Loop，看轨迹
python cli.py optimize examples/review_target.py   # 看三级验证报告
python cli.py multi examples/review_target.py      # 看多智能体协作
```

离线后端（`agent/mock.py`）会模拟一条**真实的 Agent 行为轨迹**（先调工具取证、再基于观察作答），
因此它验证的是 runtime 各分支是否真的工作，而不是返回一段假数据。

### 4.3 接入真实模型

```bash
cp .env.example .env
# 编辑 .env：填入 API_KEY 与 MODEL_NAME，并把 MOCK_MODE 设为 false
python cli.py review your_file.py
```

### 4.4 运行测试

```bash
python test/test_agent.py              # 45 项：Agent Loop / 工具层 / 证据校验 / 多智能体 / 记忆 / 评测
python test/test_review_assistant.py   # LangGraph 版：四维度委派 + 敏感操作中断 + 报告汇总
python test/test_dataset_loader.py     # 实验数据集加载（含截断修复策略）
python test/test_web_app.py            # 前端端到端：自动起 MOCK 后端 + 无头点击「开始审查」

# 有 pytest 时也可用 pytest 运行
pytest test/ -q
```

---

## 5. 使用方式

### CLI

```bash
python cli.py <file> [--optimize] [--format json] [--fail-on-high-risk]   # 无子命令调用（CI 使用）
python cli.py review   <file>        # Agent 审查
python cli.py optimize <file>        # 审查 + 带验证的优化
python cli.py multi    <file>        # 多智能体协作审查
python cli.py trace    <run_id>      # 查看某次运行的完整轨迹
python cli.py stats                  # 聚合统计（步数/token/成本/工具分布）
python cli.py memory add "本项目禁止 print，统一用 logging"
```

`--fail-on-high-risk` 会返回非 0 退出码，可直接用于 CI 卡点（见 `ci_integrate.sh`）。

### REST API

```bash
python api/main.py            # http://127.0.0.1:8000/docs
```

| 端点 | 说明 |
|---|---|
| `POST /review`、`POST /optimize` | 扁平响应结构，供插件与前端直接调用 |
| `POST /agent/review` | 报告 + 完整轨迹 + 统计（`mode=multi` 走多智能体） |
| `POST /agent/review/stream` | **SSE 流式**：实时推送 Agent 每一步（思考/工具/观察），最后推送报告 |
| `POST /agent/optimize` | 优化结果 + 三级验证报告 |
| `POST /agent/feedback` | 记录人工反馈，沉淀为长期记忆 |
| `GET /agent/trace/{run_id}` | 取回持久化轨迹 |
| `GET /agent/stats` | 聚合统计 |
| `GET /agent/memory` | 查看长期记忆库 |

SSE 事件格式：`start` → `step`（每步一个）→ `result` → `end`。

### MCP Server（可与 Claude Desktop / Cursor 等客户端互通）

```bash
python mcp_server.py --list                    # 自检：打印工具清单
python mcp_server.py --profile review          # 只暴露只读工具（推荐编辑器集成）
python mcp_server.py --codebase-root .         # 额外启用代码库检索工具
```

在客户端的 MCP 配置中登记：

```json
{
  "mcpServers": {
    "code-review-agent": {
      "command": "python",
      "args": ["C:/Projects/code_review/mcp_server.py", "--profile", "review"]
    }
  }
}
```

实现为 stdio + 换行分隔的 JSON-RPC 2.0，支持 `initialize` / `tools/list` / `tools/call` / `ping` 与批量请求。
日志一律写 stderr，stdout 只承载协议消息。

### Web 前端（Streamlit）

```bash
python api/main.py          # 终端 1：先起后端
streamlit run web/app.py    # 终端 2：再起前端
```

前端直接对接 Agent 端点：结构化问题表（可按类别/严重度筛选）、每条结论的证据详情、
SSE 实时步骤、以及一键「标记为误报」写回长期记忆。

---

## 6. 评测

```bash
# 离线基线：只用静态分析，不调用 LLM（零成本，先验证口径）
python experiments/agent_eval.py --mode rules --dataset data/processed/splits/test.jsonl --max-samples 30

# 单 Agent / 多智能体
python experiments/agent_eval.py --mode single --max-samples 50 --max-workers 4
python experiments/agent_eval.py --mode multi  --max-samples 20
```

指标分两层：

**子任务指标（识别能力）** —— P/R/F1，整体与分四类；匹配口径与 `exp1_recognition.py` 完全一致，新旧结果可直接对比。

**Agent 层指标（执行过程）** —— 工具选择准确率、任务成功率、结构化输出成功率、平均步数、平均/ P95 耗时、
平均 token 与成本、证据覆盖率、证据校验通过率、因缺证据被丢弃条数、被记忆压制条数、停止原因分布、工具使用分布。

> ⚠️ **匹配口径会被明确标注**。项目里的数据集有三套 schema，其中 BugSinPy/CodeFuse 转换出的 JSONL
> **不含行号**（`line: null`），此时会自动退化为「问题类别级」匹配并在报告中标注 `match_level=category`。
> 类别级匹配显著宽松于行号级，两者不可直接比较——这个差异会打印在结果里，避免误读。

实验脚本位于 `experiments/`，用法见 [experiments/README.md](experiments/README.md)。

---

## 7. 配置

全部通过环境变量 / `.env`（见 `.env.example`），常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_KEY` / `MODEL_NAME` | 空 | 模型接入配置，**缺失时会给出明确报错** |
| `MOCK_MODE` | false | 使用离线模拟后端，不调用真实模型 |
| `AGENT_MAX_STEPS` | 10 | 单次运行最大步数 |
| `AGENT_MAX_TOOL_CALLS` | 14 | 单次运行最大工具调用次数 |
| `AGENT_MAX_PROMPT_TOKENS` | 120000 | token 预算 |
| `AGENT_MAX_WALL_CLOCK_S` | 300 | 墙钟超时 |
| `AGENT_REPEAT_ACTION_LIMIT` | 2 | 相同参数重复调用上限 |
| `AGENT_ENABLE_SANDBOX` | true | 是否启用代码执行沙箱 |
| `AGENT_CODEBASE_ROOT` | 空 | 代码库检索范围，留空则不注册检索工具 |
| `AGENT_PRICE_*_PER_1K` | 0 | 模型单价，填写后轨迹与评测会给出成本估算 |

---

## 8. 安全说明（务必了解）

- **密钥**：`config.py` 中原先硬编码过真实 API Key，现已移除，改为只从环境变量读取。
若密钥曾被提交进仓库，请**立即轮换该密钥**。
- **沙箱能力边界**：`agent/tools/sandbox.py` 提供的是「受限子进程」——独立进程 + 超时强杀 +
  `-I` 隔离模式 + 临时工作目录 + AST 危险调用扫描 + 输出截断。
  **它没有操作系统级隔离**（无容器 / 无 seccomp），因此适合运行受信任来源或 LLM 生成的小片段，
  不适合执行完全不可信的第三方代码。生产环境应替换为 Docker / Firecracker 等强隔离方案。
- **违规分级**：危险调用（`os.system` / `subprocess` / `eval` 等）任何时候都会被拦截；
  网络与序列化类**导入**属于警告级，是否放行取决于场景（验证用户既有文件时放行，执行 LLM 新生成片段时默认严格）。
- **路径穿越**：代码库读取工具与轨迹查询接口都做了越界校验。

---

## 9. 目录结构

```
agent/                      # ① 手写 ReAct 版 Agent 核心
├── runtime.py              #   手写 ReAct Loop（预算控制 / 降级 / 结构化重试）
├── schema.py               #   Pydantic 数据契约（兼容旧格式的归一化）
├── llm.py                  #   LLM Provider（tool calling / 流式 / 退避重试）
├── tools/                  #   工具集：静态分析 / AST / 代码库检索 / 补丁 / 沙箱
├── reviewer_agent.py       #   审查 Agent（证据闭环）
├── optimizer_agent.py      #   优化 Agent（三级验证）
├── multi_agent.py          #   Reviewer / Critic / Verifier 协作
├── memory.py               #   长期记忆（误报沉淀 + 约定召回）
├── trace.py                #   可观测性（轨迹 / 成本统计 / HTML 渲染）
├── mock.py                 #   离线模拟后端
└── settings.py             #   Agent 预算与开关配置

review_assistant/           # ② LangGraph 多智能体版（图编排）
├── workflow.py             #   图编排：主助手 + 4 维度子助手 + 修复助手 + 人工确认
├── state.py                #   状态与 reducer（消息 / 对话栈 / 结论合并 / 维度进度）
├── handoffs.py             #   交接契约（Pydantic 类即工具）
├── toolset.py              #   按角色裁剪工具、挑出敏感工具
├── tool_adapter.py         #   把 agent/tools 适配成 LangChain 工具
├── retriever.py            #   规则库检索（可退化离线哈希向量）
├── policy_tools.py         #   lookup_rule 工具
├── mock_llm.py             #   离线假模型（无密钥跑通整张图）
├── prompts.py              #   各角色提示词与上下文组装
└── main.py                 #   CLI + 敏感操作确认循环

api/main.py                 # FastAPI（兼容端点 + Agent 端点 + SSE 流式）
cli.py                      # 命令行（无子命令调用 + 子命令）
mcp_server.py               # MCP Server（stdio JSON-RPC）
web/app.py                  # Streamlit 前端（结构化报告 + SSE 实时步骤 + 误报反馈）

experiments/                # 对比实验与评测
├── dataset_loader.py       #   数据集加载（带截断修复策略）
├── agent_eval.py           #   Agent 评测 runner
├── agent_metrics.py        #   Agent 专项指标
├── exp1_recognition.py     #   实验 1：问题识别能力
├── exp2a_component_ablation.py / exp2b_interaction_mode.py   # 实验 2：组件消融 / 交互模式
├── exp3_optimize.py        #   实验 3：修复能力
├── *_plot.py               #   各自的出图脚本
├── plot_style.py           #   统一的绘图样式与公共组件
└── run_all.py              #   一键跑完全部实验并出图

core/                       # 规则引擎与数据处理（作为工具被复用）
dataset/                    # 实验数据集（legacy/ 为备用样本，默认不参与实验）
data/                       # 原始与处理后的数据集
examples/                   # 可直接拿去审查的示例代码
test/                       # 测试脚本
reports/                    # 实验结果与图表
```

文档分工：[start.md](start.md) 讲从零跑通的步骤，`README.md`（本文件）讲整体架构与用法，
[代码审查Agent项目说明.md](代码审查Agent项目说明.md) 逐节讲 LangGraph 版的实现细节，
[experiments/README.md](experiments/README.md) 讲实验与出图。

---

## 10. 已知限制

- 沙箱无系统级隔离，见第 8 节。
- 部分数据集只有类别标注、没有行号与具体描述，该类数据只能做类别级评测，无法评估定位精度。
- Multi-Agent 模式会成倍增加 LLM 调用次数（Reviewer + Critic 各一轮），成本与延迟高于单 Agent；
  建议先用单 Agent，仅在误报率无法接受时启用。
- 检索层用的是 BM25 + 标识符分词（零依赖、离线可用）。若需要语义级检索，
  实现 `agent/memory.py` 的 `Embedder` 协议注入真实 embedding 即可。

---

## 11. LangGraph 多智能体版（`review_assistant/`）

`agent/` 的编排是**手写**的（`agent/runtime.py` 里的 ReAct 循环 + 线性流水线）；
`review_assistant/` 是同一批工具与数据契约的**图编排**实现，设计对齐「旅行助手」项目：

| 旅行助手项目 | 本项目（`review_assistant/`） |
|---|---|
| `workflow.py`：主助手 + 4 个业务子助手 | 主助手 + 4 个**维度**子助手 + 修复助手 |
| `handoffs.py`：`ToFlightAssistant` 等 | `ToSecurityAssistant` / `ToBugAssistant` / … |
| `state.py`：`messages` + `dialog_state` 栈 | 同上，另加 `target` / `issues` / `reviewed_dimensions` |
| `*_tools.py`：按业务裁剪工具 | `toolset.py`：四个维度只拿**只读**工具 |
| `safe_tools` / `sensitive_tools` 拆分 | 复用工具的 `mutating` 标记自动分流 |
| `interrupt_before` 敏感节点 + `main.py` 人工确认 | 执行/改代码前停下来问用户 |
| `retriever_vector.py` + `policy_tools.py` | `retriever.py` + `policy_tools.py`（规则库 + 项目约定） |
| `llm.py`：provider 工厂 | 同样支持 openai / deepseek / **mock** |

维度划分直接对应数据集标签与 `Category` 枚举：
安全 / 缺陷 / 坏味 / 性能，各自一个子助手，结论经统一的证据校验后合并。

### 运行

```bash
# 离线跑通全流程（不需要任何 API Key）
python -m review_assistant.main --file examples/review_target.py --question "审查这个文件" --mock

# 交互式对话（同一个会话可以连续追问；修复类操作会停下来请你确认）
python -m review_assistant.main --file demo.py --codebase-root .

# 结构化输出
python -m review_assistant.main --file demo.py --question "审查这个文件" --format json

# 离线端到端测试
python test/test_review_assistant.py
```

模型后端自动选择：填了 `LLM_API_KEY`/`LLM_MODEL`（或沿用旧的 `API_KEY`/`MODEL_NAME`）
就走真实模型，否则自动退化为离线 mock；也可以用 `--mock` / `--provider` 显式指定。

详细设计与文件对照见 **[review_assistant/README.md](review_assistant/README.md)**；
逐节讲解实现细节、代码与「为什么」的教学式文档见
**[代码审查Agent项目说明.md](代码审查Agent项目说明.md)**。
