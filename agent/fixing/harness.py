"""Fix Agent 外部的确定性交付 Harness。

模型只能产生候选代码；Harness 独占验证、diff、改动统计与失败回退，模型无法跳过。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.fixing.verification import verify_optimization
from agent.schema import OptimizationResult, ReviewReport, VerificationReport
from agent.tools.base import ToolRegistry
from agent.tools.patch import make_unified_diff, patch_stats


@dataclass(frozen=True)
class HarnessDecision:
    verification: VerificationReport
    accepted: bool
    feedback: str = ""


class FixHarness:
    """验证候选修复并决定交付候选代码还是安全回退。"""

    '''
    初始化 Fix Harness。
    :param registry: 用于验证和交付的工具集。
    :param enforce_signature: 是否强制验证签名。
    :param fallback_to_original: 是否在验证失败时回退到原始代码。
    :param high_change_ratio: 高改动率阈值。
    '''
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        enforce_signature: bool = True,
        fallback_to_original: bool = True,
        high_change_ratio: float = 0.6,
    ) -> None:
        self.registry = registry
        self.enforce_signature = enforce_signature
        self.fallback_to_original = fallback_to_original
        self.high_change_ratio = high_change_ratio

    '''
    验证候选修复。
    :param original_code: 原始代码。
    :param candidate_code: 候选修复代码。
    :param test_code: 测试代码。
    :param timeout: 超时时间。
    :return: 验证决策。
    '''
    def validate(
        self,
        original_code: str,
        candidate_code: str,
        *,
        test_code: str = "",
        timeout: float = 10.0,
    ) -> HarnessDecision:
        verification = verify_optimization(
            original_code,
            candidate_code,
            registry=self.registry,
            test_code=test_code,
            timeout=timeout,
            enforce_signature=self.enforce_signature,
        )
        failures = [
            f"- {check.name}: {check.detail}"
            for check in verification.checks
            if not check.passed
        ]
        feedback = ""
        if failures:
            feedback = (
                "候选代码未通过 Harness，必须修正后重新输出完整代码：\n"
                + "\n".join(failures)
                + "\n\n保持原有公开接口，只修改选中问题，并输出完整 Python 代码。"
            )
        return HarnessDecision(verification, verification.passed, feedback)

    '''
    交付候选修复。
    :param original_code: 原始代码。
    :param candidate_code: 候选修复代码。
    :param decision: 验证决策。
    :param attempts: 尝试次数。
    :param report: 审查报告。
    :return: 优化结果。
    '''
    def deliver(
        self,
        original_code: str,
        candidate_code: str,
        decision: HarnessDecision,
        *,
        attempts: int,
        report: ReviewReport,
    ) -> OptimizationResult:
        patch = make_unified_diff(original_code, candidate_code, "snippet.py")
        stats = patch_stats(patch, max(1, len(original_code.split("\n"))))
        changed = original_code.strip() != candidate_code.strip()

        if decision.accepted:
            final_code = candidate_code
            used_fallback = False
            summary = (
                f"修复完成，通过全部验证（{len(decision.verification.checks)} 项）；"
                f"改动 +{stats.added}/-{stats.removed} 行"
            )
        elif self.fallback_to_original:
            final_code = original_code
            used_fallback = True
            summary = (
                "修复代码未通过验证（"
                + "、".join(decision.verification.failed_names)
                + "），已回退保留原代码以避免引入新缺陷。"
            )
        else:
            final_code = candidate_code
            used_fallback = True
            summary = (
                "修复代码未通过验证（"
                + "、".join(decision.verification.failed_names)
                + "），按配置仍然返回。"
            )

        if stats.change_ratio > self.high_change_ratio:
            summary += "；注意改动比例较高，建议人工复核是否超出最小修改范围。"

        return OptimizationResult(
            original_code=original_code,
            optimized_code=final_code,
            patch=patch if final_code == candidate_code else "",
            changed=changed and final_code == candidate_code,
            change_ratio=round(stats.change_ratio, 4),
            verification=decision.verification,
            attempts=attempts,
            summary=summary,
            addressed_issues=[
                f"{issue.category.value}/{issue.subtype}@行{issue.line_start}"
                for issue in report.issues
            ],
            api_ok=True,
            used_fallback=used_fallback,
        )


__all__ = ["FixHarness", "HarnessDecision"]
