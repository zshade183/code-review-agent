"""Security Reviewer：只负责安全漏洞维度。"""

from __future__ import annotations

from agent.reviewers.base import DomainReviewer
from agent.schema import Category


class SecurityReviewer(DomainReviewer):
    role = "security"
    category = Category.SECURITY
    display_name = "安全审查专家"
    focus = """注入类（SQL/命令/模板）、危险函数、不安全反序列化、硬编码凭据、
证书校验关闭、路径穿越、弱加密与随机数、外部输入未经校验直接进入敏感操作。"""
    triggers = (
        "os.system",
        "subprocess",
        "eval(",
        "exec(",
        "pickle",
        "yaml.load",
        "sql",
        "SELECT",
        "password",
        "secret",
        "token",
        "md5",
        "requests.",
        "input(",
        "open(",
        "shell=True",
        "verify=False",
    )
