"""规范检索工具：``lookup_rule`` 的实现。"""

from __future__ import annotations

from langchain_core.tools import tool

from review_assistant.retriever import get_retriever


@tool
def lookup_rule(query: str) -> str:
    """检索项目内置的代码审查规则库与项目约定。

    在判定某个问题属于哪一类、或需要引用规则依据时使用。
    返回的是本地规则库与历史约定的片段，不代表最新行业标准。

    Args:
        query: 检索关键词，例如「除零 边界」「硬编码密钥」「嵌套循环」。
    """
    results = get_retriever().query(query, k=3)
    if not results:
        return "（规则库中未找到相关条目）"
    return "\n\n".join(
        f"[{item['title']}｜来源 {item['source']}｜相似度 {item['similarity']:.3f}]\n"
        f"{item['page_content']}"
        for item in results
    )
