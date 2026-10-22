"""子助手交接契约。

这些 Pydantic 模型直接通过 ``llm.bind_tools`` 暴露给模型：
模型「调用」某个交接工具，就等于把当前任务交给对应子助手。
LangGraph 侧由 ``enter_<assistant>`` 节点消费这次调用。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToSecurityAssistant(BaseModel):
    """把安全类问题（注入、危险函数、硬编码密钥等）的取证与判定交给安全审查助手。"""

    request: str = Field(description="需要安全审查助手处理的具体任务")


class ToBugAssistant(BaseModel):
    """把潜在缺陷（空值、边界、越界、逻辑条件、除零等）的取证与判定交给缺陷审查助手。"""

    request: str = Field(description="需要缺陷审查助手处理的具体任务")


class ToSmellAssistant(BaseModel):
    """把代码坏味（命名、未使用变量、异常处理、重复代码等）的判定交给规范与坏味审查助手。"""

    request: str = Field(description="需要规范与坏味审查助手处理的具体任务")


class ToPerformanceAssistant(BaseModel):
    """把性能问题（低效循环、重复计算、数据结构误用、冗余 IO 等）的判定交给性能审查助手。"""

    request: str = Field(description="需要性能审查助手处理的具体任务")


class ToFixAssistant(BaseModel):
    """把「按已确认的问题清单修复代码」交给修复助手；其沙箱执行需要用户确认。"""

    request: str = Field(description="需要修复助手处理的具体任务")


class CompleteOrEscalate(BaseModel):
    """当前维度的任务已结束，或出现其他维度/业务需求时，交回主助手。"""

    reason: str = Field(description="完成情况或需要转交的原因")
