"""优化 Agent：带三级验证闭环的代码修复。

修复不能只凭「模型说改好了」，必须真的通过检查：

    生成修复代码 → 语法检查 → 接口兼容性检查 → 沙箱行为测试
                      ↑                                    │
                      └──────── 失败则带上错误信息反思重试 ────┘

关键设计：**验证由 harness 强制执行**。模型即便忘记
验证，最终产出也必须通过验证管线；不通过则退回原代码（避免交付坏修复）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from agent.llm import LLMClient
from agent.runtime import AgentConfig, AgentRuntime, RunOutcome
from agent.schema import (
    Issue,
    OptimizationResult,
    ReviewReport,
    VerificationReport,
)
from agent.tools import build_optimizer_registry
from agent.tools.base import ToolRegistry
from agent.tools.patch import make_unified_diff, patch_stats
from agent.fixing.verification import verify_optimization
from agent.trace import TraceRecorder

logger = logging.getLogger(__name__)

try:
    from core.prompts import OPTIMIZER_ROLE, OPTIMIZE_RULES
except Exception:  # pragma: no cover - 兼容独立运行
    OPTIMIZER_ROLE = "你是一名专业的 Python 代码优化专家。"
    OPTIMIZE_RULES = ""


OPTIMIZER_SYSTEM_PROMPT = f"""
{OPTIMIZER_ROLE}

你是一个**代码优化 Agent**。你可以调用工具来验证自己的修复，必须做到「改完自己先跑一遍」。

工作方式：
1. 【理解】阅读给出的问题清单，必要时用 `get_ast_outline` / `find_symbol` 确认问题位置与作用域。
2. 【修复】直接给出修复后的完整代码。
3. 【自验证】在给出最终答案前，**务必**依次调用：
   - `check_syntax`：确认没有语法错误
   - `check_signature`：确认没有破坏函数名/参数列表（除非问题本身要求修改签名）
   - `run_tests` 或 `execute_python_code`：真实运行一次，确认行为正确
4. 【迭代】若验证失败，根据报错修正后重新验证，不要带着已知失败交付。

{OPTIMIZE_RULES}

【硬性约束】
- 保持最小修改：只改与问题相关的行，不要顺带重排格式、改名、删注释。
- 不要新增顶层函数或类（除非问题描述明确要求）。
- 最终输出**只包含**一个 ```python 代码块，里面是完整的、可直接运行的优化后代码。
  不要输出 diff、不要输出解释文字。
- 如果你的修复经过验证仍无法通过，请如实说明并保留原逻辑，不要为了"看起来修好了"而破坏功能。
"""

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
#: 用于判断一段文本「像不像纯代码」
_CODE_HINT_RE = re.compile(r"^\s*(def|class|import|from|@)\s", re.M)


def build_optimizer_prompt(code: str, issues_text: str, test_code: str = "") -> str:
    """组装优化 Agent 的用户提示词。"""
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    numbered = "\n".join(f"{index + 1}| {line}" for index, line in enumerate(lines))
    test_section = ""
    if test_code.strip():
        test_section = (
            "\n【验收测试】修复后的代码必须通过以下测试（可用 `run_tests` 工具验证）：\n"
            "```python\n"
            f"{test_code}\n"
            "```\n"
        )
    return f"""请对以下代码进行修复优化。

【已识别的问题】
{issues_text}

【待修复代码】（左侧为行号）
```python
{numbered}
```
{test_section}
要求：
1. 逐条修复上述问题，但保持最小修改，不要改动无关代码。
2. 保持所有函数名与参数列表不变。
3. 在输出最终代码前，先调用工具完成语法检查、签名检查与运行验证。
4. 最终只输出一个 ```python 代码块，包含完整的优化后代码。
"""


def extract_code(text: str) -> str:
    """从模型输出中提取代码。

    依次尝试：JSON 的 optimized_code 字段 -> ```python 围栏 -> 整体像代码 -> 空串。
    与旧实现不同的是，这里**失败时返回空串**，让上层明确报错，而不是把解释文本
    当成代码返回给用户。
    """
    if not text:
        return ""
    stripped = text.strip()

    if stripped.startswith("{"):
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(stripped[start : end + 1])
                if isinstance(payload, dict):
                    for key in ("optimized_code", "fixed_code", "code"):
                        value = payload.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
            except json.JSONDecodeError:
                pass

    fenced = _CODE_FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()

    if _CODE_HINT_RE.search(stripped):
        return stripped
    return ""


class OptimizerAgent:
    """代码优化 Agent。"""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        codebase_root: Optional[str] = None,
        max_verify_retries: int = 2,
        fallback_to_original: bool = True,
        enforce_signature: bool = True,
    ) -> None:
        self.client = client or LLMClient()
        self.registry = registry or build_optimizer_registry(codebase_root)
        self.config = config or AgentConfig()
        self.max_verify_retries = max_verify_retries
        self.fallback_to_original = fallback_to_original
        self.enforce_signature = enforce_signature

    # ------------------------------------------------------------------
    # 验证管线（harness 侧强制执行）
    # ------------------------------------------------------------------

    def verify(
        self,
        original_code: str,
        optimized_code: str,
        test_code: str = "",
        timeout: float = 10.0,
    ) -> VerificationReport:
        """执行三级验证（实现见 ``agent/fixing/verification.py``）。"""
        return verify_optimization(
            original_code,
            optimized_code,
            registry=self.registry,
            test_code=test_code,
            timeout=timeout,
            enforce_signature=self.enforce_signature,
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def optimize(
        self,
        code: str,
        review: ReviewReport | Dict[str, Any] | None,
        test_code: str = "",
        trace: Optional[TraceRecorder] = None,
        config: Optional[AgentConfig] = None,
    ) -> Tuple[OptimizationResult, RunOutcome]:
        cfg = config or self.config
        recorder = trace or TraceRecorder(task="optimize")
        runtime = AgentRuntime(self.client, self.registry, cfg, recorder)

        report = _coerce_report(review)
        issues_text = _format_issues(report)
        holder: Dict[str, Any] = {"attempts": 0}

        def finalizer(text: str) -> Tuple[Optional[OptimizationResult], str]:
            holder["attempts"] += 1
            attempts = holder["attempts"]

            candidate = extract_code(text)
            if not candidate:
                return None, (
                    "未能从输出中提取到代码。请只输出优化后的完整代码，"
                    "并使用 ```python 围栏包裹。"
                )

            verification = self.verify(code, candidate, test_code=test_code)
            if verification.passed or attempts > self.max_verify_retries:
                return self._finalize(code, candidate, verification, attempts, report), ""

            # 验证未通过 -> 把失败信息反馈给模型，让它带着错误重试
            failures = [
                f"- {check.name}: {check.detail}" for check in verification.checks if not check.passed
            ]
            return None, (
                "你给出的修复代码未通过验证，请修正后重新输出完整代码：\n"
                + "\n".join(failures)
                + "\n\n注意：\n"
                "1. 必须保持原有函数名与参数列表不变（除非问题本身要求改）\n"
                "2. 修复后代码必须能通过上述全部检查\n"
                "3. 只输出 ```python 代码块，不要输出解释"
            )

        outcome = runtime.run(
            build_optimizer_prompt(code, issues_text, test_code),
            system_prompt=OPTIMIZER_SYSTEM_PROMPT,
            finalizer=finalizer,
            task="optimize",
        )

        result = outcome.value if isinstance(outcome.value, OptimizationResult) else None
        if result is None:
            # 兜底：未产出可用结果时返回原代码，避免交付未经验证的改动
            result = OptimizationResult(
                original_code=code,
                optimized_code=code,
                changed=False,
                verification=VerificationReport(),
                attempts=holder["attempts"],
                summary=f"优化未产出可用结果（stop_reason={outcome.run.stop_reason}），已保留原代码。",
                api_ok=outcome.run.stop_reason != "llm_error",
                used_fallback=True,
            )
        return result, outcome

    # ------------------------------------------------------------------

    def _finalize(
        self,
        original: str,
        candidate: str,
        verification: VerificationReport,
        attempts: int,
        report: ReviewReport,
    ) -> OptimizationResult:
        patch = make_unified_diff(original, candidate, "snippet.py")
        stats = patch_stats(patch, max(1, len(original.split("\n"))))
        changed = original.strip() != candidate.strip()

        if verification.passed:
            final_code = candidate
            used_fallback = False
            summary = (
                f"修复完成，通过全部验证（{len(verification.checks)} 项）；"
                f"改动 +{stats.added}/-{stats.removed} 行"
            )
        elif self.fallback_to_original:
            final_code = original
            used_fallback = True
            summary = (
                "修复代码未通过验证（"
                + "、".join(verification.failed_names)
                + "），已回退保留原代码以避免引入新缺陷。"
            )
        else:
            final_code = candidate
            used_fallback = True
            summary = "修复代码未通过验证（" + "、".join(verification.failed_names) + "），按配置仍然返回。"

        if stats.change_ratio > 0.6:
            summary += "；注意改动比例较高，建议人工复核是否超出最小修改范围。"

        return OptimizationResult(
            original_code=original,
            optimized_code=final_code,
            patch=patch if final_code == candidate else "",
            changed=changed and final_code == candidate,
            change_ratio=round(stats.change_ratio, 4),
            verification=verification,
            attempts=attempts,
            summary=summary,
            addressed_issues=[f"{i.category.value}/{i.subtype}@行{i.line_start}" for i in report.issues],
            api_ok=True,
            used_fallback=used_fallback,
        )


def _coerce_report(review: ReviewReport | Dict[str, Any] | None) -> ReviewReport:
    if review is None:
        return ReviewReport(review_summary="", issues=[])
    if isinstance(review, ReviewReport):
        return review
    if isinstance(review, dict):
        try:
            return ReviewReport.model_validate(review)
        except Exception:  # noqa: BLE001
            issues = review.get("issues") or []
            parsed: List[Issue] = []
            for item in issues:
                try:
                    parsed.append(Issue.model_validate(item))
                except Exception:
                    continue
            return ReviewReport(review_summary=str(review.get("review_summary") or ""), issues=parsed)
    return ReviewReport(review_summary="", issues=[])


def _format_issues(report: ReviewReport) -> str:
    if not report.issues:
        return "（无明确问题清单，请基于代码语义进行安全性与正确性加固）"
    lines: List[str] = []
    for index, issue in enumerate(report.issues, start=1):
        lines.append(
            f"{index}. 【{issue.category.value} / {issue.severity.value}】"
            f"行 {issue.line_start}-{issue.line_end} {issue.subtype}："
            f"{issue.risk_description}。建议：{issue.fix_suggestion}"
        )
    return "\n".join(lines)
