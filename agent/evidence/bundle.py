"""共享证据包。

多智能体场景下，如果每个领域 Reviewer 都各自跑一遍语法检查、AST 解析与规则扫描，
既浪费 token 与时间，也会因为「各自看到不同输出」而让结论难以对齐。

``EvidenceBundle`` 把一次性的公共取证结果固化下来，供所有下游角色只读复用：

- 领域 Reviewer：把与自身维度相关的候选告警作为「待复核线索」注入提示词
- 独立 Verifier：按 Reviewer 用过的引擎选择**没被用过**的引擎做交叉验证
- 裁决器：需要知道语法基线，避免在无法解析的代码上强行确认结论
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from agent.schema import Category, Issue


@dataclass
class MemoryHit:
    """记忆召回结果的精简表示（不落盘、不携带原始源码）。"""

    kind: str
    score: float
    text: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "score": round(self.score, 4), "text": self.text}


@dataclass
class EvidenceBundle:
    """一次审查的公共证据快照。"""

    code: str
    file_path: str = ""
    project: str = "default"
    syntax_ok: bool = True
    syntax_detail: str = ""
    ast_outline: List[Dict[str, Any]] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    codebase_hits: List[Dict[str, Any]] = field(default_factory=list)
    rule_findings: List[Issue] = field(default_factory=list)
    engine_errors: Dict[str, str] = field(default_factory=dict)
    memory_context: str = ""
    memory_hits: List[MemoryHit] = field(default_factory=list)
    engines_used: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    # ---------- 查询 ----------

    @property
    def total_lines(self) -> int:
        return max(1, len(self.code.split("\n")))

    def categories(self) -> Set[Category]:
        """规则扫描命中的维度集合。"""
        return {issue.category for issue in self.rule_findings}

    def findings_for(self, category: Category) -> List[Issue]:
        return [issue for issue in self.rule_findings if issue.category == category]

    def refs_for(self, category: Optional[Category] = None) -> List[str]:
        """返回可被 Reviewer / Verifier 引用的证据键（稳定、可读）。"""
        refs: List[str] = ["syntax"] if self.syntax_ok else ["syntax:error"]
        if self.ast_outline:
            refs.append("ast")
        refs.extend(f"import:{name}" for name in self.imports[:8])
        refs.extend(f"codebase:{hit.get('path', '')}:{hit.get('line_start', 1)}" for hit in self.codebase_hits[:5])
        findings = self.rule_findings if category is None else self.findings_for(category)
        refs.extend(f"rule:{issue.line_start}:{issue.subtype}" for issue in findings[:12])
        refs.extend(f"engine:{name}" for name in self.engines_used)
        return refs

    def shared_tool_names(self) -> List[str]:
        """公共取证阶段已经真实执行过的工具名，供证据校验复用。"""
        names = ["check_syntax"]
        if self.ast_outline:
            names.append("get_ast_outline")
        if self.engines_used:
            names.append("run_static_analysis")
        return names

    # ---------- 提示词渲染 ----------

    def prompt_block(
        self,
        category: Optional[Category] = None,
        max_findings: int = 12,
    ) -> str:
        """渲染注入提示词的公共证据（领域 Reviewer 复用，避免重复取证）。"""
        blocks: List[str] = ["【共享取证结果】（由 Evidence Collector 一次性生成，可直接引用）"]

        syntax = "语法检查通过" if self.syntax_ok else f"语法检查未通过：{self.syntax_detail}"
        blocks.append(f"- {syntax}")

        if self.ast_outline:
            items = []
            for entry in self.ast_outline[:12]:
                name = entry.get("name") or entry.get("kind") or "?"
                start = entry.get("line_start")
                end = entry.get("line_end")
                items.append(f"{name}@{start}-{end}")
            blocks.append("- 代码结构：" + "；".join(items))

        if self.imports:
            blocks.append("- 顶层依赖：" + "；".join(self.imports[:20]))

        if self.codebase_hits:
            lines = []
            for hit in self.codebase_hits[:5]:
                lines.append(
                    f"  · {hit.get('path', '?')}:{hit.get('line_start', 1)}-"
                    f"{hit.get('line_end', hit.get('line_start', 1))} "
                    f"[{hit.get('kind', 'code')} {hit.get('name', '')}]"
                )
            blocks.append("- 代码库相关上下文（仅作项目语义线索）：\n" + "\n".join(lines))

        findings = self.rule_findings if category is None else self.findings_for(category)
        if findings:
            lines = [
                f"  · 行 {issue.line_start}: [{issue.category.value}] {issue.subtype}"
                f"（{issue.severity.value}）"
                for issue in findings[:max_findings]
            ]
            blocks.append(
                "- 静态分析候选告警（**含误报**，请结合语义逐条复核，不要直接照抄）：\n"
                + "\n".join(lines)
            )
        elif category is not None:
            blocks.append(f"- 本维度（{category.value}）暂无静态分析候选告警，请主动取证后再下结论。")

        if self.engine_errors:
            blocks.append(
                "- 不可用的引擎：" + "、".join(sorted(self.engine_errors)) + "（相应维度覆盖可能不完整）"
            )

        if self.memory_context:
            blocks.append(self.memory_context)

        return "\n".join(blocks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "project": self.project,
            "syntax_ok": self.syntax_ok,
            "syntax_detail": self.syntax_detail,
            "ast_outline": self.ast_outline,
            "imports": list(self.imports),
            "symbols": list(self.symbols),
            "codebase_hits": list(self.codebase_hits),
            "rule_findings": [issue.to_legacy() for issue in self.rule_findings],
            "engine_errors": self.engine_errors,
            "memory_hits": [hit.to_dict() for hit in self.memory_hits],
            "engines_used": list(self.engines_used),
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


def findings_from(issues: Iterable[Issue], category: Category) -> List[Issue]:
    """按维度过滤候选告警的便捷函数。"""
    return [issue for issue in issues if issue.category == category]
