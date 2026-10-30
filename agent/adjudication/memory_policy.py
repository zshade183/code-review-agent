"""记忆策略层（不是独立 Agent）。

记忆在多 Agent 主线里是一个**服务 / 策略层**，在裁决之后做事：

- 召回：历史误报概率、项目约定、文件与组件事实、相似代码形态
- 调整：只调整置信度与分组（``memory_suspected``），**绝不在独立验证前删除候选**
- 沉淀：会话摘要、文件实体事实、规范外候选子类型

把这些能力做成策略而不是一个自主 Agent，是为了避免「记忆角色自己决定删掉哪些结论」——
最终裁决必须由确定性规则完成。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence

from agent.adjudication.policy import AdjudicationResult
from agent.memory import MemoryStore
from agent.schema import AdjudicationStatus, Issue, ReviewReport

logger = logging.getLogger(__name__)


class MemoryPolicy:
    """把 ``MemoryStore`` 包装成裁决前后的确定性策略。"""

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        project: str = "default",
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.project = project or "default"
        self.enabled = bool(enabled and store is not None)

    # ------------------------------------------------------------------
    # 裁决前：召回历史误报信号
    # ------------------------------------------------------------------

    def recall_flags(self, code: str, issues: Sequence[Issue]) -> Dict[str, str]:
        """返回 candidate_id -> 误报信号说明（仅作为裁决输入，不直接删候选）。"""
        flags: Dict[str, str] = {}
        if not self.enabled or self.store is None:
            return flags
        for issue in issues:
            try:
                suppressed, reason = self.store.suppression_check(self.project, issue, code)
            except Exception as exc:  # noqa: BLE001 - 记忆不可用不应影响审查
                logger.info("记忆召回失败: %s", exc)
                continue
            if suppressed and reason:
                flags[issue.ensure_candidate_id()] = reason
        return flags

    # ------------------------------------------------------------------
    # 裁决后：降权与分组
    # ------------------------------------------------------------------

    def adjust(self, result: AdjudicationResult) -> AdjudicationResult:
        """对裁决结果做记忆侧的最终调整（不改状态、不删条目）。"""
        if not self.enabled:
            return result
        for issue in result.issues:
            if issue.adjudication is not AdjudicationStatus.MEMORY_SUSPECTED:
                continue
            issue.confidence = min(issue.confidence, 0.4)
            note = "记忆命中：与历史被驳回的形态高度一致，已降权并单独分组，仍保留供人工确认"
            issue.verify_note = "；".join(filter(None, [issue.verify_note, note]))
        return result

    # ------------------------------------------------------------------
    # 沉淀：会话摘要 / 文件实体 / 规范外候选
    # ------------------------------------------------------------------

    def record(
        self,
        report: ReviewReport,
        code: str,
        file_path: str = "",
        session_id: str = "",
        issues: Optional[Sequence[Issue]] = None,
    ) -> Dict[str, Any]:
        """裁决完成后统一写记忆，避免各角色各自写造成重复/不一致。"""
        stats: Dict[str, Any] = {"perception": False, "turns": 0, "entities": 0, "subtype_candidates": 0}
        if not self.enabled or self.store is None:
            return stats
        tracked = list(issues if issues is not None else report.issues)
        try:
            self.store.capture_perception(
                self.project,
                code,
                source="code_review",
                metadata={"file_path": file_path or ""},
                session_id=session_id,
            )
            stats["perception"] = True
        except Exception as exc:  # noqa: BLE001
            logger.info("感知记忆写入失败: %s", exc)

        if session_id:
            try:
                digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
                self.store.remember_turn(
                    session_id,
                    "user",
                    f"请求审查 {file_path or '未命名代码'}（{len(code.splitlines())} 行，摘要 {digest}）",
                    project=self.project,
                )
                self.store.remember_turn(
                    session_id,
                    "assistant",
                    f"{report.review_summary}；保留 {len(report.issues)} 个问题",
                    project=self.project,
                    metadata={"issue_count": len(report.issues)},
                )
                stats["turns"] = 2
            except Exception as exc:  # noqa: BLE001
                logger.info("短期记忆写入失败: %s", exc)

        if file_path:
            try:
                self.store.upsert_entity(
                    self.project,
                    "file",
                    file_path,
                    {
                        "language": "python",
                        "last_code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                        "line_count": len(code.splitlines()),
                        "last_issue_count": len(report.issues),
                    },
                    source="review",
                )
                stats["entities"] = 1
            except Exception as exc:  # noqa: BLE001
                logger.info("实体记忆写入失败: %s", exc)

        for issue in tracked:
            if issue.is_subtype_valid():
                continue
            try:
                if self.store.record_subtype_candidate(self.project, issue):
                    stats["subtype_candidates"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.info("规范外候选写入失败: %s", exc)
        return stats

    def stats(self) -> Dict[str, Any]:
        if self.store is None:
            return {"enabled": False}
        try:
            return {"enabled": self.enabled, "project": self.project, **self.store.stats()}
        except Exception:  # noqa: BLE001
            return {"enabled": self.enabled, "project": self.project}
