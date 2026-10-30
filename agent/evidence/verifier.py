"""独立验证器（Verifier）。

不依赖语言模型：用与 Reviewer / Evidence Collector **不同** 的静态分析引擎复核候选结论。
「同一位置被两个独立引擎命中」才算有佐证，这样 Reviewer 自己引用的证据不能自证清白。

三条刻意保留的边界：

1. **未找到佐证 ≠ 问题不存在**：没有佐证只降低置信度，是否保留由确定性裁决器决定。
2. **引擎可缺省**：缺少 pylint / bandit 时记录 ``engine_errors``，结论里注明能力限制，
   而不是把「无法验证」当成「已验证为假」。
3. **只看 Reviewer 实际用过的引擎**：优先选择 Reviewer 与取证阶段都没用过的引擎，
   避免「用同一份证据验证自己」。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from agent.evidence.bundle import EvidenceBundle
from agent.schema import Issue, ReviewReport
from agent.tools.static_analysis import run_bandit, run_pylint, run_rules

logger = logging.getLogger(__name__)


@dataclass
class Corroboration:
    """独立引擎佐证情况。"""

    #: 在输入候选列表中的序号（兼容旧接口）
    index: int = 0
    corroborated: bool = False
    engines: List[str] = field(default_factory=list)
    note: str = ""
    #: 稳定候选标识（多 Agent 主线使用；旧接口可留空）
    candidate_id: str = ""
    #: 实际参与验证的引擎（含失败引擎），用于表达能力限制
    engines_checked: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "candidate_id": self.candidate_id,
            "corroborated": self.corroborated,
            "engines": list(self.engines),
            "engines_checked": list(self.engines_checked),
            "note": self.note,
        }


@dataclass
class VerificationResult:
    """一次独立验证的完整结果。"""

    #: candidate_id -> 佐证
    corroborations: Dict[str, Corroboration] = field(default_factory=dict)
    engines_used: List[str] = field(default_factory=list)
    engine_errors: Dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def corroborated_count(self) -> int:
        return sum(1 for item in self.corroborations.values() if item.corroborated)

    def for_candidate(self, candidate_id: str) -> Optional[Corroboration]:
        return self.corroborations.get(candidate_id)

    def limitations(self) -> str:
        """能力限制说明（会进入裁决说明，避免把「无法验证」误读成「验证为假」）。"""
        parts: List[str] = []
        if self.engine_errors:
            parts.append("不可用引擎：" + "、".join(sorted(self.engine_errors)))
        if not self.engines_used:
            parts.append("本次没有任何独立引擎可用")
        return "；".join(parts)

    def to_dict(self) -> Dict[str, object]:
        return {
            "engines_used": list(self.engines_used),
            "engine_errors": dict(self.engine_errors),
            "elapsed_ms": round(self.elapsed_ms, 2),
            "corroborated": self.corroborated_count,
            "candidates": [item.to_dict() for item in self.corroborations.values()],
        }


class VerifierAgent:
    """用独立引擎对结论做交叉验证（无 LLM 调用）。"""

    #: 可用引擎 -> 执行函数
    ENGINES = {
        "rules": run_rules,
        "pylint": run_pylint,
        "bandit": run_bandit,
    }

    def __init__(self, line_tolerance: int = 2) -> None:
        self.line_tolerance = line_tolerance

    # ------------------------------------------------------------------
    # 引擎选择
    # ------------------------------------------------------------------

    def select_engines(self, used_engines: Sequence[str] = ()) -> Tuple[List[str], Dict[str, str]]:
        """选择独立引擎，返回 (引擎名列表, 不可用说明)。"""
        used = set(used_engines)
        independent = [name for name in self.ENGINES if name not in used]
        if not independent:
            # 所有引擎都已参与 Reviewer/公共取证，不能再把同一证据包装成“独立佐证”。
            return [], {"independence": "没有未参与前序取证的独立引擎"}
        errors: Dict[str, str] = {}
        available: List[str] = []
        for name in independent:
            runner = self.ENGINES.get(name)
            if runner is None:
                errors[name] = "未注册的引擎"
                continue
            available.append(name)
        return available, errors

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _collect_independent_issues(
        self, code: str, engines: Sequence[str]
    ) -> Tuple[List[Tuple[str, Issue]], List[str], Dict[str, str]]:
        collected: List[Tuple[str, Issue]] = []
        used: List[str] = []
        errors: Dict[str, str] = {}
        for name in engines:
            runner = self.ENGINES.get(name)
            if runner is None:
                errors[name] = "未注册的引擎"
                continue
            try:
                findings = runner(code)
            except Exception as exc:  # noqa: BLE001 - 单个引擎失败不应中断验证
                errors[name] = str(exc)
                logger.info("佐证引擎 %s 执行失败: %s", name, exc)
                continue
            used.append(name)
            collected.extend((name, issue) for issue in findings)
        return collected, used, errors

    def verify_issues(
        self,
        issues: Sequence[Issue],
        bundle: EvidenceBundle,
        used_engines: Sequence[str] = (),
    ) -> VerificationResult:
        """对候选做独立佐证（多 Agent 主线入口）。

        ``used_engines`` = Evidence Collector + 各领域 Reviewer 实际调用过的引擎集合。
        """
        started = time.time()
        engines, errors = self.select_engines(used_engines)
        independent_issues, used, run_errors = self._collect_independent_issues(
            bundle.code, engines
        )
        errors.update(run_errors)

        result = VerificationResult(engines_used=used, engine_errors=errors)
        for index, issue in enumerate(issues):
            candidate_id = issue.ensure_candidate_id()
            hits: Set[str] = set()
            for engine_name, candidate in independent_issues:
                if candidate.category != issue.category:
                    continue
                if abs(candidate.line_start - issue.line_start) <= self.line_tolerance:
                    hits.add(engine_name)
            if hits:
                corroboration = Corroboration(
                    index=index,
                    corroborated=True,
                    engines=sorted(hits),
                    note=f"独立引擎 {'/'.join(sorted(hits))} 在相近位置给出同类告警",
                    candidate_id=candidate_id,
                    engines_checked=list(used),
                )
            else:
                note = "独立引擎未在相近位置给出同类告警（只降低置信度，不直接否定该问题）"
                if errors:
                    note += f"（能力限制：{', '.join(sorted(errors))} 不可用）"
                corroboration = Corroboration(
                    index=index,
                    corroborated=False,
                    engines=[],
                    note=note,
                    candidate_id=candidate_id,
                    engines_checked=list(used),
                )
            result.corroborations[candidate_id] = corroboration
        result.elapsed_ms = (time.time() - started) * 1000.0
        return result

    # ------------------------------------------------------------------
    # 向后兼容接口（旧 multi_agent / 测试使用）
    # ------------------------------------------------------------------

    def verify(
        self,
        code: str,
        report: ReviewReport,
        reviewer_engines: Sequence[str] = ("rules",),
    ) -> List[Corroboration]:
        """对每条问题做独立佐证（旧接口，按报告顺序返回）。"""
        independent = [name for name in self.ENGINES if name not in set(reviewer_engines)]
        if not independent:
            independent = list(self.ENGINES)

        independent_issues, _used, engine_errors = self._collect_independent_issues(
            code, independent
        )
        results: List[Corroboration] = []
        for index, issue in enumerate(report.issues):
            hits: Set[str] = set()
            for engine_name, candidate in independent_issues:
                if candidate.category != issue.category:
                    continue
                if abs(candidate.line_start - issue.line_start) <= self.line_tolerance:
                    hits.add(engine_name)
            if hits:
                results.append(
                    Corroboration(
                        index=index,
                        corroborated=True,
                        engines=sorted(hits),
                        note=f"独立引擎 {'/'.join(sorted(hits))} 在相近位置给出同类告警",
                        candidate_id=issue.ensure_candidate_id(),
                        engines_checked=list(independent),
                    )
                )
            else:
                note = "独立引擎未在相近位置给出同类告警"
                if engine_errors:
                    note += f"（部分引擎不可用: {', '.join(engine_errors)}）"
                results.append(
                    Corroboration(
                        index=index,
                        corroborated=False,
                        engines=[],
                        note=note,
                        candidate_id=issue.ensure_candidate_id(),
                        engines_checked=list(independent),
                    )
                )
        return results

    def verify_bundle(
        self,
        report: ReviewReport,
        bundle: EvidenceBundle,
        reviewer_engines: Optional[Sequence[str]] = None,
    ) -> List[Corroboration]:
        """基于共享证据包做佐证：默认沿用 Collector 实际用过的引擎。"""
        engines = reviewer_engines if reviewer_engines is not None else bundle.engines_used
        return self.verify(bundle.code, report, reviewer_engines=tuple(engines or ("rules",)))


def collaboration_text(corroboration: Corroboration) -> str:
    """把佐证结果渲染成一句可读说明。"""
    if corroboration.engines:
        return f"独立引擎 {'/'.join(corroboration.engines)} 给出同类告警"
    return "独立引擎无同类告警"
