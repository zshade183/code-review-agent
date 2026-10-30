"""Evidence Collector：一次性公共取证。

放到编排最前面执行，产出一个 ``EvidenceBundle`` 供所有 Reviewer / Verifier / 裁决器共享。
这样做的直接收益：

- 同一份静态分析结果不会被四个领域中重复计算
- 各角色看到的是**同一份事实**，结论差异只可能来自判断而不是取证漂移
- 取证失败（例如 pylint 不可用）会被记录到 ``engine_errors``，最终体现在覆盖度上
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Sequence

from agent.evidence.bundle import EvidenceBundle, MemoryHit
from agent.memory import MemoryStore
from agent.schema import Issue
from agent.tools.ast_tools import CheckSyntaxTool, GetAstOutlineTool
from agent.tools.codebase import CodebaseIndex
from agent.tools.static_analysis import run_bandit, run_pylint, run_rules

logger = logging.getLogger(__name__)

#: 引擎名 -> 执行函数（rules 为项目自研，其余为可选第三方）
ENGINES = {
    "rules": run_rules,
    "pylint": run_pylint,
    "bandit": run_bandit,
}


class EvidenceCollector:
    """在编排开始时执行一次公共取证。"""

    def __init__(
        self,
        engines: Sequence[str] = ("rules",),
        memory: Optional[MemoryStore] = None,
        project: str = "default",
        max_ast_items: int = 40,
        codebase_index: Optional[CodebaseIndex] = None,
    ) -> None:
        self.engines = tuple(engines)
        self.memory = memory
        self.project = project
        self.max_ast_items = max_ast_items
        self.codebase_index = codebase_index

    # ------------------------------------------------------------------

    def collect(
        self,
        code: str,
        file_path: Optional[str] = None,
        session_id: str = "",
    ) -> EvidenceBundle:
        started = time.time()
        bundle = EvidenceBundle(code=code, file_path=file_path or "", project=self.project)

        syntax_result = CheckSyntaxTool()(code=code)
        bundle.syntax_ok = bool(syntax_result.ok)
        bundle.syntax_detail = syntax_result.content if not syntax_result.ok else ""

        outline_result = GetAstOutlineTool()(code=code, max_items=self.max_ast_items)
        if outline_result.ok and isinstance(outline_result.data, dict):
            # GetAstOutlineTool 的负载键是 definitions；entries 是历史别名，保留兜底。
            entries = outline_result.data.get("definitions") or outline_result.data.get("entries")
            if isinstance(entries, list):
                bundle.ast_outline = [item for item in entries if isinstance(item, dict)]
                bundle.symbols = [
                    {
                        "name": item.get("name", ""),
                        "kind": item.get("kind", ""),
                        "line_start": item.get("line_start", 1),
                        "line_end": item.get("line_end", item.get("line_start", 1)),
                    }
                    for item in bundle.ast_outline
                    if item.get("name")
                ]
            imports = outline_result.data.get("top_level_imports")
            if isinstance(imports, list):
                bundle.imports = [str(item) for item in imports if str(item).strip()]
        elif not outline_result.ok:
            bundle.engine_errors["ast"] = outline_result.error or "AST 解析失败"

        if self.codebase_index is not None and bundle.syntax_ok:
            query_parts = [item.get("name", "") for item in bundle.symbols[:8]]
            query_parts.extend(bundle.imports[:6])
            query = " ".join(part for part in query_parts if part).strip()
            if query:
                try:
                    bundle.codebase_hits = self.codebase_index.search(query, top_k=5)
                except Exception as exc:  # noqa: BLE001 - 项目检索失败不阻塞本文件审查
                    bundle.engine_errors["codebase"] = str(exc)

        findings: List[Issue] = []
        for name in self.engines:
            runner = ENGINES.get(name)
            if runner is None:
                bundle.engine_errors[name] = "未注册的引擎"
                continue
            try:
                findings.extend(runner(code))
                bundle.engines_used.append(name)
            except Exception as exc:  # noqa: BLE001 - 单个引擎失败不应中断整体取证
                bundle.engine_errors[name] = str(exc)
                logger.info("取证引擎 %s 执行失败: %s", name, exc)
        bundle.rule_findings = findings

        if self.memory is not None:
            try:
                bundle.memory_context = self.memory.build_context(
                    code, project=self.project, session_id=session_id
                )
                bundle.memory_hits = [
                    MemoryHit(
                        kind=record.kind,
                        score=score,
                        text=record.text,
                        payload=dict(record.payload or {}),
                    )
                    for score, record in self.memory.recall(
                        code,
                        project=self.project,
                        kinds=["false_positive", "convention", "entity"],
                        top_k=5,
                    )
                ]
            except Exception as exc:  # noqa: BLE001 - 记忆不可用不应阻塞取证
                logger.info("记忆召回失败: %s", exc)

        bundle.elapsed_ms = (time.time() - started) * 1000.0
        return bundle


def collect_evidence(
    code: str,
    file_path: Optional[str] = None,
    engines: Iterable[str] = ("rules",),
    memory: Optional[MemoryStore] = None,
    project: str = "default",
    session_id: str = "",
) -> EvidenceBundle:
    """函数式快捷入口。"""
    return EvidenceCollector(engines=tuple(engines), memory=memory, project=project).collect(
        code, file_path=file_path, session_id=session_id
    )
