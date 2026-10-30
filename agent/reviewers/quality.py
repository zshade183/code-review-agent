"""Quality Reviewer：只负责代码坏味维度。"""

from __future__ import annotations

from agent.reviewers.base import DomainReviewer
from agent.schema import Category


class QualityReviewer(DomainReviewer):
    role = "quality"
    category = Category.SMELL
    display_name = "代码质量审查专家"
    focus = """未使用变量/导入、魔法数字、异常处理不当、重复/死代码、过长函数、
参数过多、命名不规范、重复代码、资源未释放（代码组织层面）。

注意：纯数据模型、配置常量这类文件通常不值得做命名/坏味审查，请只报
**确实会误导维护者或掩盖缺陷**的坏味，不要输出纯风格偏好。"""
    triggers = (
        "def ",
        "class ",
        "for ",
        "while ",
        "try:",
        "except",
        "lambda",
        "magic",
        "TODO",
        "FIXME",
        "pass",
    )
