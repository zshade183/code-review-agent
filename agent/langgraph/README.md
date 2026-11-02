# LangGraph 多 Agent 主线

正式审查入口 `agent.pipelines.ReviewPipeline` 使用固定 `StateGraph`：

```text
load_target
→ prepare_context（语法、AST 和记忆）
→ Review Agent（只读工具循环，覆盖四类问题）
→ normalize_findings（证据引用校验、去重、candidate_id）
→ plan_verification（fast / auto / full）
→ 按需 Verification Agent（独立上下文、寻找反证）
→ adjudicate（confirmed / needs_review / memory_suspected / rejected）
→ finalize_report
```

Review 与 Verification 使用独立消息上下文和只读工具白名单。工具调用生成稳定 evidence_id，
相同输入在请求内复用；复核遗漏、失败或预算不足的候选保留为 needs_review。

修复不在审查图中执行。用户选择候选后，独立 `FixPipeline` 让 Fix Agent 生成候选代码，
再由 Harness 强制执行补丁生成、验证和失败回退。

```powershell
python cli.py review examples/review_target.py --strategy auto
python cli.py review examples/review_target.py --strategy full
python -m agent.langgraph.main --file examples/review_target.py --mock
```
