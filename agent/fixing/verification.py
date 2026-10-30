"""修复验证管线（harness 侧强制执行）。

修复不能只凭「模型说改好了」，必须真的通过检查：

    语法检查 → 接口兼容性检查 → 沙箱行为测试

任一级失败：把失败详情回灌给模型反思重试；仍失败则回退原代码，绝不交付坏修复。
验证由 Python 执行，不依赖模型自觉——这是「Review Pipeline 与 Fix Pipeline 分离」的
关键：审查角色始终只读，只有 Fix 管线能拿到补丁、执行与测试能力。
"""

from __future__ import annotations

import ast
from typing import Optional

from agent.schema import VerificationReport
from agent.tools.base import ToolRegistry


def verify_optimization(
    original_code: str,
    optimized_code: str,
    registry: ToolRegistry,
    test_code: str = "",
    timeout: float = 10.0,
    enforce_signature: bool = True,
) -> VerificationReport:
    """执行三级验证，返回结构化验证报告。"""
    report = VerificationReport()

    # ---- 一级：语法 ----
    try:
        ast.parse(optimized_code)
        report.add("syntax", True, "ast.parse 通过")
    except SyntaxError as exc:
        report.add("syntax", False, f"第 {exc.lineno} 行语法错误：{exc.msg}")
        return report  # 语法都不对，后续检查无意义

    # ---- 二级：接口兼容性 ----
    signature_tool = registry.get("check_signature")
    if signature_tool is not None and enforce_signature:
        result = signature_tool(original_code=original_code, modified_code=optimized_code)
        if result.ok and isinstance(result.data, dict):
            compatible = bool(result.data.get("compatible"))
            detail_parts = []
            if result.data.get("removed_symbols"):
                detail_parts.append(f"被删除的符号: {result.data['removed_symbols']}")
            if result.data.get("changed_signatures"):
                names = [change.get("name") for change in result.data["changed_signatures"]]
                detail_parts.append(f"签名变更: {names}")
            report.add(
                "signature_compatibility",
                compatible,
                "；".join(detail_parts) or "公开接口保持不变",
                result.data,
            )
        else:
            report.add("signature_compatibility", False, result.error or "签名检查工具执行失败")
    else:
        report.add("signature_compatibility", True, "已跳过（未强制要求保持签名）")

    # ---- 三级：沙箱行为 ----
    if test_code.strip():
        run_tests = registry.get("run_tests")
        if run_tests is None:
            report.add("behavior_tests", True, "已跳过（沙箱不可用）")
        else:
            baseline = run_tests(solution_code=original_code, test_code=test_code, timeout=timeout)
            baseline_ok = bool(baseline.data and baseline.data.get("success"))
            report.add(
                "baseline_tests",
                True,
                f"原代码测试的退出码={baseline.data.get('exit_code') if baseline.data else 'n/a'}"
                "（仅作参照：若原代码本身就不过测试，说明测试用例可能不适用）",
                baseline.data,
            )
            fixed = run_tests(solution_code=optimized_code, test_code=test_code, timeout=timeout)
            fixed_data = fixed.data or {}
            passed = bool(fixed_data.get("success"))
            detail = "测试全部通过" if passed else (
                "测试失败：" + (fixed_data.get("stderr") or fixed_data.get("stdout") or "未知原因")
            )[:600]
            report.add("behavior_tests", passed, detail, fixed_data)
            if not baseline_ok:
                report.add(
                    "baseline_warning",
                    True,
                    "原代码在同一测试下也失败，结论仅以修复后是否通过为准。",
                )
    else:
        execute = registry.get("execute_python_code")
        if execute is None:
            report.add("smoke_execution", True, "已跳过（沙箱不可用）")
        else:
            # 非严格模式：被验证的是「用户既有代码 + 模型改动」，它可能本来就依赖 requests
            # 等第三方库，不应因为这类导入就直接判失败；阻断级危险调用仍会被拦截。
            smoke = execute(code=optimized_code, timeout=timeout, strict=False)
            data = smoke.data or {}
            blocked = bool(data.get("blocked"))
            ran_ok = data.get("exit_code") == 0 and not blocked

            if blocked:
                report.add(
                    "smoke_execution",
                    False,
                    "含阻断级危险调用，已拒绝执行：" + str(data.get("violations"))[:400],
                    data,
                )
            elif ran_ok:
                report.add("smoke_execution", True, "修复后代码可直接执行", data)
            else:
                # 与原代码做基线对比：很多文件在模块级就有副作用（读文件、连数据库、发请求），
                # 直接执行本来就会失败。如果原代码同样跑不通，就不应把账算到修复头上。
                baseline = execute(code=original_code, timeout=timeout, strict=False)
                baseline_ok = bool(baseline.data and baseline.data.get("exit_code") == 0)
                if not baseline_ok:
                    report.add(
                        "smoke_execution",
                        True,
                        "修复后代码与原代码一样无法直接执行（通常源于模块级副作用），"
                        "该检查不作为判定依据，请以 run_tests 的行为测试为准。",
                        data,
                    )
                else:
                    report.add(
                        "smoke_execution",
                        False,
                        "原代码可执行但修复后不可执行：" + str(data.get("stderr"))[:600],
                        data,
                    )
    return report


__all__ = ["verify_optimization"]
