# 快速开始 · 运行流程

按「装环境 → 配模型 → 跑起来 → 看懂一次请求怎么走」的顺序介绍本项目。
只想先看到效果，直接跳到 [3. 五分钟跑通](#3-五分钟跑通)。

## 0. 三条使用路径

| 想做什么 | 入口 | 是否需要模型密钥 |
|---|---|---|
| 在终端审查/修复一个文件 | `cli.py` 或 `review_assistant.main` | 离线模式不需要 |
| 给别人/别的程序调用 | `api/main.py`（REST + SSE） | 离线模式不需要 |
| 用浏览器操作 | `web/app.py`（需要后端） | 离线模式不需要 |
| 在编辑器里用 | `vscode-plugin/`（需要后端） | 离线模式不需要 |
| 给 MCP 客户端用 | `mcp_server.py`（stdio） | 离线模式不需要 |
| 跑论文实验 | `experiments/run_all.py` | `--mode local` 不需要 |

项目里有两套编排实现，共用同一批工具：

- `agent/`：手写 ReAct 循环，`cli.py` / `api/` / `mcp_server.py` 默认走它
- `review_assistant/`：LangGraph 多智能体图，`python -m review_assistant.main` 走它

---

## 1. 环境准备

需要 **Python 3.10+**（推荐 3.12）。

```powershell
cd C:\Projects\code_review

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

依赖分三组：

| 用途 | 依赖 |
|---|---|
| CLI / API / 工具 | fastapi、uvicorn、requests、pydantic、python-dotenv |
| LangGraph 版 | langgraph、langchain-core、langchain-openai |
| Web 前端 | streamlit |

`pylint` 与 `bandit` 也在 requirements 里；缺任意一个时静态分析会自动跳过对应引擎，
不会中断审查。Windows 控制台建议先设置一次编码：

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

---

## 2. 配置模型

密钥只放在 `.env`（已被 `.gitignore` 排除），不要写进源码。

```powershell
Copy-Item .env.example .env
```

### 2.1 先离线跑（推荐的第一步）

不填任何密钥，把 `MOCK_MODE` 设为 true，或者给命令加 `--mock`：

```ini
MOCK_MODE=true
```

离线后端会模拟一条**真实的 Agent 行为轨迹**（先调工具取证、再基于观察作答），
用来验证流程是否通，不会产生真实模型结论。

### 2.2 接真实模型

```ini
API_KEY=你的密钥
API_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
MODEL_NAME=你的模型 endpoint ID
MOCK_MODE=false
```

任意 OpenAI 兼容网关都可以（火山方舟 / DashScope / vLLM 等），只要 `API_URL` 是
`/chat/completions` 地址。`review_assistant` 还支持专属变量 `LLM_PROVIDER / LLM_MODEL /
LLM_API_KEY / LLM_BASE_URL`，未配置时会自动回落到上面的通用变量，所以一份 `.env` 两边通用。

| 变量 | 作用 |
|---|---|
| `API_KEY`、`API_URL`、`MODEL_NAME` | 通用模型接入 |
| `MOCK_MODE` | true = 离线假模型，不联网 |
| `LLM_PROVIDER` | `openai` / `deepseek` / `mock` |
| `AGENT_CODEBASE_ROOT` | 代码库检索工具的检索根目录，留空则不启用 |
| `AGENT_ENABLE_SANDBOX` | 是否注册 `execute_python_code` / `run_tests` |
| `AGENT_MAX_STEPS` 等 | 手写版 Agent 的预算控制 |

---

## 3. 五分钟跑通

### 3.1 手写 Agent：CLI

```powershell
$env:MOCK_MODE = "true"

python cli.py review examples/review_target.py        # 审查 + 完整轨迹
python cli.py optimize examples/optimize_target.py    # 审查 + 修复 + 三级验证
python cli.py multi examples/review_target.py         # Reviewer / Critic / Verifier
```

### 3.2 LangGraph Agent：图编排

```powershell
python -m review_assistant.main --file examples/review_target.py --mock --question "审查这个文件"
```

去掉 `--question` 进入交互模式，可以连续追问；修复类操作会在终端停下来等你确认。

### 3.3 API + Web

```powershell
# 终端 1
python api/main.py

# 终端 2
streamlit run web/app.py
```

浏览器打开 Streamlit 提示的地址（默认 http://localhost:8501），侧边栏确认后端地址为
`http://127.0.0.1:8000` 即可。接口文档在 http://127.0.0.1:8000/docs 。

---

## 4. 一次审查的完整流程

### 4.1 手写 Agent（`agent/`）

```
用户 / CLI / API / MCP
   │  提交代码
   ▼
ReviewerAgent.build_user_prompt()          代码加行号 + 项目记忆
   ▼
AgentRuntime.run()  ReAct 循环
   ├─ 调模型 ──► 有 tool_calls ──► 执行工具 ──► 回填观察 ──┐
   │                              ▲                       │
   └─ 无 tool_calls ──► finalizer 校验 ──► 通过 ──► 报告   │
                           │ 不通过（结构化重试）          │
                           └──────────────────────────────┘
   ▼
EvidencePolicy 逐条核验（行号 / 证据 / 声明的工具是否真调过）
   ▼
ReviewReport + AgentRun 轨迹（JSONL 落盘 traces/）
```

修复走 `OptimizerAgent`：生成代码 → 语法 → 接口兼容 → 沙箱行为，
失败带错误重试，仍失败则回退原代码。

### 4.2 LangGraph Agent（`review_assistant/`）

```
load_target                读入待审查文件，缓存进状态
   ▼
primary_assistant          主助手：按维度委派
   ├─ ToSecurityAssistant ──► enter ──► security_assistant ──► 只读工具
   ├─ ToBugAssistant ──────► ...                                  │
   ├─ ToSmellAssistant ────► ...                                  │
   ├─ ToPerformanceAssistant ► ...                                ▼
   └─ ToFixAssistant ──────► fix_assistant ──► 敏感工具（interrupt_before，需确认）
                                                                  │
                           collect_<子助手> 解析 → 证据校验 → 合并  │
                                                                  ▼
                                       primary_assistant 收口
                                                                  ▼
                                       finalize_report → END
```

每个子助手只拿本维度需要的只读工具；无证据的结论在 `collect_` 节点被直接丢弃。
修复助手的 `execute_python_code` / `run_tests` / `apply_patch` 会停在
`fix_assistant_sensitive_tools`，由 CLI 或 Web 请求用户确认后再继续。

### 4.3 关键数据

| 数据 | 位置 | 说明 |
|---|---|---|
| 证据 | 每条 `Issue.evidence` | 工具输出 / AST 节点 / 代码片段 / 规则 / 测试结果 |
| 轨迹 | `traces/*.jsonl` | 每步的思考、工具调用、观察、耗时、token |
| 记忆 | `memory/agent_memory.jsonl` | 人工反馈与项目约定 |
| 报告 | 函数返回值 / `GET /agent/trace/{run_id}` | 可下载为 JSON |

---

## 5. 各入口启动方式

### 5.1 CLI（手写 Agent）

```powershell
python cli.py review   <file> [--format text|json] [--codebase-root .]
python cli.py optimize <file> [--test-file <test.py>]
python cli.py multi    <file>
python cli.py trace    <run_id>
python cli.py traces
python cli.py stats
python cli.py memory add "本项目禁止 print，统一用 logging"
python cli.py memory list
```

不带子命令的 `python cli.py <file> --fail-on-high-risk` 是 CI 使用的形式，
输出结构与退出码固定，见 `ci_integrate.sh`。

### 5.2 LangGraph CLI

```powershell
python -m review_assistant.main --file <path> [参数]
```

| 参数 | 说明 |
|---|---|
| `--question "..."` | 只跑一轮后退出；不填进入交互模式 |
| `--mock` | 强制离线模型 |
| `--provider openai\|deepseek\|mock` | 指定模型后端 |
| `--codebase-root .` | 启用跨文件检索工具 |
| `--yes` | 自动批准敏感操作（仅脚本/测试） |
| `--format json` | 输出结构化结果 |
| `--no-memory` | 关闭历史误报压制 |
| `--quiet` | 不打印每个节点的运行时提示 |

### 5.3 REST API

```powershell
python api/main.py            # http://127.0.0.1:8000
```

| 端点 | 用途 |
|---|---|
| `POST /review`、`POST /optimize` | 扁平响应，供插件/前端调用 |
| `POST /agent/review` | 结构化报告 + 轨迹；`mode=multi` 走多智能体 |
| `POST /agent/review/stream` | SSE：`start → step… → result → end` |
| `POST /agent/optimize` | 修复结果 + 三级验证报告 |
| `POST /agent/feedback` | 记录人工反馈，写入长期记忆 |
| `GET /agent/trace/{run_id}` | 取回某次轨迹 |
| `GET /agent/stats` | 多次运行的聚合统计 |

### 5.4 Web

```powershell
streamlit run web/app.py
```

页面上可以：结构化查看问题（按类别/严重度筛选）、展开证据、查看 SSE 实时步骤、
把结论标记为误报、在「代码修复」页查看三级验证结果。

### 5.5 MCP Server

```powershell
python mcp_server.py --list                  # 自检：打印工具清单
python mcp_server.py --profile review        # 只暴露只读工具
python mcp_server.py --profile optimize      # 暴露补丁/沙箱工具
python mcp_server.py --codebase-root .       # 额外启用代码库检索
```

在客户端配置里登记 `command: python`、`args: [<仓库路径>/mcp_server.py, --profile, review]`。
协议为 stdio + 换行分隔的 JSON-RPC 2.0，日志走 stderr。

### 5.6 VSCode 插件

```powershell
cd vscode-plugin
npm install
npm run compile
```

在 VSCode 中按 F5 启动扩展开发宿主，或把目录打包成 VSIX 安装。
插件设置项 `aiReviewer.apiHost` 默认 `http://127.0.0.1:8000`，使用前先启动 API。
命令面板里有「AI 代码审查」「AI 代码优化」及选区版本。

---

## 6. 自检与测试

```powershell
python test/test_agent.py              # 45 项：Agent Loop / 工具层 / 证据校验 / 多智能体 / 记忆
python test/test_review_assistant.py   # LangGraph：维度委派 / 敏感操作中断 / 报告汇总
python test/test_dataset_loader.py     # 数据集加载与截断修复
python test/test_web_app.py            # 前端端到端（自动起 MOCK 后端 + 无头点击）
```

全部使用离线后端，不需要 API Key。也可以用 `pytest test/ -q` 统一跑。

---

## 7. 实验与出图

```powershell
python experiments/run_all.py --mode local            # 零成本跑完 exp1/2a/2b/3 并出图
python experiments/run_all.py --mode remote           # 接真实模型
python experiments/run_all.py --only exp2a exp2b --max-samples 30
```

产物落在 `reports/paper/`：

```
reports/paper/exp1/                     report.json + 2 张图
reports/paper/exp2/exp2a_ablation/      report.json + plots/01..05.png
reports/paper/exp2/exp2b_interaction/   report.json + plots/01..05.png
reports/paper/exp3/                     report.json + 4 张图
```

跨模型对比图是独立步骤（要读多个模型的报告）：

```powershell
python experiments/exp1_comparison_plot.py
python experiments/exp3_comparison_plot.py --metric fix_rate
```

所有图共用 `experiments/plot_style.py` 的样式与配色，详见
[experiments/README.md](experiments/README.md)。

---

## 8. 目录速查

```
agent/                  手写 ReAct Agent（runtime / tools / reviewer / optimizer / multi_agent）
review_assistant/       LangGraph 多智能体（workflow / state / handoffs / toolset / main）
api/main.py             FastAPI（扁平端点 + Agent 端点 + SSE）
web/app.py              Streamlit 前端
cli.py                  命令行
mcp_server.py           MCP Server
experiments/            对比实验、评测与出图
core/                   规则引擎与数据处理（作为工具复用）
dataset/                实验数据集（legacy/ 为备用样本）
examples/               可直接审查的示例代码
test/                   测试脚本
reports/                实验结果与图表
```

## 9. 常见问题

**没有 API Key 能跑吗？**
能。设 `MOCK_MODE=true`，或给命令加 `--mock`。离线模型只验证流程，不产生真实结论。

**端口 8000 被占用？**
改 `api/main.py` 末尾的 `uvicorn.run(port=...)`，同时改 Web 侧边栏 / 插件设置里的地址。

**Web 提示连不上后端？**
先启动 `python api/main.py`；再检查侧边栏的 API 地址是否带 `http://`、端口是否一致。

**修复时提示“用户拒绝执行”？**
`execute_python_code` / `run_tests` / `apply_patch` 属于敏感工具，需要确认。交互模式下输入 `y`；
脚本里可以传 `--yes`，但这只适合受控环境。

**提示 pylint / bandit 不可用？**
执行 `pip install pylint bandit`，或忽略——静态分析会自动降级到剩余引擎。

**长文件审查很慢或超预算？**
配置 `AGENT_CODEBASE_ROOT` 让 Agent 用检索工具按需读文件；手写版可调 `AGENT_MAX_STEPS` /
`AGENT_MAX_TOOL_CALLS`，LangGraph 版可调 `--max-chars` 与 `recursion_limit`。

**Windows 控制台中文乱码？**
先执行 `$env:PYTHONIOENCODING = "utf-8"` 再运行。
