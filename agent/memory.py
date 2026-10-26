"""长期记忆：把「人类反馈」沉淀成可复用的判断，让 Agent 不再重复犯错。

同一条误报形态被反复驳回、却没有被记住，下一次审查还会再报一次。
本模块提供两层记忆：

1. **误报记忆（false_positive）**：记录「某条结论被人工驳回」，下次遇到相同
   （分类 + 二级分类 + 规范化代码形态）的结论时直接压制，并给出压制依据
2. **项目约定记忆（convention）**：记录该项目的编码规范与历史决策，在审查时
   召回并注入提示词，让建议贴合项目实际而非泛泛而谈

存储用 JSONL（可直接 diff、可人工编辑、无需数据库）。相似度用标识符感知分词后的
哈希向量余弦 + 词项重叠，不引入任何外部依赖。``Embedder`` 协议留出了替换为真实
embedding 服务的接口。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from agent import settings
from agent.schema import Issue
from agent.tools.codebase import tokenize

_WS_RE = re.compile(r"\s+")
_STR_RE = re.compile(r"(['\"]).*?\1")
_NUM_RE = re.compile(r"\b\d+\b")


class Embedder(Protocol):
    """文本向量化接口（可替换为真实 embedding 服务）。"""

    dim: int

    def embed(self, text: str) -> List[float]:  # pragma: no cover - 协议声明
        ...


class HashingEmbedder:
    """确定性哈希词袋向量。

    说明：这不是语义 embedding，而是**词项哈希 + L2 归一化**，用于识别「同一类代码形态
    反复出现」。相比引入 embedding 服务，它的优点是完全离线、零成本、结果可复现；
    若需要语义级相似度，实现 ``Embedder`` 协议并注入即可（见 ``MemoryStore(embedder=...)``）。
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for token in tokenize(text or ""):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def normalize_code_form(text: str) -> str:
    """把代码片段归一化为「形态指纹」：忽略字面量、数字与空白差异。"""
    normalized = _STR_RE.sub("'S'", text or "")
    normalized = _NUM_RE.sub("N", normalized)
    return _WS_RE.sub(" ", normalized).strip().lower()


def issue_signature(issue: Issue) -> str:
    """问题的稳定指纹。

    只用「分类 + 二级分类」这两个受白名单约束的规范字段。**刻意不纳入
    ``violated_rule``**：它是自由文本（有的来源写 "Command Injection"，有的写规则 ID，
    有的为空），把它算进指纹会导致同一个问题在不同来源下指纹不一致，记忆无法命中。
    """
    payload = f"{issue.category.value}|{issue.subtype}".lower()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def code_at_lines(code: str, line_start: int, line_end: int, padding: int = 1) -> str:
    """取出问题行附近的代码片段。"""
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = max(0, line_start - 1 - padding)
    end = min(len(lines), max(line_start, line_end) + padding)
    return "\n".join(lines[start:end])


@dataclass
class MemoryRecord:
    """一条记忆。"""

    kind: str  # false_positive | convention | decision
    project: str = "default"
    text: str = ""
    signature: str = ""
    code_form: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "MemoryRecord":
        known = {f for f in MemoryRecord.__dataclass_fields__}
        return MemoryRecord(**{k: v for k, v in data.items() if k in known})


class MemoryStore:
    """JSONL 记忆库。"""

    def __init__(
        self,
        path: Optional[str] = None,
        embedder: Optional[Embedder] = None,
        similarity_threshold: float = 0.72,
        suppress_min_reports: int = 2,
        suppress_threshold: float = 0.9,
    ) -> None:
        self.path = Path(path or settings.MEMORY_PATH)
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.similarity_threshold = similarity_threshold
        #: 同一形态至少被驳回多少次才启用自动压制（避免仅凭一条记录就永久静音）
        self.suppress_min_reports = suppress_min_reports
        #: 驳回占比超过该阈值才压制
        self.suppress_threshold = suppress_threshold
        self._records: List[MemoryRecord] = []
        self._loaded = False
        self._vector_cache: Dict[str, List[float]] = {}

    # ---------- 读写 ----------

    def load(self) -> "MemoryStore":
        self._records = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._records.append(MemoryRecord.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        self._loaded = True
        return self

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def append(self, record: MemoryRecord) -> MemoryRecord:
        self.ensure_loaded()
        self._records.append(record)
        self._vector_cache.pop(record.id, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            # 记忆写入失败不应影响主流程
            pass
        return record

    @property
    def records(self) -> List[MemoryRecord]:
        self.ensure_loaded()
        return list(self._records)

    # ---------- 写入接口 ----------

    def add_convention(self, project: str, text: str, tags: Optional[Dict[str, Any]] = None) -> MemoryRecord:
        """记录一条项目约定 / 历史决策。"""
        return self.append(
            MemoryRecord(
                kind="convention",
                project=project,
                text=text.strip(),
                payload=tags or {},
            )
        )

    def record_feedback(
        self,
        project: str,
        issue: Issue,
        accepted: bool,
        code: str = "",
        note: str = "",
    ) -> MemoryRecord:
        """记录一条人工反馈：该结论被采纳还是驳回。"""
        snippet = code_at_lines(code, issue.line_start, issue.line_end) if code else ""
        return self.append(
            MemoryRecord(
                kind="false_positive" if not accepted else "accepted",
                project=project,
                text=(note or issue.risk_description).strip(),
                signature=issue_signature(issue),
                code_form=normalize_code_form(snippet),
                payload={
                    "category": issue.category.value,
                    "subtype": issue.subtype,
                    "severity": issue.severity.value,
                    "line": issue.line_start,
                    "accepted": accepted,
                },
            )
        )

    # ---------- 规范外候选子类型 ----------

    def record_subtype_candidate(
        self, project: str, issue: Issue, note: str = ""
    ) -> Optional[MemoryRecord]:
        """记录一条「规范外问题」的候选子类型，供后续人工晋升。

        同一项目下同一 ``(分类, 子类型)`` 只落一条，避免每轮审查都重复堆积；
        返回新写入的记录，已存在时返回 ``None``。
        """
        self.ensure_loaded()
        category = issue.category.value
        subtype = (issue.suggested_subtype or issue.subtype).strip()
        if not subtype:
            return None
        signature = hashlib.sha256(f"{category}|{subtype}".lower().encode("utf-8")).hexdigest()[:16]
        for record in self._records:
            if record.kind != "subtype_candidate" or record.project != project:
                continue
            if record.signature == signature:
                return None
        return self.append(
            MemoryRecord(
                kind="subtype_candidate",
                project=project,
                text=(note or issue.risk_description or subtype).strip(),
                signature=signature,
                payload={
                    "category": category,
                    "subtype": subtype,
                    "severity": issue.severity.value,
                    "line": issue.line_start,
                    "description": issue.risk_description,
                },
            )
        )

    def subtype_candidates(
        self, project: str = "default", min_count: int = 1
    ) -> List[Dict[str, Any]]:
        """按 ``(分类, 子类型)`` 聚合候选，按出现次数降序返回。"""
        self.ensure_loaded()
        groups: Dict[Tuple[str, str], List[MemoryRecord]] = {}
        for record in self._records:
            if record.kind != "subtype_candidate":
                continue
            if project != "default" and record.project not in (project, "default"):
                continue
            category = str(record.payload.get("category") or "")
            subtype = str(record.payload.get("subtype") or record.text or "").strip()
            if not category or not subtype:
                continue
            groups.setdefault((category, subtype), []).append(record)

        results: List[Dict[str, Any]] = []
        for (category, subtype), records in groups.items():
            if len(records) < min_count:
                continue
            times = [record.created_at for record in records]
            results.append(
                {
                    "category": category,
                    "subtype": subtype,
                    "count": len(records),
                    "projects": sorted({record.project for record in records}),
                    "samples": [record.text for record in records if record.text][:3],
                    "first_seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(min(times))),
                    "last_seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(max(times))),
                }
            )
        results.sort(key=lambda item: (-item["count"], item["category"], item["subtype"]))
        return results

    def promote_subtype(
        self, project: str, category: str, subtype: str
    ) -> MemoryRecord:
        """把候选子类型晋升为白名单项（人工确认后调用）。

        写入 ``kind="subtype_whitelist"`` 记忆后立即刷新运行期允许集合，
        后续审查同一进程内即可按规范内问题对待。
        """
        self.ensure_loaded()
        subtype = re.sub(r"\s+", " ", str(subtype or "")).strip()
        category_text = str(category or "").strip()
        if not subtype or not category_text:
            raise ValueError("category 和 subtype 均不能为空")
        payload = {
            "category": category_text,
            "subtype": subtype,
            "source": "manual_promotion",
        }
        for record in self._records:
            if record.kind != "subtype_whitelist":
                continue
            if (
                str(record.payload.get("category") or "") == category_text
                and str(record.payload.get("subtype") or record.text or "").strip() == subtype
            ):
                return record
        record = self.append(
            MemoryRecord(
                kind="subtype_whitelist",
                project=project,
                text=subtype,
                payload=payload,
            )
        )
        from agent.schema import refresh_allowed_subtypes

        refresh_allowed_subtypes()
        return record

    # ---------- 召回 ----------

    def _vector(self, record: MemoryRecord) -> List[float]:
        if record.id not in self._vector_cache:
            self._vector_cache[record.id] = self.embedder.embed(
                f"{record.text} {record.code_form} {record.payload.get('subtype', '')}"
            )
        return self._vector_cache[record.id]

    def recall(
        self,
        query: str,
        project: str = "default",
        kinds: Optional[Iterable[str]] = None,
        top_k: int = 3,
    ) -> List[Tuple[float, MemoryRecord]]:
        """按语义相似度召回记忆。"""
        self.ensure_loaded()
        wanted = set(kinds) if kinds else None
        query_vector = self.embedder.embed(query)
        scored: List[Tuple[float, MemoryRecord]] = []
        for record in self._records:
            if wanted and record.kind not in wanted:
                continue
            if project != "default" and record.project not in (project, "default"):
                continue
            score = cosine(query_vector, self._vector(record))
            if score >= 0.15:
                scored.append((score, record))
        scored.sort(key=lambda item: -item[0])
        return scored[:top_k]

    # ---------- 误报压制 ----------

    def suppression_check(self, project: str, issue: Issue, code: str = "") -> Tuple[bool, str]:
        """判断某条结论是否与历史误报高度一致，应当压制。

        规则（两条件需同时满足，避免误杀真实问题）：
        1. 指纹相同，**或**代码形态相似度超过阈值
        2. 该形态的历史驳回占比 ≥ ``suppress_threshold``，且样本数 ≥ ``suppress_min_reports``
        """
        self.ensure_loaded()
        snippet = code_at_lines(code, issue.line_start, issue.line_end)
        form = normalize_code_form(snippet)
        form_vector = self.embedder.embed(form) if form else []
        signature = issue_signature(issue)

        rejected = 0
        accepted = 0
        for record in self._records:
            if record.kind not in ("false_positive", "accepted"):
                continue
            # 项目隔离：不能拿 A 项目的误报记录去压制 B 项目的结论。
            # project="default" 视为全局共识，对所有项目生效。
            if project != "default" and record.project not in (project, "default"):
                continue
            same_form = bool(form) and cosine(form_vector, self._vector(record)) >= self.similarity_threshold
            if record.signature != signature and not same_form:
                continue
            if record.payload.get("accepted"):
                accepted += 1
            else:
                rejected += 1

        total = rejected + accepted
        if total < self.suppress_min_reports:
            return False, ""
        ratio = rejected / total
        if ratio >= self.suppress_threshold:
            return True, (
                f"历史反馈显示该形态（{issue.category.value}/{issue.subtype}）最近 {total} 次中出现 "
                f"{rejected} 次误报（误报率 {ratio:.0%}），已自动压制。"
                "如判定有误，请记录一次采纳反馈以恢复。"
            )
        return False, ""

    def apply_suppression(
        self, project: str, issues: Sequence[Issue], code: str = ""
    ) -> Tuple[List[Issue], List[Dict[str, Any]]]:
        """对报告批量应用记忆压制，返回 (保留的问题, 压制记录)。"""
        kept: List[Issue] = []
        suppressed: List[Dict[str, Any]] = []
        for issue in issues:
            drop, reason = self.suppression_check(project, issue, code)
            if drop:
                suppressed.append(
                    {
                        "subtype": issue.subtype,
                        "line": issue.line_start,
                        "reason": reason,
                    }
                )
                continue
            kept.append(issue)
        return kept, suppressed

    # ---------- 提示词上下文 ----------

    def build_context(self, code: str, project: str = "default", max_items: int = 3) -> str:
        """构建注入提示词的记忆段落（项目约定 + 历史误报提示）。"""
        conventions = self.recall(code, project=project, kinds=["convention"], top_k=max_items)
        false_positives = self.recall(code, project=project, kinds=["false_positive"], top_k=max_items)

        blocks: List[str] = []
        if conventions:
            lines = [f"- {record.text}" for _score, record in conventions if record.text]
            if lines:
                blocks.append("【项目约定 / 历史决策】\n" + "\n".join(lines))
        if false_positives:
            lines = []
            for _score, record in false_positives:
                subtype = record.payload.get("subtype", "")
                lines.append(f"- 曾经被驳回的结论：{subtype} — {record.text}")
            if lines:
                blocks.append(
                    "【历史误报提示】以下形态在本项目曾被判定为误报，请谨慎复核后再报告：\n"
                    + "\n".join(lines)
                )
        return "\n\n".join(blocks)

    def stats(self) -> Dict[str, Any]:
        self.ensure_loaded()
        by_kind: Dict[str, int] = {}
        by_project: Dict[str, int] = {}
        for record in self._records:
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
            by_project[record.project] = by_project.get(record.project, 0) + 1
        return {
            "path": str(self.path),
            "total": len(self._records),
            "by_kind": by_kind,
            "by_project": by_project,
        }
