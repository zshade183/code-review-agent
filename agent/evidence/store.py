"""共享证据存储。

同一个文件在一次会话里经常被反复审查（编辑器保存、CI 重跑、修复后再审），
每次重新跑 pylint / bandit 既慢又会产生完全相同的输出。``EvidenceStore`` 按
「代码内容 + 项目 + 引擎集合」缓存 ``EvidenceBundle``，让所有领域 Reviewer 与
Verifier 始终拿到**同一份**取证快照：

- 缓存键包含代码哈希：代码一变就重新取证，不会给出过期结论
- 只缓存取证结果，不缓存 LLM 结论：审查仍然是每次独立推理
- 有容量上限，避免长驻服务把内存吃满
"""

from __future__ import annotations

import copy
import hashlib
import threading
from collections import OrderedDict
from typing import Dict, Iterable, Optional, Tuple

from agent.evidence.bundle import EvidenceBundle, MemoryHit
from agent.evidence.collector import EvidenceCollector
from agent.memory import MemoryStore
from agent.tools.codebase import CodebaseIndex


def code_fingerprint(code: str) -> str:
    """代码内容指纹（只用于缓存键，不用于安全场景）。"""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:24]


class EvidenceStore:
    """带 LRU 上限的进程内证据缓存。"""

    def __init__(self, max_entries: int = 64) -> None:
        self.max_entries = max(1, max_entries)
        self._cache: "OrderedDict[str, EvidenceBundle]" = OrderedDict()
        self._lock = threading.Lock()
        self._codebase_indexes: Dict[str, CodebaseIndex] = {}
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------

    @staticmethod
    def _key(fingerprint: str, project: str, engines: Iterable[str], codebase_root: str = "") -> str:
        return f"{fingerprint}|{project}|{','.join(sorted(set(engines)))}|{codebase_root}"

    def get_or_collect(
        self,
        code: str,
        file_path: Optional[str] = None,
        project: str = "default",
        engines: Tuple[str, ...] = ("rules",),
        memory: Optional[MemoryStore] = None,
        session_id: str = "",
        codebase_root: Optional[str] = None,
    ) -> EvidenceBundle:
        """读取缓存，未命中则执行一次公共取证并写回。

        注意：缓存里**不保存** ``memory_context``——它包含会话短期记忆，
        按 session 隔离；命中缓存时按当前会话重新构建，避免跨会话串读。
        """
        fingerprint = code_fingerprint(code)
        root = str(codebase_root or "")
        key = self._key(fingerprint, project, engines, root)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self.hits += 1
                self._cache.move_to_end(key)
                bundle = copy.copy(cached)
                bundle.memory_context = ""
                bundle.memory_hits = []
                if memory is not None:
                    try:
                        bundle.memory_context = memory.build_context(
                            code, project=project, session_id=session_id
                        )
                        bundle.memory_hits = [
                            MemoryHit(
                                kind=record.kind,
                                score=score,
                                text=record.text,
                                payload=dict(record.payload or {}),
                            )
                            for score, record in memory.recall(
                                code,
                                project=project,
                                kinds=["false_positive", "convention", "entity"],
                                top_k=5,
                            )
                        ]
                    except Exception:  # noqa: BLE001 - 记忆不可用不应影响审查
                        pass
                return bundle
            self.misses += 1

        codebase_index = None
        if root:
            with self._lock:
                codebase_index = self._codebase_indexes.get(root)
                if codebase_index is None:
                    codebase_index = CodebaseIndex(root)
                    self._codebase_indexes[root] = codebase_index
        bundle = EvidenceCollector(
            engines=engines, memory=memory, project=project, codebase_index=codebase_index
        ).collect(code, file_path=file_path, session_id=session_id)

        stored = copy.copy(bundle)
        stored.memory_context = ""
        stored.memory_hits = list(bundle.memory_hits)
        with self._lock:
            self._cache[key] = stored
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
        return bundle

    # ------------------------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> Dict[str, object]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._cache),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }
