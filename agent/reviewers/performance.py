"""Performance Reviewer：只负责性能维度。"""

from __future__ import annotations

from agent.reviewers.base import DomainReviewer
from agent.schema import Category


class PerformanceReviewer(DomainReviewer):
    role = "performance"
    category = Category.PERFORMANCE
    display_name = "性能审查专家"
    focus = """循环内重复计算/重复 IO、可用生成器却建大列表、数据结构选择不当
（列表查找代替集合/字典）、正则低效、不必要的深拷贝、循环内拼接字符串、
N+1 查询、无界缓存与内存泄漏。"""
    #: 没有循环 / 批量数据处理 / IO 的代码通常不需要启动性能审查
    triggers = (
        "for ",
        "while ",
        "list(",
        "dict(",
        "set(",
        "sum(",
        "sorted(",
        "range(",
        "append(",
        "deepcopy",
        "re.compile",
        "readlines(",
    )
