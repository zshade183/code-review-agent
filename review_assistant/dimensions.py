"""审查维度的注册表。

代码审查按四大类问题拆分，与数据集标签、``Category`` 枚举一一对应：

    Security Vulnerability -> security_assistant    安全审查助手
    Potential Bug          -> bug_assistant         缺陷审查助手
    Code Smell             -> smell_assistant       规范与坏味审查助手
    Performance Issue      -> performance_assistant 性能审查助手

修复（改代码 + 沙箱验证）不是「审查维度」，单独作为 ``fix_assistant``。
"""

from __future__ import annotations

from typing import Dict, Tuple

from agent.schema import Category

#: 主助手节点名
PRIMARY_ASSISTANT = "primary_assistant"

#: 四个审查维度助手（顺序即主助手默认的委派顺序）
DIMENSION_ASSISTANTS: Tuple[str, ...] = (
    "security_assistant",
    "bug_assistant",
    "smell_assistant",
    "performance_assistant",
)

#: 修复助手（唯一持有敏感工具的子助手）
FIX_ASSISTANT = "fix_assistant"

#: 子助手 -> 负责的问题分类
ASSISTANT_CATEGORY: Dict[str, Category] = {
    "security_assistant": Category.SECURITY,
    "bug_assistant": Category.BUG,
    "smell_assistant": Category.SMELL,
    "performance_assistant": Category.PERFORMANCE,
}

#: 问题分类 -> 负责的子助手
CATEGORY_ASSISTANT: Dict[Category, str] = {
    category: assistant for assistant, category in ASSISTANT_CATEGORY.items()
}

#: 子助手 -> 中文名（打印提示与提示词里都用它）
ASSISTANT_LABEL: Dict[str, str] = {
    PRIMARY_ASSISTANT: "主助手",
    "security_assistant": "安全审查助手",
    "bug_assistant": "缺陷审查助手",
    "smell_assistant": "规范与坏味审查助手",
    "performance_assistant": "性能审查助手",
    FIX_ASSISTANT: "修复助手",
}

#: handoff 工具名（Pydantic 类名）-> 子助手节点名
HANDOFF_TARGET: Dict[str, str] = {
    "ToSecurityAssistant": "security_assistant",
    "ToBugAssistant": "bug_assistant",
    "ToSmellAssistant": "smell_assistant",
    "ToPerformanceAssistant": "performance_assistant",
    "ToFixAssistant": FIX_ASSISTANT,
}

#: 需要人工确认后才允许执行的工具
SENSITIVE_TOOL_NAMES: Tuple[str, ...] = (
    "apply_patch",
    "execute_python_code",
    "run_tests",
)

#: 各维度助手的推荐取证工具组合（写进提示词，减少无效试探）
DIMENSION_ENGINES: Dict[str, str] = {
    "security_assistant": "rules,bandit",
    "bug_assistant": "rules",
    "smell_assistant": "pylint",
    "performance_assistant": "rules",
}
