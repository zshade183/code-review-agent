"""领域 Reviewer 注册表与动态选择。

多 Agent 是默认能力，但**不是每次请求都无条件调用所有 Agent**：

- ``fast`` 策略根本不进入这里（走单 Agent 基线）
- ``auto`` 策略调用 ``select_applicable_roles``，按共享证据与代码特征决定启动几个领域
- ``full`` 策略固定启动四个领域

选择逻辑保持确定性（纯 Python 规则），不使用 LLM 决定「该启动哪个领域」。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Type

from agent.evidence.bundle import EvidenceBundle
from agent.llm import LLMClient
from agent.reviewers.base import DomainReviewer
from agent.reviewers.base import DOMAIN_REVIEWER_TOOL_NAMES
from agent.reviewers.bug import BugReviewer
from agent.reviewers.performance import PerformanceReviewer
from agent.reviewers.quality import QualityReviewer
from agent.reviewers.security import SecurityReviewer
from agent.runtime import AgentConfig
from agent.schema import Category
from agent.tools import build_reviewer_registry
from agent.tools.base import ToolRegistry

#: 稳定顺序：覆盖度报告、trace 与并发提交顺序都按它走，保证可复现
REVIEWER_CLASSES: Dict[str, Type[DomainReviewer]] = {
    "security": SecurityReviewer,
    "bug": BugReviewer,
    "quality": QualityReviewer,
    "performance": PerformanceReviewer,
}

#: 分类 -> 默认负责该分类的领域角色
CATEGORY_TO_ROLE: Dict[Category, str] = {
    Category.SECURITY: "security",
    Category.BUG: "bug",
    Category.SMELL: "quality",
    Category.PERFORMANCE: "performance",
}


def all_roles() -> List[str]:
    return list(REVIEWER_CLASSES)


def build_domain_reviewer(
    role: str,
    client: Optional[LLMClient] = None,
    registry: Optional[ToolRegistry] = None,
    config: Optional[AgentConfig] = None,
    codebase_root: Optional[str] = None,
) -> DomainReviewer:
    """按角色名实例化领域 Reviewer。"""
    cls = REVIEWER_CLASSES.get(role)
    if cls is None:
        raise KeyError(f"未知的 Reviewer 角色: {role}（可选：{', '.join(REVIEWER_CLASSES)}）")
    return cls(
        client=client,
        registry=registry or build_reviewer_registry(codebase_root),
        config=config,
        codebase_root=codebase_root,
    )


def build_domain_reviewers(
    roles: Sequence[str],
    client: Optional[LLMClient] = None,
    configs: Optional[Dict[str, AgentConfig]] = None,
    codebase_root: Optional[str] = None,
    registry: Optional[ToolRegistry] = None,
) -> Dict[str, DomainReviewer]:
    """批量实例化（同一轮内共享只读工具注册表，避免重复建索引）。"""
    full_registry = registry or build_reviewer_registry(codebase_root)
    available = [name for name in DOMAIN_REVIEWER_TOOL_NAMES if name in full_registry]
    shared_registry = full_registry.subset(available)
    configs = configs or {}
    return {
        role: build_domain_reviewer(
            role,
            client=client,
            registry=shared_registry,
            config=configs.get(role),
            codebase_root=codebase_root,
        )
        for role in roles
    }


def select_applicable_roles(
    code: str,
    bundle: EvidenceBundle,
    client: Optional[LLMClient] = None,
    max_reviewers: int = 4,
) -> List[str]:
    """按证据与代码特征选择要启动的领域（auto 策略）。

    规则完全确定性：

    1. 语法不合法时只保留 bug（负责语法错误本身）
    2. 静态分析已经命中某维度 -> 该维度必选
    3. 否则用各领域的关键特征做启发式判断（例如没有循环就不启动性能审查）
    4. 至少启动一个领域；最多 ``max_reviewers`` 个
    """
    selected: List[str] = []
    for role, cls in REVIEWER_CLASSES.items():
        if not bundle.syntax_ok and cls.category is not Category.BUG:
            applicable = False
        elif bundle.findings_for(cls.category):
            applicable = True
        elif not cls.triggers:
            applicable = True
        else:
            applicable = any(trigger in code for trigger in cls.triggers)
        if applicable:
            selected.append(role)
    if not selected:
        selected = ["bug"]
    return selected[: max(1, max_reviewers)]
