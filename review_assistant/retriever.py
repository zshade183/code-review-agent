"""规则库检索。

把「审查规则库 + 项目约定记忆」建成索引，供 ``lookup_rule`` 使用。

索引后端有两种，自动选择：

- 配了 ``EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL``：走真实向量服务
- 没配：退化为 ``HashingEmbedder``（词项哈希向量）+ 余弦相似度

第二种不是语义检索，但**完全离线、零成本、结果可复现**，足以支撑「按关键词找到
对应规则条目」这个用途；没有外部服务时也能检索。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from agent.memory import HashingEmbedder, MemoryStore
from agent.settings import MEMORY_PATH

_SECTION_RE = re.compile(r"(?=【规则\s*\d+)")


def _load_embeddings():
    """有 embedding 配置就返回 LangChain Embeddings，否则 None。"""
    api_key = (os.getenv("EMBEDDING_API_KEY") or "").strip()
    base_url = (os.getenv("EMBEDDING_BASE_URL") or "").strip()
    model = (os.getenv("EMBEDDING_MODEL") or "").strip()
    if not (api_key and base_url and model):
        return None
    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError:  # pragma: no cover - 可选依赖
        return None
    return OpenAIEmbeddings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        check_embedding_ctx_length=False,
        timeout=60,
        max_retries=1,
    )


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def build_corpus(project: str = "default") -> List[Dict[str, str]]:
    """拼装检索语料：内置规则库 + 项目约定记忆。"""
    docs: List[Dict[str, str]] = []

    try:
        from core.prompts import OPTIMIZE_RULES, REVIEW_RULES
    except Exception:  # pragma: no cover - 兼容独立运行
        REVIEW_RULES = OPTIMIZE_RULES = ""

    for index, section in enumerate(_SECTION_RE.split(REVIEW_RULES)):
        text = section.strip().strip("= \n")
        if not text:
            continue
        docs.append(
            {
                "source": "review_rules",
                "title": f"审查规则库 第 {index} 段",
                "page_content": text,
            }
        )

    if OPTIMIZE_RULES.strip():
        docs.append(
            {
                "source": "optimize_rules",
                "title": "修复/优化约束",
                "page_content": OPTIMIZE_RULES.strip(),
            }
        )

    try:
        store = MemoryStore()
        allowed_projects = {"default"} if project == "default" else {"default", project}
        for record in store.records:
            if record.kind != "convention":
                continue
            if record.project not in allowed_projects:
                continue
            docs.append(
                {
                    "source": "memory",
                    "title": f"项目约定（{record.project}）",
                    "page_content": record.text,
                }
            )
        for record in store.entities(project):
            facts = json.dumps(record.payload.get("facts", {}), ensure_ascii=False, sort_keys=True)
            docs.append(
                {
                    "source": "entity_memory",
                    "title": f"实体事实（{record.payload.get('entity_type', 'entity')}）",
                    "page_content": f"{record.payload.get('name', record.text)}: {facts}",
                }
            )
    except Exception:  # noqa: BLE001 - 记忆库不可用不应阻断检索
        pass

    return docs


class RuleRetriever:
    """极简向量检索器：文档只有几十条，直接全量点积即可。"""

    def __init__(self, docs: Sequence[Dict[str, str]], vectors: np.ndarray, backend: str) -> None:
        if len(docs) == 0:
            raise ValueError("没有可检索的文档")
        self._docs = list(docs)
        self._matrix = _normalize(np.asarray(vectors, dtype=float))
        self.backend = backend

    @classmethod
    def from_docs(cls, docs: Sequence[Dict[str, str]]) -> "RuleRetriever":
        texts = [doc["page_content"] for doc in docs]
        embeddings = _load_embeddings()
        if embeddings is not None:
            vectors = np.asarray(embeddings.embed_documents(texts), dtype=float)
            return cls(docs, vectors, "openai-embeddings")

        hashing = HashingEmbedder()
        vectors = np.asarray([hashing.embed(text) for text in texts], dtype=float)
        return cls(docs, vectors, "hashing-embedder(offline)")

    def _embed_query(self, query: str) -> np.ndarray:
        if self.backend == "openai-embeddings":
            embeddings = _load_embeddings()
            vector = np.asarray(embeddings.embed_query(query), dtype=float)  # type: ignore[union-attr]
        else:
            vector = np.asarray(HashingEmbedder().embed(query), dtype=float)
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def query(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        if not (query or "").strip():
            raise ValueError("查询内容不能为空")
        k = max(1, min(int(k), len(self._docs)))

        scores = self._matrix @ self._embed_query(query)
        top = np.argsort(-scores)[:k]
        return [
            {**self._docs[int(index)], "similarity": float(scores[index])}
            for index in top
        ]


_RETRIEVERS: Dict[str, Tuple[int, RuleRetriever]] = {}


def get_retriever(project: str = "default") -> RuleRetriever:
    """按项目缓存检索器；记忆文件变化后自动重建。"""
    path = Path(MEMORY_PATH)
    revision = path.stat().st_mtime_ns if path.exists() else 0
    cached = _RETRIEVERS.get(project)
    if cached is None or cached[0] != revision:
        cached = (revision, RuleRetriever.from_docs(build_corpus(project)))
        _RETRIEVERS[project] = cached
    return cached[1]
