# 代码审查 Agent

**项目背景**：随着大语言模型的发展，传统的代码审查工具（规则引擎 + 一次性提示词调用）已难以满足真实场景的需求：
同一段代码在静态分析工具里会报出大量误报，判断哪条成立需要结合上下文取证；修复建议是否可用，必须真的跑一遍才知道。
为此，本项目基于 **LangChain + LangGraph** 框架实现了一套多 Agent + WorkFlow 的智能代码审查助手，
聚焦于证据驱动的结论、多维度协作审查、以及敏感操作的人工确认。

**项目描述**：
围绕软件研发过程中的代码审查场景，数据涵盖源码文本、静态分析告警、AST 结构、代码库上下文等核心信息，
替代人工初审环节，通过交互的方式完成代码审查与修复。由 AI 大模型自主完成：
调用工具取证、按维度判定问题、给出可落地的修复建议、在修复代码时自主验证并交付 diff。

**项目实现**：
主要包括业务工具模块、审查对象与上下文准备模块、多 Agent 调度模块、状态管理模块。

1. **工具函数模块**：把项目原有的 14 个代码能力（规则引擎 / pylint / bandit 静态分析、AST 大纲与签名比对、代码库 BM25 检索、补丁生成与应用、受限沙箱执行、规则库检索）统一封装为 LangChain 工具；依据风险划分为「只读工具」与「敏感工具」，按审查维度绑定到不同子助手，使大模型能够动态触发工具调用。
2. **审查对象与上下文准备模块**：主助手一开始就通过节点 `load_target` 从请求参数 `config.configurable` 中获取待审查文件路径，读取源码并连同行号保存在 `State` 中；借助 `InMemorySaver()` 保存上下文，即使中途因人工确认而中断，也能通过 `thread_id` 恢复同一轮审查。
3. **多 Agent 调度模块**：把代码审查细分为五条子工作流，由一个主 Agent 居中调度。每个子工作流是一个独立的 Agent，负责各自的维度，分为：安全审查助手、缺陷审查助手、规范与坏味审查助手、性能审查助手、修复助手。每个子工作流包含五个节点：入口节点（`make_entry_node`）、决策节点（带大模型的节点）、只读工具节点、敏感工具节点、离开当前任务节点（所有子助手共用）；其中只有修复助手存在敏感工具节点，四个审查维度助手只持有只读工具。主工作流到子工作流的调度通过动态路由条件 `add_conditional_edges` 实现；子工作流回主工作流通过控制 `State` 中的 `update_dialog_stack` 管理。
4. **状态管理模块**：整个模块是 `ReviewState`，保存工作流中的 `messages`（历史消息列表）、`target`（审查对象）、`issues`（已收集的结论）、`reviewed_dimensions`（已完成的维度）以及工作流状态栈 `dialog_state`。

> 说明：本项目同时存在两套编排实现。
> `agent/` 是**手写 ReAct 循环**版本（自研 Agent Runtime，八类停止条件、文本 ReAct 降级、三级验证）；
> `review_assistant/` 是本文档介绍的 **LangGraph 图编排**版本，两者共用同一批工具、数据契约、
> 规则库与长期记忆，可以对照阅读。本文档聚焦后者。

---

# 1 创建工具

按不同助手创建所需的工具：有只需要读取代码与告警的**只读工具**，和会真实执行代码、改写代码产物的**敏感工具**。
后续在定义工作流时，调用敏感工具之前需要中断，让用户确认是否要继续执行；只读工具则不需要。

工具的类型划分不是凭经验标注的，而是由工具自身的 `mutating` 属性决定，适配层会把它原样带进 LangGraph，
工作流据此自动分流（见 1.8）。

## 1.1 安全审查助手工具

| 工具名 | 类型 | 作用 |
|---|---|---|
| run_static_analysis | 只读工具 | 运行规则引擎 / pylint / bandit，返回客观告警清单（安全维度建议 `engines="rules,bandit"`） |
| scan_dangerous_code | 只读工具 | 扫描危险调用（eval/exec/os.system/subprocess 等） |
| get_ast_outline | 只读工具 | 输出代码结构大纲，确认问题所在函数与作用域 |
| find_symbol | 只读工具 | 查找某个标识符的定义与引用行 |
| check_syntax | 只读工具 | 语法检查 |
| search_codebase | 只读工具 | 在代码库内检索同类写法（需配置 `--codebase-root`） |
| read_project_file | 只读工具 | 读取项目内文件（需配置 `--codebase-root`） |
| find_symbol_in_project | 只读工具 | 跨文件查找符号（需配置 `--codebase-root`） |
| lookup_rule | 只读工具 | 检索项目内置的审查规则库与项目约定 |
| CompleteOrEscalate | 控制工具 | 将对话控制权交还主助手 |

## 1.2 缺陷审查助手工具

| 工具名 | 类型 | 作用 |
|---|---|---|
| run_static_analysis | 只读工具 | 静态分析（缺陷维度建议 `engines="rules"`） |
| get_ast_outline / find_symbol / check_syntax | 只读工具 | 结构、作用域、语法取证 |
| search_codebase / read_project_file / find_symbol_in_project | 只读工具 | 代码库上下文（需配置 `--codebase-root`） |
| lookup_rule | 只读工具 | 规则库检索 |
| CompleteOrEscalate | 控制工具 | 交还主助手 |

## 1.3 规范与坏味审查助手工具

| 工具名 | 类型 | 作用 |
|---|---|---|
| run_static_analysis | 只读工具 | 静态分析（坏味维度建议 `engines="pylint"`） |
| get_ast_outline / find_symbol / check_syntax | 只读工具 | 结构、作用域、语法取证 |
| search_codebase / read_project_file / find_symbol_in_project | 只读工具 | 代码库上下文（需配置 `--codebase-root`） |
| lookup_rule | 只读工具 | 规则库检索 |
| CompleteOrEscalate | 控制工具 | 交还主助手 |

## 1.4 性能审查助手工具

| 工具名 | 类型 | 作用 |
|---|---|---|
| run_static_analysis | 只读工具 | 静态分析（性能维度建议 `engines="rules"`） |
| get_ast_outline / find_symbol / check_syntax | 只读工具 | 结构、作用域、语法取证 |
| search_codebase / read_project_file / find_symbol_in_project | 只读工具 | 代码库上下文（需配置 `--codebase-root`） |
| lookup_rule | 只读工具 | 规则库检索 |
| CompleteOrEscalate | 控制工具 | 交还主助手 |

> 四个维度助手的工具集刻意保持**极简且只读**：它们拿不到任何执行或改写代码的能力。
> 提示词可以被模型绕过，工具白名单不行——这是 Agent 安全设计里最便宜也最有效的一道闸门。

## 1.5 修复助手工具

| 工具名 | 类型 | 作用 |
|---|---|---|
| run_static_analysis | 只读工具 | 修复前后的告警对比 |
| get_ast_outline | 只读工具 | 确认要改的位置与作用域 |
| check_syntax | 只读工具 | 修复代码的语法校验 |
| check_signature | 只读工具 | 比对修复前后的公开接口，防止顺手改了函数签名 |
| create_patch | 只读工具 | 生成 unified diff，便于只评审改动部分 |
| scan_dangerous_code | 只读工具 | 修复代码的危险调用扫描 |
| **apply_patch** | **敏感工具** | 把补丁应用到代码上，返回应用后的完整代码 |
| **execute_python_code** | **敏感工具** | 在受限子进程中真实执行代码 |
| **run_tests** | **敏感工具** | 在受限子进程中运行验收测试 |
| search_codebase / read_project_file / find_symbol_in_project | 只读工具 | 代码库上下文（需配置 `--codebase-root`） |
| lookup_rule | 只读工具 | 规则库检索 |
| CompleteOrEscalate | 控制工具 | 交还主助手 |

## 1.6 主助手工具

| 工具名 | 类型 | 作用 |
|---|---|---|
| lookup_rule | 只读工具 | 检索审查规则库与项目约定 |
| search_codebase | 只读工具 | 代码库检索（需配置 `--codebase-root`） |
| read_project_file | 只读工具 | 读取项目内文件（需配置 `--codebase-root`） |
| find_symbol_in_project | 只读工具 | 跨文件查找符号（需配置 `--codebase-root`） |
| ToSecurityAssistant | 交接工具 | 把安全维度的取证与判定交给安全审查助手 |
| ToBugAssistant | 交接工具 | 把潜在缺陷交给缺陷审查助手 |
| ToSmellAssistant | 交接工具 | 把代码坏味与规范问题交给规范与坏味审查助手 |
| ToPerformanceAssistant | 交接工具 | 把性能问题交给性能审查助手 |
| ToFixAssistant | 交接工具 | 把「按已确认的问题清单修复代码」交给修复助手 |

主助手刻意**不给结论、不执行修复**，只有规范检索、代码库定位和交接三类能力。
这与「主管不亲自下场写代码，而是派活并汇总」的分工一致。

### 交接工具与 CompleteOrEscalate 的数据模型类

`handoffs.py` 用 Pydantic 类定义交接契约：

```python
class ToSecurityAssistant(BaseModel):
    """把安全类问题（注入、危险函数、硬编码密钥等）的取证与判定交给安全审查助手。"""

    request: str = Field(description="需要安全审查助手处理的具体任务")


class CompleteOrEscalate(BaseModel):
    """当前维度的任务已结束，或出现其他维度/业务需求时，交回主助手。"""

    reason: str = Field(description="完成情况或需要转交的原因")
```

**在定义中它们只是类，为什么可以当作工具？**

| 机制 | 说明 |
|---|---|
| 底层约定 | LangChain 把任何继承 `BaseModel` 的 Pydantic 类视为「工具签名」（Tool Signature） |
| 自动转换 | `bind_tools` 自动把类定义转换成 OpenAI 标准的 function calling 格式 |
| 字段即参数 | 类里的 `Field(description=...)` 会成为参数说明，模型据此决定该传什么 |
| 调用即路由 | 模型「调用」某个类，等价于把任务交接出去；LangGraph 侧由入口节点消费这次调用 |

用类而不是函数来定义交接工具的好处：

| 优势 | 说明 |
|---|---|
| 可复用性 | 类可以同时作为数据类型在别处使用（本项目用它做路由表的键） |
| 自描述性 | 参数说明、示例、约束都写在类里，模型看到的就是接口文档 |
| 扩展性 | 以后新增字段（如 priorities、file_hint）不影响既有逻辑 |

## 1.7 规则库检索工具

对应旅行助手的 `lookup_policy`。语料是内置的审查规则库（`core/prompts.py` 的 `REVIEW_RULES`）
加上通过 CLI 写入的项目约定记忆，用向量检索取最相关的几段：

```python
@tool
def lookup_rule(query: str) -> str:
    """检索项目内置的代码审查规则库与项目约定。

    在判定某个问题属于哪一类、或需要引用规则依据时使用。
    返回的是本地规则库与历史约定的片段，不代表最新行业标准。

    Args:
        query: 检索关键词，例如「除零 边界」「硬编码密钥」「嵌套循环」。
    """
    results = get_retriever().query(query, k=3)
    if not results:
        return "（规则库中未找到相关条目）"
    return "\n\n".join(
        f"[{item['title']}｜来源 {item['source']}｜相似度 {item['similarity']:.3f}]\n"
        f"{item['page_content']}"
        for item in results
    )
```

索引后端自动选择：配了 `EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL` 就走真实向量服务，
否则退化为 `HashingEmbedder`（词项哈希向量）+ 余弦相似度——完全离线、零成本、结果可复现。

## 1.8 工具适配器：把已有工具接进 LangGraph

项目原有的 14 个工具是自定义的 `Tool` 抽象（`name` / `description` / `parameters` / `run`），
而 LangGraph 的 `ToolNode` 需要 LangChain 的 `BaseTool`。`tool_adapter.py` 做一层薄适配，**原工具一行都不用改**：

```python
class RegistryTool(BaseTool):
    """LangChain 形态的 agent.tools.base.Tool。"""

    name: str
    description: str
    #: 原工具的副作用标记；工作流用它做「只读 / 需确认」分流
    mutating: bool = False
    args_schema: Type[BaseModel] = BaseModel

    _inner: Any = PrivateAttr()

    @classmethod
    def from_inner(cls, tool: Any) -> "RegistryTool":
        instance = cls(
            name=tool.name,
            description=tool.description,
            mutating=bool(getattr(tool, "mutating", False)),
            args_schema=build_args_schema(tool.name, getattr(tool, "parameters", {}) or {}),
        )
        instance._inner = tool
        return instance

    def _run(self, **kwargs: Any) -> str:
        result = self._inner(**kwargs)
        if getattr(result, "ok", False):
            return result.content or "（工具无输出）"
        return f"[工具执行失败] {getattr(result, 'error', '') or getattr(result, 'content', '')}"
```

其中 `build_args_schema` 在运行时把工具的 JSON Schema 转成 Pydantic 模型，交给 LangChain 做参数校验：

```python
def build_args_schema(tool_name: str, parameters: Dict[str, Any]) -> Type[BaseModel]:
    properties = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])

    fields: Dict[str, Any] = {}
    for key, spec in properties.items():
        spec = spec or {}
        annotation = _TYPE_MAP.get(str(spec.get("type") or "string"), str)
        description = str(spec.get("description") or "")
        if key in required:
            fields[key] = (annotation, Field(description=description))
        else:
            default = spec.get("default", None)
            fields[key] = (Optional[annotation], Field(default=default, description=description))
    ...
```

**这样设计的收益**：

1. 工具定义只有一处（`agent/tools/*`），手写版本和图编排版本共用，不会出现「两份实现走偏」。
2. 工具的 `mutating` 标记直接决定它在图里落到只读节点还是敏感节点，安全策略与工具定义同源。
3. 工具抛异常会被适配层转成可观察的文本，`ToolNode` 的 `handle_tool_errors` 再兜一层，模型看到的是错误说明而不是崩溃。

## 1.9 只读工具与敏感工具的划分

`toolset.py` 按角色装配工具集，并把敏感工具单独挑出来：

```python
READ_ONLY_NAMES = (
    "run_static_analysis",
    "get_ast_outline",
    "find_symbol",
    "check_syntax",
)

DIMENSION_EXTRA: Dict[str, Sequence[str]] = {
    "security_assistant": ("scan_dangerous_code",),
}

FIX_NAMES = (
    "run_static_analysis", "get_ast_outline", "check_syntax", "check_signature",
    "create_patch", "apply_patch", "execute_python_code", "run_tests", "scan_dangerous_code",
)

CODEBASE_NAMES = ("search_codebase", "read_project_file", "find_symbol_in_project")
```

```python
    fix_tools = _pick(converted, FIX_NAMES) + codebase + policy
    assistants["fix_assistant"] = fix_tools
    sensitive["fix_assistant"] = {
        tool.name
        for tool in fix_tools
        if getattr(tool, "mutating", False) or tool.name in SENSITIVE_TOOL_NAMES
    }
```

最终只有修复助手会产出敏感节点，编译时用它生成 `interrupt_before` 列表：

```python
    def sensitive_nodes(self) -> List[str]:
        """需要 ``interrupt_before`` 的节点名。"""
        return [
            f"{name}_sensitive_tools"
            for name, names in self.sensitive.items()
            if names
        ]
```

## 1.10 带错误回退机制的工具节点

工具正常调用则返回正常的执行结果；执行失败时，由 `ToolNode` 的 `handle_tool_errors` 把异常转成提示文本回填给模型，
而不是让整张图崩掉：

```python
        builder.add_node(
            safe_node,
            ToolNode(safe_tools, handle_tool_errors="查询失败，不能编造结果。"),
        )

        builder.add_node(
            sensitive_node,
            ToolNode(
                sensitive_tools,
                handle_tool_errors=(
                    "工具执行失败，请检查实际结果，不能声称操作成功。"
                ),
            ),
        )
```

这里的措辞是刻意写的：错误提示不只是报错，还要**阻断模型的下一步幻觉**
（「不能编造结果」「不能声称操作成功」）。

## 1.11 运行事件的打印函数

图的每个节点都会往 `state["events"]` 追加一条事件，便于观察整轮的节点时序；
交互模式下另有 `_log` 负责把「当前是哪个助手、请求了什么工具」实时打出来：

```python
def _log(message: str) -> None:
    if VERBOSE:
        print(message)


def make_assistant(llm, tools, role_prompt, node_name):
    ...
    def call_model(state: ReviewState) -> Dict[str, Any]:
        system_content = build_system_content(...)
        _log(f"\n[助手：{label}]")
        _log(f"[状态栈] {state.get('dialog_state') or []}")

        response = model.invoke([SystemMessage(content=system_content), *(state.get("messages") or [])])
        calls = getattr(response, "tool_calls", None) or []
        for call in calls:
            _log(
                "[工具请求] "
                f"{call.get('name')} {json.dumps(call.get('args') or {}, ensure_ascii=False)}"
            )
        if not calls:
            _log(f"[回复] {str(getattr(response, 'content', '') or '')[:200]}")

        return {
            "messages": [response],
            "events": [
                {
                    "type": "assistant",
                    "node": node_name,
                    "tool_calls": [call.get("name") for call in calls],
                }
            ],
        }

    return call_model
```

---

# 2 定义状态 State

状态定义首先是 `messages`，使用 LangGraph 框架的 `add_messages` 自动追加最新消息；
接着根据项目添加特定字段：

- `target`：审查对象。在工作流的准备节点 `load_target` 中读取文件并填充，后续所有助手都能直接取用，不用重复读盘。
- `issues`：各维度子助手收集到的结论。使用自定义 reducer `merge_issues` 做**跨助手去重合并**。
- `reviewed_dimensions`：已完成的助手，注入主助手提示词，让它知道自己走到哪一步。
- `fix`：修复助手产出的修复结果。
- `report`：汇总后的结构化报告。
- `dialog_state`：多 Agent 场景下记录当前所处 agent，用自定义函数 `update_dialog_stack` 控制更新逻辑。
- `events`：简易事件流，`operator.add` 累加。

```python
class ReviewState(TypedDict, total=False):
    """图状态。"""

    messages: Annotated[List[AnyMessage], add_messages]
    dialog_state: Annotated[List[str], update_dialog_stack]
    #: 本轮审查对象
    target: Dict[str, Any]
    #: 各维度收集到的结论（dict 形态）
    issues: Annotated[List[Dict[str, Any]], merge_issues]
    #: 已完成的子助手（含 fix_assistant）
    reviewed_dimensions: Annotated[List[str], merge_dimensions]
    #: 修复结果
    fix: Dict[str, Any]
    #: 最终结构化报告
    report: Dict[str, Any]
    #: 简易事件流（每次工具调用/收集动作一条），便于 CLI 与测试观察
    events: Annotated[List[Dict[str, Any]], operator.add]
```

为什么 `issues` 里存的是纯 dict 而不是 `Issue` 对象？
状态需要能被 checkpoint 与人工确认流程原样保存，保持 JSON 可序列化最省事；
进入校验环节时再用 `Issue.model_validate` 还原成强类型对象。

## 2.1 状态栈的更新函数

```python
def update_dialog_stack(current: List[str], update: Optional[str]) -> List[str]:
    """对话栈的 reducer：``None`` 不变，``"pop"`` 出栈，其余值入栈。"""
    if update is None:
        return current
    if update == "pop":
        return current[:-1]
    return current + [update]
```

Python 的 list 天然具备栈的特性（`append` 入栈、`[:-1]` 出栈），
栈顶元素就是当前该说话的 agent。

## 2.2 结论合并的 reducer

```python
def merge_issues(current: List[Dict[str, Any]], update: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """问题合并 reducer：按 (分类, 二级分类, 起始行) 去重，保留置信度更高的一条。

    这是「多个子助手先后写入同一状态字段」的关键：没有 reducer 时后写入的会
    覆盖先写入的，多维度审查就只能留下最后一个维度的结论。
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    for item in list(current) + list(update):
        if not isinstance(item, dict):
            continue
        key = (item.get("category"), item.get("subtype"), item.get("line_start"))
        previous = merged.get(key)
        if previous is None or float(item.get("confidence") or 0) > float(previous.get("confidence") or 0):
            merged[key] = item
    return list(merged.values())
```

**为什么需要它？**
LangGraph 的每个节点返回的是「状态的增量更新」。如果某个字段没有 reducer，
默认行为是**覆盖**：安全助手写入 3 条结论、缺陷助手接着写入 2 条，最后状态里就只剩 2 条。
加上 reducer 之后，多个子助手对同一字段的写入会按我们定义的规则合并，
这就是多智能体「共享记忆」在 LangGraph 里的标准做法。

## 2.3 维度去重的 reducer

```python
def merge_dimensions(current: List[str], update: List[str]) -> List[str]:
    """已完成维度的 reducer：只追加没出现过的。"""
    result = list(current)
    for name in update:
        if name not in result:
            result.append(name)
    return result
```

---

# 3 定义子 agent

分别创建每个子 agent，以安全审查助手为例。

## 3.1 prompt

子助手的系统消息由 `build_system_content` 统一组装：**公共约束 + 角色提示词 + 运行态上下文**。
除了基本提示语句外，还会动态注入四块内容：

1. **审查对象**：文件路径与带行号的源码（左列为行号），保证模型引用的行号真实存在
2. **已收集的问题**：其他维度已确认的结论，避免各审各的、重复报同一处
3. **审查维度进度**：仅注入主助手，让它知道哪些维度已完成、还要继续委派什么
4. **规则库**：项目内置的审查规则与二级分类白名单

```python
def build_system_content(
    node: str,
    role_prompt: str,
    target: Dict[str, Any],
    issues: List[Dict[str, Any]],
    reviewed: List[str],
    fix: Dict[str, Any],
) -> str:
    """组装系统消息：公共约束 + 角色提示词 + 运行态上下文。

    与旅行助手把 ``user_info`` 注入每个助手是同一手法：把「当前事实」放进系统消息，
    模型每轮都能看到最新状态，不需要自己去翻聊天记录。
    """
    path = target.get("path") or "（未指定文件）"
    code = target.get("code") or ""

    blocks = [
        COMMON_PROMPT,
        role_prompt,
        f"{NODE_MARKER}{node}",
        f"当前时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"审查对象：{path}（共 {target.get('line_count') or len(code.split(chr(10)))} 行）",
    ]

    if node == PRIMARY_ASSISTANT:
        blocks.append(_progress_line(reviewed))
    elif node in ASSISTANT_CATEGORY:
        blocks.append(f"已收集的问题（含其他维度）：\n{_issues_block(issues)}")
    elif node == FIX_ASSISTANT:
        blocks.append(f"【已确认问题清单】\n{_issues_block(issues)}")
        if fix.get("fixed_code"):
            blocks.append("上一轮修复结果已存在，如需重试请说明原因。")

    if REVIEW_RULES:
        blocks.append(REVIEW_RULES)

    blocks.append(f"{CODE_MARKER}\n```python\n{numbered_code(code)}\n```")
    return "\n\n".join(blocks)
```

其中「审查维度进度」这一行是让主助手能收敛的关键，格式固定、便于解析：

```python
def _progress_line(reviewed: List[str]) -> str:
    # 「已完成」要把修复助手也算进去：否则主助手无法知道修复已经做过，
    # 会在「委派修复 → 收到结果 → 再委派修复」之间来回空转。
    done = [
        ASSISTANT_LABEL[name]
        for name in [*DIMENSION_ASSISTANTS, FIX_ASSISTANT]
        if name in reviewed
    ]
    pending = [ASSISTANT_LABEL[name] for name in DIMENSION_ASSISTANTS if name not in reviewed]
    return (
        f"{PROGRESS_MARKER}已完成：{'、'.join(done) or '（无）'}；"
        f"未完成：{'、'.join(pending) or '（无）'}"
    )
```

## 3.2 Tools

分别取只读工具集合与敏感工具集合（见第 1 节），按维度绑定；
子助手在绑定工具时额外绑定 `CompleteOrEscalate`，作用：
在子助手识别到无法完成需求、或本维度已结束时调用，把控制权交回主助手。

维度助手还会拿到自己维度的**输出契约**，包含允许的二级分类白名单与 JSON 结构：

```python
def dimension_prompt(assistant: str) -> str:
    """拼装某个维度子助手的完整系统提示词。"""
    category = ASSISTANT_CATEGORY[assistant]
    label = ASSISTANT_LABEL[assistant]
    engines = DIMENSION_ENGINES.get(assistant, "rules")
    return f"""
你是{label}，{_DIMENSION_FOCUS[assistant]}

取证建议：
- 先用 run_static_analysis 拿到客观告警（建议引擎：{engines}）。
  静态分析**必然包含误报**，必须逐条结合代码语义判断哪些成立、哪些应当排除。
...
{findings_schema_hint(category)}
"""
```

## 3.3 构建子 agent

把「角色提示词 + 大模型 + 工具调用能力」组合成一个可执行的节点函数。
`make_assistant` 是一个**闭包工厂**：调用一次产出一个节点函数，函数自带该助手的提示词与工具集。

```python
def make_assistant(
    llm: Any,
    tools: Sequence[Any],
    role_prompt: str,
    node_name: str,
) -> Callable[..., Dict[str, Any]]:
    """造一个助手节点：组装系统消息 → 调模型 → 把回复写回消息列表。"""
    model = llm.bind_tools(list(tools)) if tools else llm
    label = ASSISTANT_LABEL.get(node_name, node_name)

    def call_model(state: ReviewState) -> Dict[str, Any]:
        system_content = build_system_content(...)
        response = model.invoke([SystemMessage(content=system_content), *(state.get("messages") or [])])
        ...
        return {"messages": [response], "events": [...]}

    return call_model
```

**与传统独立 Agent 的差异**：

| 特性 | 本项目的子 agent | 传统独立 Agent |
|---|---|---|
| 创建方式 | 闭包工厂 + `bind_tools` 组合 | 通常继承 Agent 基类 |
| 工具范围 | 严格受限（只绑定本维度工具） | 可能访问全局工具库 |
| 状态管理 | 依赖父图（`StateGraph`）传递状态 | 自主管理状态 |
| 生命周期 | 由主助手按需激活，入口/退出节点管理 | 独立运行 |
| 结论落库 | 必须过 `collect_*` 节点的证据校验 | 自己说了算 |

---

# 4 定义子工作流

以安全审查助手的子工作流为例。

## 4.1 定义入口节点函数

每一个子工作流都需要一个入口节点。

**为什么需要入口节点？**

1. 模型调用 `ToSecurityAssistant` 只是「发起了一次工具调用」；如果没有入口节点把它转成一条真实的 `ToolMessage`，
   下一条消息就是 `tool_calls` 无人应答的悬空状态，多数模型网关会直接报错。
2. 入口节点同时把子助手压入对话栈，明确标记当前由谁说话，后续 `load_target → current_assistant` 才能正确路由。
3. 让上下文过渡平滑：交接说明以工具结果的形式出现，模型看到的是「任务已交接」，而不是一段突兀的对话。

**入口节点函数为什么要用闭包实现？**

| 理由 | 说明 |
|---|---|
| 保持状态 | 闭包携带 `assistant_name`，这个值在 `make_entry_node` 被调用时就固定下来 |
| 封装复用 | 把「生成交接消息 + 设置对话状态」封装成一个可复用的行为，5 个子助手共用一份逻辑 |
| 延迟执行 | 定义行为与执行行为分离，节点在运行时才被 LangGraph 调用 |

代码如下：

```python
def make_entry_node(assistant_name: str) -> Callable[[ReviewState], Dict[str, Any]]:
    """交接落地节点：把 handoff 工具调用变成一条 ToolMessage，并把助手压入对话栈。"""
    label = ASSISTANT_LABEL[assistant_name]

    def enter(state: ReviewState) -> Dict[str, Any]:
        message = _last_message(state)
        call = (getattr(message, "tool_calls", None) or [{}])[0]
        request = (call.get("args") or {}).get("request", "")
        return {
            "messages": [
                ToolMessage(
                    content=f"已转交给{label}。任务：{request}",
                    tool_call_id=call.get("id") or "",
                )
            ],
            "dialog_state": assistant_name,
        }

    return enter
```

> 关于 `Callable`：它描述任何支持 `()` 调用操作的对象，包括普通函数、lambda、
> 类（调用类会创建实例）以及实现了 `__call__` 的对象。这里返回的是内部定义的普通函数。

## 4.2 定义退出节点函数

所有子工作流共用一个退出节点 `leave_skill`。

**触发条件**：当子助手调用 `CompleteOrEscalate` 时，路由函数把它映射到 `leave_skill`，状态机随即跳转到该节点。

**为什么需要退出节点？**

1. 维护对话栈：若直接从子助手跳回主助手，栈顶仍然停留在子助手上，后续路由会一直把用户的话转给子助手。
2. 保留交接说明：把「本维度结论如何、还有什么没做」以工具结果的形式交回主助手，供它决定下一步。

**为什么所有子助手共用？** 因为退出行为完全一致（出栈 + 交回说明），
节点在 LangGraph 中是全局可复用的，注册一次即可，所有子助手的路由函数都能返回同一个节点名。

```python
def leave_skill(state: ReviewState) -> Dict[str, Any]:
    """子助手主动交回主助手（对应旅行助手的 ``leave_skill``）。"""
    message = _last_message(state)
    call = (getattr(message, "tool_calls", None) or [{}])[0]
    reason = (call.get("args") or {}).get("reason", "")
    label = ASSISTANT_LABEL.get(current_assistant(state), "子助手")
    return {
        "messages": [
            ToolMessage(
                content=f"{label}已交回主助手。说明：{reason}",
                tool_call_id=call.get("id") or "",
            )
        ],
        "dialog_state": "pop",
    }
```

## 4.3 定义收集节点（本项目相对旅行助手最关键的改动）

旅行助手的子助手返回的是**工具执行结果**（订单建好了、航班改了）；
代码审查的子助手返回的是**结论**（我认为第 12 行有除零风险）。
结论是模型生成的，天然可能编造，所以中间必须插一道校验闸门——这就是收集节点。

子助手输出无工具调用的最终文本后，路由把它送到 `collect_<assistant>`，
该节点解析结论、逐条校验、合并进状态，然后把对话栈出栈、回到主助手：

```python
def make_collect_node(assistant_name: str) -> Callable[[ReviewState], Dict[str, Any]]:
    """子助手给出最终文本后的落库节点：解析 → 校验 → 合并 → 回主助手。"""
    category = ASSISTANT_CATEGORY[assistant_name]
    label = ASSISTANT_LABEL[assistant_name]

    def collect(state: ReviewState) -> Dict[str, Any]:
        text = _last_text(state)
        target = state.get("target") or {}
        total_lines = int(target.get("line_count") or 1)
        called = _tools_called_since_entry(state.get("messages") or [])

        payload, error = parse_json_object(text)
        kept: List[Dict[str, Any]] = []
        notes: List[str] = []

        if payload is None:
            notes.append(f"未产出结构化结论（{error}）")
        else:
            raw_issues = payload.get("issues")
            ...
            kept, notes = validate_issues(raw_issues, category, total_lines, called)
        ...
        return {
            "issues": kept,
            "reviewed_dimensions": [assistant_name],
            "dialog_state": "pop",
            "events": [...],
        }

    return collect
```

校验规则写在 `findings.py`（原项目「证据闭环」的落点）：

```python
def validate_issues(
    raw_issues: Sequence[Any],
    expected_category: Category,
    total_lines: int,
    called_tools: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """校验并归一化一个维度助手提交的结论。"""
```

| 规则 | 处理方式 |
|---|---|
| 字段过不了 `Issue` 契约 | 丢弃，记录原因 |
| 分类越界（安全助手报了个性能问题） | 强制归位到本维度分类，并记录说明 |
| 行号超出源码范围 | 丢弃（首行越界的直接丢，尾行越界的裁剪到末行） |
| 没有 `evidence` | **丢弃**（不是降权，是丢弃） |
| 声称证据来自某工具，但该工具本轮没被调用过 | 保留但标记未核实、置信度压到 0.5 以下 |
| `subtype` 不在该分类的白名单内 | 保留但标记，提示该结论的可信度 |

其中「本轮到底调用了哪些工具」由 `_tools_called_since_entry` 统计——
从消息尾部往前扫，遇到入口节点写入的交接消息就停：

```python
def _tools_called_since_entry(messages: Sequence[BaseMessage]) -> Set[str]:
    """统计「最近一次交接之后」被调用过的工具名。"""
    names: Set[str] = set()
    for message in reversed(list(messages)):
        if isinstance(message, ToolMessage) and str(message.content or "").startswith("已转交"):
            break
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                names.add(str(name))
    return names
```

> 效果：`「第 0 条证据不足：subtype 'possibly-used-before-assignment' 不在 Potential Bug 的允许集合内」`
> 这类校验说明会写进事件流，最终报告里也会保留，评审者一眼能看出哪些结论是硬的、哪些是软的。

修复助手用的是另一个收集节点 `collect_fix_assistant`，它做的是**harness 侧强制校验**：
修复代码必须能通过 `ast.parse`，且公开接口不变——
**验证由代码强制，而不是依赖模型自觉调用工具**：

```python
            ok = True
            try:
                ast.parse(fixed_code)
                fix["checks"].append({"name": "syntax", "passed": True, "detail": "ast.parse 通过"})
            except SyntaxError as exc:
                ok = False
                fix["checks"].append(
                    {"name": "syntax", "passed": False, "detail": f"第 {exc.lineno} 行语法错误：{exc.msg}"}
                )

            registry = getattr(toolset, "registry", None)
            signature_tool = registry.get("check_signature") if registry is not None else None
            if signature_tool is not None and fixed_code:
                result = signature_tool(original_code=original, modified_code=fixed_code)
                ...
                ok = ok and compatible
```

注意这里只做**无副作用**的语法与接口校验；真正的沙箱执行（`execute_python_code` / `run_tests`）
必须走敏感节点，由用户确认后才能跑——人工确认的闸门不会被 harness 绕过。

## 4.4 构建子工作流

以安全审查助手为例，子工作流的构建步骤：

1. 添加入口节点 `enter_security_assistant`
2. 添加决策节点 `security_assistant`，用 `make_assistant` 封装
3. 连接入口节点到决策节点（非条件边，直接 `add_edge`）
4. 分别添加只读工具节点与敏感工具节点（敏感工具节点仅在存在敏感工具时创建）
5. 创建只读/敏感工具节点回到决策节点的边
6. 添加收集节点 `collect_security_assistant`
7. 添加条件边，由决策节点路由到：只读工具节点、敏感工具节点、离开节点 `leave_skill`、收集节点
8. 注册共用的 `leave_skill` 与 `reject_tool_calls`（后续子工作流不需要重复注册）
9. 添加 `collect_* → 主助手`、`leave_skill → 主助手` 的边

对应代码（`build_graph` 中的循环体，5 个子助手共用这段逻辑）：

```python
    for name, tools in toolset.assistants.items():
        entry_name = f"enter_{name}"
        safe_node = f"{name}_safe_tools"
        sensitive_node = f"{name}_sensitive_tools"
        collect_node = f"collect_{name}"
        sensitive_names = toolset.sensitive.get(name) or set()

        safe_tools = [tool for tool in tools if tool.name not in sensitive_names]
        sensitive_tools = [tool for tool in tools if tool.name in sensitive_names]

        builder.add_node(
            name,
            make_assistant(
                llm,
                [*tools, CompleteOrEscalate],
                dimension_prompt(name) if name in ASSISTANT_CATEGORY else fix_prompt(),
                name,
            ),
        )
        builder.add_node(entry_name, make_entry_node(name))
        builder.add_node(
            collect_node,
            make_collect_node(name) if name in ASSISTANT_CATEGORY else make_collect_fix_node(toolset),
        )
        builder.add_edge(entry_name, name)
        builder.add_edge(collect_node, PRIMARY_ASSISTANT)
        ...
        builder.add_conditional_edges(
            name,
            make_router({"CompleteOrEscalate": "leave_skill"}, routes, collect_node),
            targets,
        )
```

好处是显见的：新增一个审查维度，只需要在 `dimensions.py` 里加一行映射、
在 `toolset.py` 里配一组工具，图的结构由这段循环自动长出来，不必手写五遍。

---

# 5 定义主 agent

## 5.1 prompt

prompt 逻辑和子 agent 一致，区别在于主助手看到的是**维度进度**而不是「已收集的问题」：

```python
PRIMARY_PROMPT = """
你是代码审查主助手。

你手里只有三类能力：
1. lookup_rule：检索项目内置的审查规则库与项目约定。
2. 代码库检索工具（search_codebase / read_project_file / find_symbol_in_project）：
   用于定位代码、确认调用关系。若工具列表里没有，说明未配置代码库根目录。
3. 交接工具：把具体维度的取证与判定交给对应子助手。
...
调度方式：
- 一次请求通常要覆盖多个维度：**每次只委派一个维度**，等该助手返回后再委派下一个。
- 系统会告诉你已完成的维度与尚未完成的维度，不要重复委派已完成的维度。
- 已完成的维度若结论为空，说明该维度确实没有发现问题，不必重试。
- 全部需要的维度完成后，**直接输出一份中文审查总结**（发现数量、按严重度排序的要点、
  修复建议概述），此时不要再调用任何工具。
"""
```

## 5.2 Tools

主助手只绑定 `lookup_rule`、代码库检索工具（如果有），以及 5 个交接模型类；
代码库类工具不存在时也不影响——它只是少了一种定位手段。

## 5.3 构建主 agent

与子 agent 不同的是，主 agent 不绑定 `CompleteOrEscalate`，而是绑定各个子 agent 的数据模型类，
用于由主 agent 向各子 agent 路由时判断：

```python
    builder.add_node(
        PRIMARY_ASSISTANT,
        make_assistant(
            llm,
            [*toolset.primary, *HANDOFF_MODELS],
            PRIMARY_PROMPT,
            PRIMARY_ASSISTANT,
        ),
    )
```

## 5.4 数据模型类

数据模型类定义见 1.6 节（`handoffs.py`）。
主助手的绑定关系可以概括成一句话：**`bind_tools` 把 Pydantic 模型类转化为 LLM 可调用的工具，
扩展了主助手的「派活」能力边界。**

## 5.5 主助手的路由函数

与旅行助手的差异在于：本项目的路由需要识别**五类交接**，并且要防止
「所有维度都做完了，主助手还在继续派活」这种空转。为此加了一个 `all_done_hint` 提示节点，
并在连续两次提示后直接收口：

```python
def make_primary_router(business_routes: Dict[str, str]) -> Callable[[ReviewState], str]:
    controls = {
        model.__name__: f"enter_{HANDOFF_TARGET[model.__name__]}" for model in HANDOFF_MODELS
    }
    inner = make_router(controls, business_routes, "finalize_report")

    def route(state: ReviewState) -> str:
        calls = getattr(_last_message(state), "tool_calls", None) or []
        if len(calls) == 1:
            target = HANDOFF_TARGET.get(str(calls[0].get("name") or ""))
            reviewed = state.get("reviewed_dimensions") or []
            hints = sum(1 for event in (state.get("events") or []) if event.get("type") == "all_done_hint")
            if (
                target in DIMENSION_ASSISTANTS
                and all(name in reviewed for name in DIMENSION_ASSISTANTS)
                and hints < 2
            ):
                return "all_done_hint"
        return inner(state)

    return route
```

---

# 6 定义最终的工作流

首先定义流程图的构建对象：

```python
builder = StateGraph(ReviewState)
```

## 6.1 定义审查对象的准备节点

定义函数 `load_target` 读取待审查文件，并把源码与行数写入 `target`。
该节点作为工作流的起点，每次对话开始前先执行；后续所有 agent 都能直接在 `target` 中取到源码，
不需要重复读盘：

```python
def load_target(state: ReviewState, config: RunnableConfig) -> Dict[str, Any]:
    """确认审查对象（对应旅行助手的 ``fetch_user_info``）。

    审查对象在第一次运行时从 ``config.configurable["target_path"]`` 读取并缓存进状态；
    之后每轮工具调用回到这里时直接跳过，避免重复读盘。
    """
    target = state.get("target") or {}
    if target.get("code"):
        return {}

    configurable = (config or {}).get("configurable", {}) or {}
    path = str(configurable.get("target_path") or "").strip()
    if not path:
        raise ValueError(...)

    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"待审查文件不存在：{file_path}")

    code = file_path.read_text(encoding="utf-8", errors="replace")
    line_count = max(1, len(code.split("\n")))
    return {
        "target": {"path": str(file_path), "code": code, "line_count": line_count},
        "events": [{"type": "load_target", "path": str(file_path), "lines": line_count}],
    }
```

```python
builder.add_node("load_target", load_target)
builder.add_edge(START, "load_target")
```

## 6.2 在工作流中添加子工作流

调用构建子工作流的循环，把 5 个子助手的工作流添加到工作流中：

```python
    for name, tools in toolset.assistants.items():
        ...  # 见 4.4 节
```

## 6.3 添加主 agent

1. 添加定义好的主 agent 节点
2. 添加主助手工具的节点
3. 添加主助手到各子助手入口节点的条件边
4. 添加主助手工具返回的边

```python
    handoff_routes = {
        model.__name__: f"enter_{HANDOFF_TARGET[model.__name__]}" for model in HANDOFF_MODELS
    }
    primary_business = {tool.name: "primary_tools" for tool in toolset.primary}

    builder.add_conditional_edges(
        PRIMARY_ASSISTANT,
        make_primary_router(primary_business),
        [
            *handoff_routes.values(),
            "primary_tools",
            "all_done_hint",
            "reject_tool_calls",
            "finalize_report",
        ],
    )
    builder.add_edge("primary_tools", "load_target")
```

## 6.4 定义从准备节点到下一节点的边

用户提问后，工作流首先执行 `load_target` 获取审查对象，接下来该去哪个节点由对话栈决定：

```python
def current_assistant(state: ReviewState) -> str:
    """对话栈栈顶即当前该说话的助手。"""
    stack = state.get("dialog_state") or []
    return stack[-1] if stack else PRIMARY_ASSISTANT
```

```python
    assistant_names = [PRIMARY_ASSISTANT, *toolset.assistants.keys()]
    builder.add_conditional_edges("load_target", current_assistant, assistant_names)
```

| 需求 | 实现方式 | 解决的问题 |
|---|---|---|
| 上下文保持 | 用 `dialog_state` 栈跟踪活跃的工作流 | 避免用户的多轮追问被错误路由 |
| 无缝回落 | 栈为空时返回主助手 | 防止系统卡死在无效状态 |
| 动态扩展性 | 支持任意数量的子助手 | 新增维度无需修改路由逻辑 |

## 6.5 只读工具与敏感工具的分流

路由函数统一处理「控制工具 / 业务工具 / 无工具调用」三种情况，
并且**在代码层面保证一次只执行一个工具调用**：

```python
def make_router(
    control_routes: Dict[str, str],
    business_routes: Dict[str, str],
    fallback: str,
) -> Callable[[ReviewState], str]:
    """通用路由：控制工具 → 专用节点；业务工具 → 工具节点；无调用 → fallback。"""

    def route(state: ReviewState) -> str:
        calls = getattr(_last_message(state), "tool_calls", None) or []
        if not calls:
            return fallback
        # 代码层面保证一次只执行一个请求
        if len(calls) != 1:
            return "reject_tool_calls"
        name = str(calls[0].get("name") or "")
        if name in control_routes:
            return control_routes[name]
        return business_routes.get(name, "reject_tool_calls")

    return route
```

被拒绝的工具调用会收到一条明确的说明，而不是静默失败：

```python
def reject_tool_calls(state: ReviewState) -> Dict[str, Any]:
    """一次想调多个工具时全部拒绝（与旅行助手同样的代码层面兜底）。"""
    message = _last_message(state)
    calls = getattr(message, "tool_calls", None) or []
    return {
        "messages": [
            ToolMessage(
                content="本次没有执行任何工具。请每次只调用一个当前助手允许的工具。",
                tool_call_id=call.get("id") or "",
            )
            for call in calls
        ]
    }
```

## 6.6 汇总节点

主助手输出最终总结文本后，路由把它送到 `finalize_report`：
去重、可选的长期记忆压制（复用原项目的误报记忆）、产出结构化报告。

```python
        report = ReviewReport(review_summary=_last_text(state), issues=issues).dedupe()

        suppressed: List[Dict[str, Any]] = []
        configurable = (config or {}).get("configurable", {}) or {}
        if configurable.get("use_memory", True) and report.issues:
            try:
                from agent.memory import MemoryStore

                store = MemoryStore()
                kept, suppressed = store.apply_suppression(
                    str(configurable.get("project") or "default"),
                    report.issues,
                    (state.get("target") or {}).get("code") or "",
                )
                report = ReviewReport(review_summary=report.review_summary, issues=kept)
            except Exception as exc:
                _log(f"[汇总] 记忆压制跳过：{exc}")
```

## 6.7 保存对话并设置中断

创建一个内存检查点（checkpointer）用于保存对话状态：

- `InMemorySaver` 是轻量级的状态存储器，以 `thread_id` 为键保存每次对话的状态快照
- 支持对话中断后恢复：即使因为敏感操作确认而暂停，也能带着完整上下文继续

编译工作流时，对于敏感工具需要在调用前向用户确认，因此添加中断：
`interrupt_before` 让工作流在这些节点执行前暂停。

```python
    return builder.compile(
        checkpointer=InMemorySaver(),
        interrupt_before=toolset.sensitive_nodes(),
    )
```

本项目这一处只有一个敏感节点：`['fix_assistant_sensitive_tools']`。

因为四个审查助手只持有只读工具，按 1.9 节的划分逻辑它们不会产生敏感节点。

---

# 7 执行工作流

## 7.1 主循环与敏感操作确认

执行流程：

1. 检查用户输入，如果是 `q` / `exit` / `quit` 则结束对话
2. `graph.invoke()` 把用户输入注入系统，推动状态机前进；首次执行会一路跑到敏感节点或终点
3. 通过 `graph.get_state(config).next` 获取当前待执行的节点：
   - 为空表示工作流已正常结束，无需中断
   - 否则说明停在敏感工具节点上，需要询问用户是否批准
4. 用户批准后继续执行工作流，此时**不需要重复传入用户输入**（`invoke(None)`）
5. 用户拒绝时，构造一条拒绝的 `ToolMessage` 注入状态，让模型知道工具没有执行，然后继续执行后续逻辑

```python
def run_turn(
    graph: Any,
    question: str,
    config: Dict[str, Any],
    approver: Approver = ask_user,
) -> Dict[str, Any]:
    """跑一轮完整对话，遇到敏感工具就停下来问用户，直到图执行结束。"""
    graph.invoke({"messages": [HumanMessage(content=question)]}, config=config)

    while True:
        snapshot = graph.get_state(config)
        pending = list(snapshot.next or ())

        if not pending:
            break
        ...
        call = calls[0]
        reason = approver(str(call.get("name")), call.get("args") or {})

        if reason is not None:
            rejection = ToolMessage(
                content=json.dumps(
                    {
                        "status": "rejected",
                        "message": "用户拒绝执行，工具未运行",
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=call.get("id") or "",
            )
            graph.update_state(config, {"messages": [rejection]}, as_node=node)

        graph.invoke(None, config=config)
    ...
```

终端里用户看到的内容：

```
待确认操作：
工具： execute_python_code
参数： {
    "code": "...",
    "timeout": 5,
    "strict": false
}
批准只表示允许执行，是否成功要看工具返回结果。
输入 y 执行；其它输入表示拒绝，也可填写拒绝原因：
```

## 7.2 一次完整运行的节点时序

离线运行 `--file examples/review_target.py --question "请审查这个文件，然后修复发现的问题"`，
事件流（`result["events"]`）如下：

```
{'type': 'load_target', 'path': 'test\\test_review.py', 'lines': 11}
{'type': 'assistant', 'node': 'primary_assistant', 'tool_calls': ['ToFixAssistant']}
{'type': 'assistant', 'node': 'fix_assistant', 'tool_calls': ['check_syntax']}
{'type': 'assistant', 'node': 'fix_assistant', 'tool_calls': ['execute_python_code']}
{'type': 'assistant', 'node': 'fix_assistant', 'tool_calls': []}
{'type': 'collect_fix', 'verified': True}
{'type': 'assistant', 'node': 'primary_assistant', 'tool_calls': ['ToSecurityAssistant']}
{'type': 'assistant', 'node': 'security_assistant', 'tool_calls': ['run_static_analysis']}
{'type': 'assistant', 'node': 'security_assistant', 'tool_calls': []}
{'type': 'collect', 'node': 'security_assistant', 'kept': 1, 'notes': []}
{'type': 'assistant', 'node': 'primary_assistant', 'tool_calls': ['ToBugAssistant']}
...
{'type': 'assistant', 'node': 'primary_assistant', 'tool_calls': []}
{'type': 'finalize', 'issues': 4}
```

可以清楚看到：主助手派活 → 子助手取证 → 子助手给结论 → 收集校验 → 回到主助手 → 下一个维度，
直到四个维度走完，主助手给出总结，汇总节点产出报告。

## 7.3 离线 mock 模型

没有 API Key、也不想消耗额度时，仍然需要能把整张图跑通。
`mock_llm.py` 实现了一个 LangChain 的 `BaseChatModel`，它**按节点逐步模拟一次真实的行为轨迹**
（而不是直接返回一份假报告），因此能覆盖路由、状态合并、证据校验、中断恢复等分支：

```python
def _primary(self, messages: Sequence[BaseMessage], system: str) -> AIMessage:
    pending = _pending_dimensions(system)
    done = _completed(system)
    request = _last_human_text(messages)

    wants_fix = any(word in request for word in ("修复", "优化", "改一下", "改进"))
    if wants_fix and "fix_assistant" not in done:
        return _call("ToFixAssistant", {"request": "按已确认的问题清单修复代码"}, "mock_handoff_fix")

    if pending:
        assistant = pending[0]
        return _call(
            _HANDOFF_OF_ASSISTANT[assistant],
            {"request": f"请审查该文件在{ASSISTANT_LABEL[assistant]}范围内的所有问题"},
            f"mock_handoff_{assistant}",
        )
    ...
```

---

# 8 运行方式与配置

## 8.1 安装

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt   # 含 langgraph / langchain-core / langchain-openai
```

## 8.2 三种运行方式

```bash
# 离线跑通全流程（不需要任何 API Key）
python -m review_assistant.main --file examples/review_target.py --question "审查这个文件" --mock

# 交互式对话；修复类操作会停下来请你确认
python -m review_assistant.main --file demo.py --codebase-root .

# 结构化输出
python -m review_assistant.main --file demo.py --question "审查这个文件" --format json

# 离线端到端测试（10 项断言，覆盖中断与放行两条路径）
python test/test_review_assistant.py
```

## 8.3 配置项

| 变量 | 作用 |
|---|---|
| LLM_PROVIDER | `openai` / `deepseek` / `mock`；留空时按密钥是否配置自动判断 |
| LLM_MODEL、LLM_API_KEY、LLM_BASE_URL | 模型接入；不填则回落读取旧的 `MODEL_NAME` / `API_KEY` / `API_URL` |
| LLM_TEMPERATURE、LLM_EXTRA_BODY | 采样温度与网关额外参数 |
| EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL | 规则库检索用的向量服务；不配则退化为离线哈希向量 |
| AGENT_ENABLE_SANDBOX | 是否注册沙箱工具（`execute_python_code` / `run_tests`） |
| AGENT_CODEBASE_ROOT | 代码库检索工具的检索范围；留空则不注册检索类工具 |

运行参数：`--file` `--question` `--codebase-root` `--provider` `--mock` `--yes`
`--thread` `--project` `--no-memory` `--format` `--max-chars` `--quiet`。

> `--yes` 会自动批准所有敏感操作，仅供脚本与测试使用；真实使用时请保留人工确认。

---

# 9 与手写 ReAct 版本（`agent/`）的对比

同一批工具、同一份数据契约，两种编排范式：

| | `agent/`（原实现） | `review_assistant/`（本项目） |
|---|---|---|
| 编排方式 | 手写 ReAct 循环 + 线性流水线 | LangGraph `StateGraph` |
| 谁决定下一步 | Python 代码里的循环与 `if` | 图的拓扑 + 模型给出的工具调用 |
| 停止条件 | 八类显式停止条件（步数 / 工具数 / token / 墙钟 / 重复动作 …） | 图自然收敛 + `recursion_limit` |
| 多智能体 | Reviewer / Critic / Verifier 三次串行调用，各自独立上下文 | 主助手 + 5 子助手，共享同一份消息与状态 |
| 工具调用协议 | 原生 function calling，不支持时降级为文本 ReAct | 原生 function calling |
| 人工确认 | 无（依靠沙箱违规分级） | `interrupt_before` + CLI 确认 |
| 安全边界 | 靠角色白名单 | 靠角色白名单 + 图的节点划分 |
| 可观测性 | 全链路 trace（JSONL，可聚合统计） | 节点事件流 + 逐节点运行时打印 |

两种实现没有优劣之分，取舍在于：
**手写循环把控制力留在自己手里**（终止条件、预算、降级都能精确控制，也更容易讲清每个细节）；
**图编排把编排逻辑显式化**（谁在什么时候说话、状态怎么合并、哪个节点要停下来问用户，一眼能看懂，也更容易扩展）。

---

# 10 已知取舍

1. **代码要进每次调用的系统消息**：与旅行助手注入 `user_info` 同理，助手必须拿到源码才能把它作为参数传给工具。
   因此长文件建议配置 `--codebase-root`，让助手改用 `read_project_file` / `search_codebase` 按需读取
   （`numbered_code` 默认在 20000 字符处截断）。
2. **一次只允许一个工具调用**：路由节点会拒绝「一轮多个调用」，让交接与状态流转可预测，代价是长任务需要更多轮次。
3. **mock 模型不做真实修复**：它只验证链路是否工作，不产生真实结论；正式使用请配置真实模型。
4. **沙箱执行必须人工确认**：修复助手要跑 `execute_python_code` / `run_tests` 时会暂停等待；
   `collect_fix_assistant` 只做无副作用的语法与接口校验，不会绕过这道闸门。
5. **checkpointer 是内存实现**：`InMemorySaver` 适合单进程交互；要跨进程恢复需换成持久化 checkpointer。
