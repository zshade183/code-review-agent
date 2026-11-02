# Code Review Agent

以 LangGraph 多 Agent 为主线的 Python 代码审查与修复项目，支持命令行、REST API、
VS Code 插件、Streamlit 页面和 MCP 接入。Review Agent 自主使用只读工具发现问题，
风险候选交给独立 Verification Agent 查证；用户确认后由 Fix Agent 生成候选修复，
确定性 Harness 强制完成验证、diff 和失败回退。

`fast` 只运行初审，`auto` 按风险复核候选，`full` 复核全部有效候选。

## 安装与配置

使用 Python 3.10+（推荐 3.12）。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env  # 仅首次创建，已有配置时跳过
$env:PYTHONIOENCODING = "utf-8"
```

编辑 `.env`：

```ini
API_KEY=你的密钥
API_URL=https://你的服务地址/v1/chat/completions
MODEL_NAME=你的模型名称
MOCK_MODE=false
```

`API_URL` 使用完整的 chat/completions 接口地址。离线验证可设置
`$env:MOCK_MODE="true"`；切换真实模型前执行 `$env:MOCK_MODE="false"`。
离线模式用于检查调用流程，优化时可能返回原代码。

可选配置见 `.env.example`：`AGENT_MAX_STEPS`、`AGENT_MAX_TOOL_CALLS`、
`AGENT_MAX_WALL_CLOCK_S` 控制修复 Agent 预算；`AGENT_DEFAULT_STRATEGY` 设置默认审查档位；
`AGENT_REVIEWER_TIMEOUT_S` 和 `AGENT_REVIEWER_RETRIES` 控制领域任务；`AGENT_CODEBASE_ROOT`
指定检索目录。多 Agent 路由和状态由 LangGraph 管理。
Pylint 与 Bandit 缺失时会跳过对应引擎。代码执行使用受限子进程，不是容器隔离。

## 命令行

在项目根目录运行，文件参数替换为待审查的 Python 文件：

```powershell
python cli.py --help
python cli.py review your_file.py --strategy auto --format json
python cli.py review your_file.py --strategy fast --no-memory
python cli.py review your_file.py --strategy full --output review.txt
python cli.py fix your_file.py --yes
python cli.py optimize your_file.py --test-file acceptance_test.py
python cli.py traces
python cli.py trace RUN_ID
python cli.py stats
python cli.py memory list
python cli.py memory add "本项目统一使用 logging"
python cli.py candidates list
```

`review` 是统一审查入口；旧 `multi` 子命令仅作兼容，等价于 `review --strategy full`。
审查默认读取历史记忆，`--no-memory` 禁用；记忆只在裁决后降权和分组，不会提前删除候选。
`fix` 默认先展示修复计划，`--yes` 后才执行 Fix Agent。优化结果不会自动覆盖源文件。
兼容入口支持 CI 风险检查：

```powershell
python cli.py your_file.py --format json --fail-on-high-risk
python cli.py your_file.py --optimize --output report.txt
```

`--fail-on-high-risk` 属于无子命令入口，发现高风险问题时返回退出码 1。

## API 与 Web

分别在两个终端启动：

```powershell
python api/main.py
```

```powershell
streamlit run web/app.py
```

API 文档位于 http://127.0.0.1:8000/docs，Streamlit 通常位于 http://localhost:8501。

| 接口 | 功能 |
|---|---|
| `POST /review` | 插件使用的审查接口，返回 `result` 对象 |
| `POST /optimize` | 插件使用的优化接口，返回 `result` 代码字符串 |
| `POST /agent/review` | 多 Agent 报告、覆盖度、裁决与角色轨迹 |
| `POST /agent/review/stream` | 带角色标记的 SSE 审查过程 |
| `POST /agent/fix` | 独立修复管线；默认只返回待确认计划 |
| `POST /agent/optimize` | 兼容入口：审查后执行修复 |
| `POST /agent/feedback` | 保存人工反馈 |
| `GET /agent/trace/{run_id}` | 获取轨迹 |
| `GET /agent/stats`、`GET /agent/memory` | 运行统计、分层记忆查询 |
| `POST /agent/memory/entity` | 新增或更新项目实体事实 |
| `DELETE /agent/memory/session/{session_id}` | 清除短期会话与当前感知 |

插件接口请求体为 `{"code":"待审查代码"}`。

## 多 Agent 主线

```text
基础上下文（语法、AST、项目记忆）
→ Review Agent 按需调用静态分析、符号和项目检索工具
→ 候选契约校验与去重
→ 条件式 Verification Agent 独立查证
→ 确定性裁决
→ 确定性裁决与 Final Report
→ 用户确认
→ Fix Pipeline
```

Review 与 Verification 使用独立上下文和只读工具白名单，工具结果按输入 hash 在请求内缓存。
高风险、低置信度及历史误报疑似候选触发复核；失败或预算未完成的候选保留为待人工复核，
相同引擎的缓存结果不会被重复计为新的独立证据。

工具权限按 capability profile 强制划分：Review 可运行静态扫描来发现候选；Verification
默认只使用语法、AST、符号与项目上下文工具寻找成立条件或反证，不重复完整扫描；Fix Agent
只能分析候选和沙箱自测，拿不到最终补丁工具；Harness 独占验证、diff、交付与失败回退。
`build_full_registry()` 仅供显式管理和调试入口，正式工作流使用最小工具并集。

公共证据同时包含顶层 import、符号摘要；配置 `AGENT_CODEBASE_ROOT` 后还会加入跨文件
BM25 检索结果。审查结果会获得持久化 `review_id`，`/agent/fix` 返回待确认计划及
`fix_id`，确认时可只提交 ID，服务重启后仍能恢复。

长期记忆默认保存在 SQLite。旧的同名 JSONL 会在首次加载时幂等迁移并保留原文件。
生产环境可以配置服务 API Key、请求限流，并通过 `AGENT_SANDBOX_BACKEND=docker`
启用无网络、只读文件系统和资源配额的容器验证。

## VS Code 插件

先启动 API 服务，再安装 Node.js/npm 并编译插件：

```powershell
cd vscode-plugin
npm install
npm run compile
```

在 VS Code 打开 `vscode-plugin` 目录，使用扩展开发宿主调试；也可通过
`code --extensionDevelopmentPath="C:\Projects\code_review\vscode-plugin"` 启动开发宿主。
插件设置 `aiReviewer.apiHost` 默认是 `http://127.0.0.1:8000`。

打开 Python 文件，通过右键菜单或命令面板执行：

- `AI 代码审查 (Review)`、`AI 代码审查 (选中代码)`：显示报告及问题标记，支持跳转、复制和保存 JSON。
- `AI 代码优化 (Optimize)`、`AI 代码优化 (选中代码)`：展示优化代码，确认后应用到文件或选区。

生成方案后若文件已修改，需要重新生成方案再应用。

## MCP 与 LangGraph 入口

```powershell
python mcp_server.py --list
python mcp_server.py --profile review
python -m agent.langgraph.main --file your_file.py --mock --question "审查这个文件"
```

MCP 使用标准输入输出传输 JSON-RPC；客户端配置 Python 解释器和 `mcp_server.py` 的绝对路径。
交互式 LangGraph 入口与正式管线都位于 `agent/`。

## 实验与数据

当前保留实验 1：基线工具、单次模型调用、完整 Agent 的问题识别对比。
`data/eval.json` 有 50 条合成样本：24 条单问题、12 条双问题、14 条无问题，
共 48 个标注问题。数据约束与限制见 [数据说明](data/README.md)。

```powershell
python data/build_eval_dataset.py
python experiments/exp1_recognition.py
python experiments/exp1_recognition.py --arms baseline
python experiments/exp1_recognition.py --mock --max-samples 3 --out-dir reports/smoke
```

正式输出为 `reports/exp1/report.json` 和 `summary.md`，包含识别指标、耗时、Token 和逐样本结果。

`reports/simulation/` 是人为构造的展示数据，不是实测结果。详见 [实验说明](experiments/README.md)。

## 验证与目录

```powershell
python test/test_agent.py
python test/test_langgraph_agent.py
python test/test_dataset_loader.py
python test/test_web_app.py
```

| 目录或文件 | 内容 |
|---|---|
| `agent/agents/` | Review / Verification 角色配置与提示词；不执行工具 |
| `agent/executors/` | Review、Verification、Fix 共用的模型调用、工具循环、预算、finalizer 重试和执行轨迹 |
| `agent/tools/` | AST、静态分析、检索、补丁、沙箱等原子能力与注册表 |
| `agent/adapters/` | MCP 等外部协议适配；工作流包装不会进入 Agent 工具白名单 |
| `agent/langgraph/` | Review → Verification 的状态、路由与确定性裁决 |
| `agent/pipelines/` | 面向 CLI / API 的审查与修复应用服务 |
| `core/` | 规则引擎与兼容实现 |
| `cli.py`、`api/`、`mcp_server.py` | 命令行、HTTP、MCP 入口 |
| `vscode-plugin/`、`web/` | 编辑器插件、Web 页面 |
| `data/`、`experiments/` | 评测集与实验 1 |
| `test/`、`examples/` | 测试与示例 |
| `reports/`、`traces/`、`memory/` | 报告、轨迹、长期记忆 |

## 分层记忆

审查记忆分为四层：当前源码作为感知记忆，仅在进程内暂存；请求传入相同
`session_id` 时，最近 12 条审查摘要作为短期记忆回注；人工反馈和项目约定作为长期
记忆写入 `memory/agent_memory.jsonl`；文件、组件等结构化事实作为实体记忆按稳定键更新。
感知与短期记忆都设有容量上限且不会写盘，实体与长期记忆按 `project` 隔离。
