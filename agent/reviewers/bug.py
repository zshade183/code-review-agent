"""Bug Reviewer：只负责潜在缺陷维度。"""

from __future__ import annotations

from agent.reviewers.base import DomainReviewer
from agent.schema import Category


class BugReviewer(DomainReviewer):
    role = "bug"
    category = Category.BUG
    display_name = "缺陷审查专家"
    focus = """空值/None 处理、边界条件与越界、除零、逻辑条件写错、参数顺序、
返回值错误、类型不匹配、资源未释放、异常吞掉、文件/目录存在性假设、可变默认参数。"""
    #: 缺陷维度几乎对所有可解析代码都适用：即使没有静态告警，也要看逻辑正确性
    triggers = ()
