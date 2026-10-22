"""LangGraph 代码审查 Agent：主助手 + 领域子助手 + 人工确认。

采用 supervisor + handoff 的图编排：

- 复用 ``agent/tools`` 的 14 个工具、``agent/schema`` 的数据契约、
  ``agent/memory`` 的长期记忆、``core/prompts`` 的规则库
- 主助手按审查维度委派给 5 个领域子助手，敏感工具在执行前中断等待用户确认

模块分工::

    state.py            状态与 reducer
    handoffs.py         交接用的 Pydantic 契约
    llm.py              模型接入工厂
    retriever.py        规则库检索
    policy_tools.py     规范检索工具
    toolset.py          工具组装与白名单
    workflow.py         图编排
    main.py             CLI + 人工确认

入口::

    python -m review_assistant.main --file examples/review_target.py
"""

__all__ = ["workflow", "state", "handoffs", "llm", "toolset", "dimensions"]
