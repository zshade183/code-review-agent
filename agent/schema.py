"""Agent 数据契约。

这里定义了整个 Agent 系统的「结构化输出协议」。所有 LLM 产出都必须先通过
Pydantic 校验才能进入下游；JSON 主体统一由 ``agent/runtime.py`` 的
``extract_json_block`` 提取。

核心模型关系::

    AgentRun ─┬─ AgentStep ─┬─ ToolCall
              │             └─ ToolResult
              └─ ReviewReport ─── Issue ─── Evidence

其中 ``Evidence`` 是「证据闭环」的载体：Agent 报告的任何问题都必须携带
可核验的依据（工具输出 / AST 节点 / 代码片段），否则会被验证器剔除。
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Category(str, Enum):
    """问题一级分类（与既有实验/数据集标签保持一致）。"""

    SECURITY = "Security Vulnerability"
    BUG = "Potential Bug"
    SMELL = "Code Smell"
    PERFORMANCE = "Performance Issue"


class Severity(str, Enum):
    HIGH = "High"
    MEDIUM_HIGH = "Medium-High"
    MEDIUM = "Medium"
    LOW = "Low"


class AdjudicationStatus(str, Enum):
    """候选问题的最终裁决状态（由确定性规则产出，不经过 LLM 决策）。"""

    #: 交叉验证通过，可直接展示
    CONFIRMED = "confirmed"
    #: 证据不足或角色间存在分歧，保留但提示人工复核
    NEEDS_REVIEW = "needs_review"
    #: 与历史误报形态高度一致，降权并单独分组（不直接删除）
    MEMORY_SUSPECTED = "memory_suspected"
    #: 多方一致认为不成立，移出最终报告
    REJECTED = "rejected"


class ReviewStrategy(str, Enum):
    """LangGraph 审查档位；决定初审预算和独立复核范围。"""

    #: 兼容低延迟客户端标记
    FAST = "fast"
    #: 默认标记
    AUTO = "auto"
    #: 初审后复核全部有效候选
    FULL = "full"


def normalize_strategy(value: Any, default: str = "auto") -> "ReviewStrategy":
    """把外部传入的策略字符串归一；兼容历史 ``mode=single/multi`` 写法。

    无法识别时回退到默认策略，而不是抛异常——入口层（CLI/API）不应因为
    一个可选字段拼错就把整次审查打回。
    """
    if isinstance(value, ReviewStrategy):
        return value
    text = str(value or "").strip().lower()
    aliases = {
        "single": ReviewStrategy.FAST,
        "single_agent": ReviewStrategy.FAST,
        "multi": ReviewStrategy.FULL,
        "multi_agent": ReviewStrategy.FULL,
        "all": ReviewStrategy.FULL,
    }
    if text in aliases:
        return aliases[text]
    for strategy in ReviewStrategy:
        if text == strategy.value:
            return strategy
    fallback = str(default or "auto").strip().lower()
    for strategy in ReviewStrategy:
        if fallback == strategy.value:
            return strategy
    return ReviewStrategy.AUTO



#: 严重性 -> 中文风险等级（兼容 VSCode 插件与既有前端）
SEVERITY_TO_RISK_LEVEL: Dict[Severity, str] = {
    Severity.HIGH: "高",
    Severity.MEDIUM_HIGH: "中",
    Severity.MEDIUM: "中",
    Severity.LOW: "低",
}

#: 各分类允许的二级分类白名单，用于约束 LLM 输出空间
SUBTYPE_WHITELIST: Dict[Category, tuple[str, ...]] = {
    Category.SECURITY: (
        "SQL Injection",
        "Command Injection",
        "Dangerous Function Abuse",
        "Hardcoded Sensitive Information",
        "Insecure Deserialization",
        "Cross-Site Scripting (XSS)",
    ),
    Category.BUG: (
        "Null Pointer/None Check Missing",
        "Boundary Condition Error",
        "Index Out of Bounds",
        "Logical Condition Error",
        "Parameter Position Error",
        "Type Error",
        "Division by Zero",
        "Return Value Error",
        "File Existence Check Missing",
    ),
    Category.SMELL: (
        "Unused Variable/Import",
        "Magic Number",
        "Improper Exception Handling",
        "Redundant Code/Dead Code",
        "Overly Long Function",
        "Too Many Parameters",
        "Non-Standard Naming",
        "Duplicate Code",
        "Unreleased Resource",
    ),
    Category.PERFORMANCE: (
        "Inefficient Loop",
        "Redundant Calculation",
        "Data Structure Misuse",
        "Redundant I/O/Database Query",
        "Memory Leak",
        "Unnecessary Deep Copy",
        "Inefficient Regular Expression",
    ),
}


#: 运行期允许集合缓存：静态白名单 + 人工晋升的候选子类型
_EXTRA_SUBTYPES: Optional[Dict[Category, set[str]]] = None


def refresh_allowed_subtypes() -> None:
    """清空运行期缓存，使下次校验重新读取已晋升的子类型。"""
    global _EXTRA_SUBTYPES
    _EXTRA_SUBTYPES = None


def _extra_subtypes() -> Dict[Category, set[str]]:
    """读取人工晋升的子类型（长期记忆 ``kind="subtype_whitelist"``）。

    这里延迟导入 ``MemoryStore``，避免 ``schema <- memory`` 的循环依赖；
    记忆不可用时静默退回静态白名单，核心校验始终可用。
    """
    global _EXTRA_SUBTYPES
    if _EXTRA_SUBTYPES is not None:
        return _EXTRA_SUBTYPES
    extra: Dict[Category, set[str]] = {}
    try:
        from agent.memory import MemoryStore

        for record in MemoryStore().records:
            if record.kind != "subtype_whitelist":
                continue
            subtype = str(record.payload.get("subtype") or record.text or "").strip()
            if subtype:
                extra.setdefault(_normalize_category(record.payload.get("category")), set()).add(subtype)
    except Exception:  # noqa: BLE001 - 记忆只是增强，失败时退回静态白名单
        extra = {}
    _EXTRA_SUBTYPES = extra
    return extra


def allowed_subtypes(category: Category) -> tuple[str, ...]:
    """某分类当前允许的二级分类：静态白名单 + 已晋升候选。"""
    if not isinstance(category, Category):
        category = _normalize_category(category)
    base = SUBTYPE_WHITELIST.get(category, ())
    extra = sorted(_extra_subtypes().get(category, set()) - set(base))
    return tuple(base) + tuple(extra)


def _normalize_category(value: Any) -> Category:
    """把 LLM 可能输出的各种写法归一到四大类。"""
    if isinstance(value, Category):
        return value
    text = str(value or "").strip()
    if text in {c.value for c in Category}:
        return Category(text)
    low = text.lower()
    if "security" in low or "安全" in text or "漏洞" in text:
        return Category.SECURITY
    if "performance" in low or "性能" in text:
        return Category.PERFORMANCE
    if "smell" in low or "style" in low or "规范" in text or "坏味" in text:
        return Category.SMELL
    return Category.BUG


def _normalize_severity(value: Any) -> Severity:
    if isinstance(value, Severity):
        return value
    text = str(value or "").strip()
    if text in {s.value for s in Severity}:
        return Severity(text)
    low = text.lower()
    if low in ("high", "严重", "高", "critical"):
        return Severity.HIGH
    if low in ("medium-high", "medium_high", "中高", "major"):
        return Severity.MEDIUM_HIGH
    if low in ("medium", "中", "moderate"):
        return Severity.MEDIUM
    if low in ("low", "低", "minor"):
        return Severity.LOW
    return Severity.MEDIUM


_LINE_TOKEN_RE = re.compile(r"\d+")


def _parse_line_range(value: Any) -> tuple[int, int]:
    """解析行号，支持 3 / "3" / "3,5" / "3-5" / "3~5" 等写法。"""
    if value is None:
        return 1, 1
    if isinstance(value, (list, tuple)):
        numbers = [int(v) for v in value if str(v).strip().isdigit()]
        if not numbers:
            return 1, 1
        return min(numbers), max(numbers)
    if isinstance(value, int):
        return value, value
    tokens = [int(m.group(0)) for m in _LINE_TOKEN_RE.finditer(str(value))]
    if not tokens:
        return 1, 1
    return min(tokens), max(tokens)


#: 模型自报的 evidence.kind 常见写法 → 契约枚举的映射。
#: 不在这里做容错的话，模型写 ``"kind": "tool"`` 会让整条 issue 校验失败并被静默丢弃。
_EVIDENCE_KIND_ALIASES = {
    "tool": "tool_output",
    "tool_call": "tool_output",
    "tool_result": "tool_output",
    "tool_output": "tool_output",
    "static_analysis": "tool_output",
    "observation": "tool_output",
    "ast": "ast_node",
    "ast_node": "ast_node",
    "code": "code_snippet",
    "snippet": "code_snippet",
    "source": "code_snippet",
    "code_snippet": "code_snippet",
    "rule": "rule",
    "lint": "rule",
    "regex": "rule",
    "convention": "rule",
    "test": "test_result",
    "test_result": "test_result",
    "runtime": "test_result",
}


class Evidence(BaseModel):
    """问题证据：Agent 必须给出「凭什么这么判断」的可核验依据。"""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["tool_output", "ast_node", "code_snippet", "rule", "test_result"] = "code_snippet"
    evidence_id: str = Field(default="", description="对应本次请求中的证据记录 ID")
    tool: Optional[str] = Field(default=None, description="产出该证据的工具名")
    line: Optional[int] = Field(default=None, description="证据所在行号")
    snippet: str = Field(default="", description="代码片段或工具原始输出片段")
    detail: str = Field(default="", description="证据如何支撑该结论")

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: Any) -> str:
        """把模型自由填写的证据类型归一到契约枚举，避免整条结论被丢弃。"""
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not text:
            return "code_snippet"
        if text in _EVIDENCE_KIND_ALIASES:
            return _EVIDENCE_KIND_ALIASES[text]
        if "tool" in text or "static" in text:
            return "tool_output"
        if "ast" in text or "node" in text:
            return "ast_node"
        if "test" in text:
            return "test_result"
        if "rule" in text or "lint" in text:
            return "rule"
        return "code_snippet"

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value: Any) -> Optional[int]:
        """兼容 ``"第 3 行"``、``"3-5"`` 这类行号写法。"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return None
        match = re.search(r"\d+", text)
        return int(match.group(0)) if match else None

    @field_validator("evidence_id", "snippet", "detail", mode="before")
    @classmethod
    def _coerce_text(cls, value: Any) -> str:
        """把模型偶发的对象/数组字段转成字符串，避免类型错误拖垮整条结论。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    @field_validator("tool", mode="before")
    @classmethod
    def _coerce_tool(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def is_verifiable(self) -> bool:
        """证据是否可用作核验（至少有工具来源或代码片段）。"""
        return bool(self.tool) or bool(self.snippet.strip())


class Issue(BaseModel):
    """单条代码问题。字段名兼容既有 REST API / VSCode 插件输出。"""

    model_config = ConfigDict(extra="ignore")

    category: Category
    severity: Severity
    subtype: str
    #: 规范外问题的建议分类名。subtype 无法落入白名单时填写，作为白名单候选保留
    suggested_subtype: str = ""
    line_start: int = Field(default=1, ge=1)
    line_end: int = Field(default=1, ge=1)
    violated_rule: str = ""
    risk_description: str = ""
    fix_suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: List[Evidence] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    #: 由哪个 Agent 角色产出（多智能体场景下用于溯源）
    produced_by: str = "reviewer"
    #: 验证器给出的结论，None 表示未验证
    verified: Optional[bool] = None
    verify_note: str = ""
    #: 多智能体场景下各角色结论存在冲突
    disagreement: bool = False
    #: 稳定的候选标识：合并、裁决与前端回传反馈都以它为锚点
    candidate_id: str = ""
    #: 产出该候选的角色列表（确定性合并后可能来自多个领域 Reviewer）
    sources: List[str] = Field(default_factory=list)
    #: 最终裁决状态，None 表示尚未进入裁决阶段
    adjudication: Optional[AdjudicationStatus] = None
    adjudication_reason: str = ""

    # ---------- 归一化 ----------

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """兼容旧格式：line_number / dimension / risk_level 等别名。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data["category"] = _normalize_category(
            data.get("category") or data.get("dimension")
        )
        data["severity"] = _normalize_severity(
            data.get("severity") or data.get("risk_level")
        )

        if "line_start" not in data and "line_end" not in data:
            start, end = _parse_line_range(
                data.get("line_number") or data.get("line") or data.get("lines")
            )
            data["line_start"], data["line_end"] = start, end
        else:
            start, end = _parse_line_range(data.get("line_start"))
            if data.get("line_end") is None:
                data["line_end"] = start
            else:
                end = _parse_line_range(data.get("line_end"))[0]
            data["line_start"], data["line_end"] = min(start, end), max(start, end)

        if not data.get("subtype") or str(data.get("subtype")).strip() == "Unspecified":
            data["subtype"] = (
                str(data.get("suggested_subtype") or "").strip()
                or str(data.get("violated_rule") or "").strip()
            )
        if not data.get("risk_description"):
            data["risk_description"] = str(
                data.get("description") or data.get("risk") or ""
            ).strip()
        if not data.get("fix_suggestion"):
            data["fix_suggestion"] = str(data.get("fix") or data.get("suggestion") or "").strip()

        # 兼容模型把 evidence 写成字符串/对象而非数组的情况，避免整条结论校验失败被丢弃
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            text = evidence.strip()
            data["evidence"] = [{"detail": text}] if text else []
        elif isinstance(evidence, dict):
            data["evidence"] = [evidence]
        elif isinstance(evidence, list):
            data["evidence"] = [
                {"detail": item.strip()}
                if isinstance(item, str) and item.strip()
                else item
                for item in evidence
                if not isinstance(item, str) or item.strip()
            ]
        elif evidence is None:
            data["evidence"] = []

        refs = data.get("evidence_refs")
        if isinstance(refs, str):
            refs = refs.strip()
            data["evidence_refs"] = [refs] if refs else []
        return data

    @field_validator("subtype")
    @classmethod
    def _keep_subtype_readable(cls, value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text or "Unspecified"

    @field_validator("suggested_subtype")
    @classmethod
    def _keep_suggested_readable(cls, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    # ---------- 派生属性 ----------

    @property
    def line_number(self) -> str:
        """兼容既有接口的字符串行号（多行用逗号分隔）。"""
        if self.line_end > self.line_start:
            return f"{self.line_start},{self.line_end}"
        return str(self.line_start)

    @property
    def risk_level(self) -> str:
        return SEVERITY_TO_RISK_LEVEL.get(self.severity, "中")

    @property
    def dimension(self) -> str:
        return self.category.value

    def is_subtype_valid(self) -> bool:
        """subtype 与建议分类是否都落在当前允许集合内（含已晋升项）。"""
        allowed = allowed_subtypes(self.category)
        if self.subtype not in allowed:
            return False
        return not self.suggested_subtype or self.suggested_subtype in allowed

    def identity(self) -> tuple[str, str, int]:
        """去重用的身份标识。"""
        return (self.category.value, self.subtype, self.line_start)

    def ensure_candidate_id(self, suffix: str = "") -> str:
        """返回稳定的候选标识（同一问题在不同角色/合并后保持一致）。

        标识只由「分类 + 二级分类 + 起始行」决定（可选附加后缀），**不包含产出角色**：
        同一问题被不同领域 Reviewer 重复报告时必须落到同一个 ``candidate_id``，
        否则确定性合并就无法去重，前端反馈也会指向不同的候选。
        """
        if not self.candidate_id:
            parts = [self.category.value, self.subtype, str(self.line_start)]
            if suffix:
                parts.append(suffix)
            raw = "|".join(parts)
            self.candidate_id = "cand-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return self.candidate_id

    def to_legacy(self) -> Dict[str, Any]:
        """转换为既有 REST API / 插件期望的扁平字典。"""
        return {
            "category": self.category.value,
            "dimension": self.category.value,
            "severity": self.severity.value,
            "subtype": self.subtype,
            "subtype_valid": self.is_subtype_valid(),
            "suggested_subtype": self.suggested_subtype,
            "line_number": self.line_number,
            "violated_rule": self.violated_rule or self.subtype,
            "risk_level": self.risk_level,
            "risk_description": self.risk_description,
            "fix_suggestion": self.fix_suggestion,
            "confidence": self.confidence,
            "produced_by": self.produced_by,
            "verified": self.verified,
            "verify_note": self.verify_note,
            "disagreement": self.disagreement,
            "candidate_id": self.ensure_candidate_id(),
            "sources": list(self.sources),
            "adjudication": self.adjudication.value if self.adjudication else None,
            "adjudication_reason": self.adjudication_reason,
            "evidence": [e.model_dump() for e in self.evidence],
            "evidence_refs": list(self.evidence_refs),
        }


class FindingCandidate(BaseModel):
    """领域 Reviewer 的产出契约。

    领域 Reviewer 不直接产出最终报告，而是输出「候选问题 + 来源角色 + 证据引用」，
    交给确定性的 Candidate Merger 统一规范化、去重与冲突检测。这样同一份输入无论
    哪个角色先返回、并发顺序如何变化，合并结果都一致。
    """

    model_config = ConfigDict(extra="ignore")

    #: 产出角色的稳定标识：security / bug / quality / performance / single_agent
    reviewer: str
    issue: Issue
    #: 该候选引用的共享证据键（如 "syntax"、"rules:12"），便于回溯「凭什么这么判断」
    evidence_refs: List[str] = Field(default_factory=list)
    #: Reviewer 的自述理由（评审过程说明，不进入最终报告）
    notes: str = ""
    #: 该候选在 Reviewer 原始输出中的序号
    raw_index: int = 0

    def to_issue(self) -> Issue:
        """转成带来源标注的 ``Issue``。"""
        issue = self.issue.model_copy(deep=True)
        issue.produced_by = self.reviewer
        if self.reviewer not in issue.sources:
            issue.sources = [self.reviewer, *issue.sources]
        issue.ensure_candidate_id()
        return issue


class ReviewReport(BaseModel):
    """审查 Agent 的最终结构化产出。"""

    model_config = ConfigDict(extra="ignore")

    review_summary: str = ""
    issues: List[Issue] = Field(default_factory=list)

    @property
    def high_risk_count(self) -> int:
        return sum(1 for i in self.issues if i.risk_level == "高")

    def dedupe(self) -> "ReviewReport":
        """按 (分类, 二级分类, 起始行) 去重，保留置信度更高的一条。"""
        best: Dict[tuple[str, str, int], Issue] = {}
        for issue in self.issues:
            key = issue.identity()
            current = best.get(key)
            if current is None or issue.confidence > current.confidence:
                best[key] = issue
        return ReviewReport(review_summary=self.review_summary, issues=list(best.values()))

    def to_legacy(self) -> Dict[str, Any]:
        return {
            "review_summary": self.review_summary,
            "issues": [i.to_legacy() for i in self.issues],
        }


class VerificationCheck(BaseModel):
    """一条验证检查的结果。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    passed: bool
    detail: str = ""
    data: Any = None


class VerificationReport(BaseModel):
    """优化的三级验证报告：语法 -> 接口兼容 -> 沙箱行为。"""

    model_config = ConfigDict(extra="ignore")

    checks: List[VerificationCheck] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failed_names(self) -> List[str]:
        return [c.name for c in self.checks if not c.passed]

    def add(self, name: str, passed: bool, detail: str = "", data: Any = None) -> "VerificationReport":
        self.checks.append(VerificationCheck(name=name, passed=passed, detail=detail, data=data))
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed_names,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


class OptimizationResult(BaseModel):
    """优化 Agent 的最终产出。"""

    model_config = ConfigDict(extra="ignore")

    original_code: str = ""
    optimized_code: str = ""
    patch: str = ""
    changed: bool = False
    change_ratio: float = 0.0
    verification: VerificationReport = Field(default_factory=VerificationReport)
    attempts: int = 0
    summary: str = ""
    addressed_issues: List[str] = Field(default_factory=list)
    api_ok: bool = True
    used_fallback: bool = False

    @property
    def signature_match(self) -> bool:
        """兼容既有接口：接口兼容性检查是否通过。"""
        for check in self.verification.checks:
            if check.name == "signature_compatibility":
                return check.passed
        return False

    @property
    def syntax_valid(self) -> bool:
        for check in self.verification.checks:
            if check.name == "syntax":
                return check.passed
        return False

    def to_legacy(self) -> Dict[str, Any]:
        """转换为既有 API / CLI / 实验脚本期望的字典结构。"""
        return {
            "optimized_code": self.optimized_code,
            "signature_match": self.signature_match,
            "optimization_summary": self.summary,
            "api_ok": self.api_ok,
            "used_fallback": self.used_fallback,
            "changed": self.changed,
            "patch": self.patch,
            "verification": self.verification.to_dict(),
            "attempts": self.attempts,
        }


class DimensionCoverage(BaseModel):
    """单个审查维度（或角色）的执行覆盖情况。"""

    model_config = ConfigDict(extra="ignore")

    dimension: str
    status: Literal["completed", "timeout", "error", "skipped", "not_required"] = "skipped"
    detail: str = ""
    elapsed_ms: float = 0.0
    issues: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {"completed", "not_required"}


class CoverageReport(BaseModel):
    """一次审查的覆盖度报告：用于表达「部分成功」而不是整体失败。"""

    model_config = ConfigDict(extra="ignore")

    strategy: str = "auto"
    dimensions: Dict[str, DimensionCoverage] = Field(default_factory=dict)

    @property
    def partial(self) -> bool:
        """任一维度未完成即为部分成功。"""
        return any(not item.ok for item in self.dimensions.values())

    @property
    def incomplete(self) -> List[str]:
        return [name for name, item in self.dimensions.items() if not item.ok]

    def mark(
        self,
        dimension: str,
        status: str,
        detail: str = "",
        elapsed_ms: float = 0.0,
        issues: int = 0,
    ) -> "CoverageReport":
        self.dimensions[dimension] = DimensionCoverage(
            dimension=dimension,
            status=status,  # type: ignore[arg-type]
            detail=detail,
            elapsed_ms=round(elapsed_ms, 2),
            issues=issues,
        )
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "partial": self.partial,
            "incomplete": self.incomplete,
            "dimensions": {
                name: item.model_dump() for name, item in self.dimensions.items()
            },
        }


class TokenUsage(BaseModel):
    """token 用量（用于成本统计）。"""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def accumulate(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


class ToolCall(BaseModel):
    """LLM 发起的一次工具调用请求。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    #: 原始 arguments 字符串，解析失败时保留以便排查
    raw_arguments: str = ""
    parse_error: str = ""

    def signature(self) -> str:
        """用于重复动作检测的签名。"""
        import json

        try:
            payload = json.dumps(self.arguments, sort_keys=True, ensure_ascii=False)
        except Exception:
            payload = self.raw_arguments
        return f"{self.name}:{payload}"


class ToolResult(BaseModel):
    """工具执行结果（作为 Observation 回填给 LLM）。"""

    model_config = ConfigDict(extra="ignore")

    tool: str
    ok: bool = True
    content: str = ""
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0
    from_cache: bool = False
    error_type: str = ""
    retryable: bool = False
    attempts: int = 1

    def to_observation(self, max_chars: int = 4000) -> str:
        """转成喂给 LLM 的观察文本。"""
        text = self.content or ""
        if not self.ok:
            text = f"[工具执行失败] type={self.error_type or 'execution_error'} retryable={self.retryable} attempts={self.attempts}: {self.error or text}"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [输出被截断，原始长度 {len(text)} 字符]"
        return text


class AgentStep(BaseModel):
    """Agent Loop 中的一步。"""

    model_config = ConfigDict(extra="ignore")

    index: int
    #: think（模型思考/文本） | act（工具调用） | observe（工具结果） | final（最终产出）
    kind: Literal["think", "act", "observe", "final", "error"]
    content: str = ""
    tool_call: Optional[ToolCall] = None
    tool_result: Optional[ToolResult] = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    timestamp: float = 0.0

    def brief(self, max_chars: int = 160) -> str:
        text = re.sub(r"\s+", " ", self.content or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        if self.kind == "act" and self.tool_call:
            args = re.sub(r"\s+", " ", str(self.tool_call.arguments))
            return f"call {self.tool_call.name}({args[:max_chars]})"
        if self.kind == "observe" and self.tool_result:
            status = "ok" if self.tool_result.ok else "fail"
            return f"obs[{status}] {text}"
        return text


class AgentRun(BaseModel):
    """一次完整 Agent 运行的结果与轨迹。"""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    task: str = "review"
    steps: List[AgentStep] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_ms: float = 0.0
    stop_reason: str = ""
    report: Optional[ReviewReport] = None
    #: 非审查类任务（如优化）的自由产出
    output: Optional[Dict[str, Any]] = None
    error: str = ""

    # ---------- 统计 ----------

    @property
    def tool_calls(self) -> List[ToolCall]:
        return [s.tool_call for s in self.steps if s.kind == "act" and s.tool_call]

    @property
    def tool_names(self) -> List[str]:
        return [t.name for t in self.tool_calls]

    @property
    def llm_calls(self) -> int:
        return sum(1 for s in self.steps if s.usage.total_tokens > 0)

    def tool_call_stats(self) -> Dict[str, int]:
        stats: Dict[str, int] = {}
        for name in self.tool_names:
            stats[name] = stats.get(name, 0) + 1
        return stats

    def to_trace_dict(self) -> Dict[str, Any]:
        """给前端 / 评测模块消费的轨迹视图。"""
        return {
            "run_id": self.run_id,
            "task": self.task,
            "stop_reason": self.stop_reason,
            "duration_ms": round(self.duration_ms, 2),
            "usage": self.usage.model_dump(),
            "steps": len(self.steps),
            "tool_calls": self.tool_call_stats(),
            "timeline": [
                {
                    "index": s.index,
                    "kind": s.kind,
                    "brief": s.brief(),
                    "latency_ms": round(s.latency_ms, 2),
                }
                for s in self.steps
            ],
        }
