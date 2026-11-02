"""正式 LangGraph 多 Agent 编排内核。

固定图负责公共取证、策略选择、并行领域审查、确定性合并、独立验证、
带独立工具权限的领域 Agent、条件式跨领域复核、规则裁决和最终报告。LLM 不参与流程调度。
"""

__all__ = ["workflow", "state", "llm", "toolset"]
