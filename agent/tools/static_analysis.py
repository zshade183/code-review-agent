"""静态分析工具。

把三种「取证手段」包装成 LLM 可调用的工具：

1. 项目自研的规则引擎（``core.reviewer.rule_based_review``）—— 规则型检查
2. ``pylint`` —— 通用规范与错误检查
3. ``bandit`` —— 安全漏洞检查

三路结果统一成同一套 ``Issue`` 契约，由模型按需调用：可以只调其中一个，
也可以全部调用后交叉验证。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from agent import settings
from agent.schema import Category, Evidence, Issue, Severity, ToolResult
from agent.tools.base import Tool

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# pylint 消息 -> (分类, 二级分类, 严重性)
# ----------------------------------------------------------------------------
# 键必须是**当前安装的 pylint 真实存在**的消息名。名字写错时，pylint 会退化成
# W0012 `unknown-option-value` 配置告警——它说的是调用参数，不是被审代码，会被误收成问题。
# `_pylint_enable_args()` 在运行时再与消息表对齐一次，升级 pylint 后也不会把这类假告警带进结果。
_PYLINT_SECURITY = {
    "eval-used": "Dangerous Function Abuse",
    "exec-used": "Dangerous Function Abuse",
    "anomalous-backslash-in-string": "Dangerous Function Abuse",
}

_PYLINT_PERFORMANCE = {
    "consider-using-generator": "Inefficient Loop",
    "consider-using-dict-items": "Inefficient Loop",
    "consider-using-enumerate": "Inefficient Loop",
    "consider-using-set-comprehension": "Inefficient Loop",
    "consider-using-dict-comprehension": "Inefficient Loop",
    "unnecessary-comprehension": "Inefficient Loop",
    "consider-using-f-string": "Redundant Calculation",
    "consider-using-join": "Redundant Calculation",
    "chained-comparison": "Redundant Calculation",
    "unnecessary-lambda": "Redundant Calculation",
    "consider-using-in": "Data Structure Misuse",
    "use-maxsplit-arg": "Data Structure Misuse",
    "use-dict-literal": "Data Structure Misuse",
    "consider-using-with": "Redundant I/O/Database Query",
}

_PYLINT_SMELL = {
    "unused-import": "Unused Variable/Import",
    "unused-variable": "Unused Variable/Import",
    "unused-argument": "Unused Variable/Import",
    "unused-wildcard-import": "Unused Variable/Import",
    "redefined-builtin": "Non-Standard Naming",
    "redefined-outer-name": "Non-Standard Naming",
    "invalid-name": "Non-Standard Naming",
    "pointless-statement": "Redundant Code/Dead Code",
    "unreachable": "Redundant Code/Dead Code",
    "duplicate-code": "Duplicate Code",
    "too-many-arguments": "Too Many Parameters",
    "too-many-locals": "Too Many Parameters",
    "too-many-branches": "Overly Long Function",
    "too-many-statements": "Overly Long Function",
    "too-many-nested-blocks": "Overly Long Function",
    "too-many-return-statements": "Overly Long Function",
    "bare-except": "Improper Exception Handling",
    "broad-exception-caught": "Improper Exception Handling",
    "try-except-raise": "Improper Exception Handling",
}

_PYLINT_BUG = {
    "undefined-variable": "Type Error",
    "used-before-assignment": "Type Error",
    "no-member": "Type Error",
    "not-callable": "Type Error",
    "unsupported-assignment-operation": "Type Error",
    "unsupported-binary-operation": "Type Error",
    "potential-index-error": "Index Out of Bounds",
    "unbalanced-tuple-unpacking": "Boundary Condition Error",
    "unsubscriptable-object": "Boundary Condition Error",
    "assignment-from-none": "Null Pointer/None Check Missing",
    "not-an-iterable": "Type Error",
    "invalid-sequence-index": "Index Out of Bounds",
    "return-in-init": "Return Value Error",
}


def _pylint_mapping(symbol: str, message_id: str) -> Tuple[Category, str, Severity]:
    """把 pylint 消息映射到项目统一的四分类体系。"""
    symbol = (symbol or "").strip()
    if symbol in _PYLINT_SECURITY:
        return Category.SECURITY, _PYLINT_SECURITY[symbol], Severity.HIGH
    if symbol in _PYLINT_PERFORMANCE:
        return Category.PERFORMANCE, _PYLINT_PERFORMANCE[symbol], Severity.MEDIUM
    if symbol in _PYLINT_BUG:
        return Category.BUG, _PYLINT_BUG[symbol], Severity.MEDIUM_HIGH
    if symbol in _PYLINT_SMELL:
        return Category.SMELL, _PYLINT_SMELL[symbol], Severity.LOW

    # 未知消息：按消息码前缀归类，二级分类直接沿用 pylint 符号（信息不丢失）
    subtype = symbol or "Unspecified"
    prefix = (message_id or "")[:1].upper()
    if prefix == "E":
        return Category.BUG, subtype, Severity.MEDIUM_HIGH
    if prefix == "F":
        return Category.BUG, subtype, Severity.HIGH
    return Category.SMELL, subtype, Severity.LOW


# ----------------------------------------------------------------------------
# bandit 测试项 -> (分类, 二级分类)
# ----------------------------------------------------------------------------
_BANDIT_SUBTYPE = {
    "B608": "SQL Injection",
    "B610": "SQL Injection",
    "B611": "SQL Injection",
    "B602": "Command Injection",
    "B603": "Command Injection",
    "B604": "Command Injection",
    "B605": "Command Injection",
    "B607": "Command Injection",
    "B601": "Command Injection",
    "B102": "Dangerous Function Abuse",
    "B307": "Dangerous Function Abuse",
    "B322": "Dangerous Function Abuse",
    "B105": "Hardcoded Sensitive Information",
    "B106": "Hardcoded Sensitive Information",
    "B107": "Hardcoded Sensitive Information",
    "B501": "Cross-Site Scripting (XSS)",
    "B506": "Insecure Deserialization",
    "B301": "Insecure Deserialization",
    "B302": "Insecure Deserialization",
    "B303": "Insecure Deserialization",
    "B304": "Insecure Deserialization",
    "B305": "Insecure Deserialization",
    "B306": "Insecure Deserialization",
    "B313": "Insecure Deserialization",
    "B314": "Insecure Deserialization",
    "B315": "Insecure Deserialization",
    "B316": "Insecure Deserialization",
    "B317": "Insecure Deserialization",
    "B318": "Insecure Deserialization",
    "B319": "Insecure Deserialization",
    "B320": "Insecure Deserialization",
    "B110": "Improper Exception Handling",
    "B112": "Improper Exception Handling",
    "B101": "Redundant Code/Dead Code",
}

_BANDIT_SEVERITY = {
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
}

#: 关注的问题子集（全量跑 pylint 很慢且噪音大，这里只启用与四分类相关的检查）
_PYLINT_ENABLE = sorted(
    set(_PYLINT_SECURITY) | set(_PYLINT_PERFORMANCE) | set(_PYLINT_SMELL) | set(_PYLINT_BUG)
)


@lru_cache(maxsize=1)
def _supported_pylint_symbols() -> FrozenSet[str]:
    """读取当前环境里 pylint 认识的消息名（只探测一次，约 0.5s）。

    探测失败（未安装 pylint / API 变动）时返回空集合，调用方退化为「原样传入」。
    """
    try:
        from pylint.lint import PyLinter

        linter = PyLinter()
        linter.load_default_plugins()
        symbols = {str(getattr(item, "symbol", "") or "") for item in linter.msgs_store.messages}
        symbols.discard("")
        return frozenset(symbols)
    except Exception as exc:  # noqa: BLE001 - 探测失败不应影响静态分析本身
        logger.info("无法探测 pylint 消息表（%s），将按原样传入 --enable", exc)
        return frozenset()


@lru_cache(maxsize=1)
def _pylint_enable_args() -> Tuple[str, ...]:
    """过滤掉当前 pylint 已不存在的消息名。

    把它们直接塞进 ``--enable`` 的话，pylint 会对**每个被审文件**报一条
    ``W0012 unknown-option-value``：那条告警说的是我们自己的参数写错了，不是代码问题，
    却会被当成「代码坏味」收进审查结果。所以要在下传之前先和消息表对齐一次。
    """
    supported = _supported_pylint_symbols()
    if not supported:
        return tuple(_PYLINT_ENABLE)

    kept = tuple(name for name in _PYLINT_ENABLE if name in supported)
    dropped = sorted(set(_PYLINT_ENABLE) - set(kept))
    if dropped:
        logger.warning(
            "以下 pylint 消息名在当前版本中不存在，已从 --enable 中剔除：%s",
            "、".join(dropped),
        )
    return kept


def _run_subprocess(cmd: List[str], timeout: float, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """在子进程中执行命令，返回 (returncode, stdout, stderr)。"""
    creationflags = 0
    if sys.platform == "win32":  # pragma: no cover - 平台相关
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            encoding="utf-8",
            errors="ignore",
            creationflags=creationflags,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"命令超时（>{timeout}s）: {' '.join(cmd[:3])} ..."
    except FileNotFoundError as exc:
        return -2, "", f"命令不可用: {exc}"


def _module_available(module: str) -> bool:
    try:
        return subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            timeout=20,
        ).returncode == 0
    except Exception:
        return False


def run_pylint(code: str, timeout: float = 45.0) -> List[Issue]:
    """调用 pylint 并转换为统一 Issue 列表。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "snippet.py"
        target.write_text(code, encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "pylint",
            "--output-format=json",
            "--score=n",
            "--persistent=no",  # 禁止写 ~/.pylint 缓存，避免污染用户目录与沙箱外写
            "--disable=all",
            f"--enable={','.join(_pylint_enable_args())}",
            str(target),
        ]
        _code, stdout, stderr = _run_subprocess(cmd, timeout=timeout)
    text = (stdout or "").strip()
    if not text:
        if stderr and "No module named pylint" in stderr:
            logger.info("pylint 不可用，跳过")
        return []
    try:
        messages = json.loads(text)
    except json.JSONDecodeError:
        # pylint 有时会输出多段文本，取第一段合法 JSON 数组
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            messages = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []

    issues: List[Issue] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        if symbol == "unknown-option-value":
            # 兜底：万一仍有 pylint 不认识的参数名，这条告警说的是调用方配置，不是被审代码
            logger.warning("pylint 报告了未知参数值，已忽略该条：%s", item.get("message"))
            continue
        message_id = str(item.get("message-id") or "")
        category, subtype, severity = _pylint_mapping(symbol, message_id)
        line = int(item.get("line") or 1)
        issues.append(
            Issue(
                category=category,
                severity=severity,
                subtype=subtype,
                line_start=line,
                line_end=line,
                violated_rule=symbol or message_id,
                risk_description=f"pylint[{message_id}] {item.get('message') or ''}".strip(),
                fix_suggestion="依据 pylint 提示修正；若属误报，请说明理由后忽略。",
                confidence=0.75,
                produced_by="pylint",
            )
        )
    return issues


def run_bandit(code: str, timeout: float = 45.0) -> List[Issue]:
    """调用 bandit 并转换为统一 Issue 列表。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "snippet.py"
        target.write_text(code, encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "bandit",
            "-f",
            "json",
            "-q",
            "--exit-zero",
            str(target),
        ]
        _code, stdout, _stderr = _run_subprocess(cmd, timeout=timeout)

    text = (stdout or "").strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            return []
        text = text[start:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    issues: List[Issue] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        test_id = str(item.get("test_id") or "")
        subtype = _BANDIT_SUBTYPE.get(test_id, "Dangerous Function Abuse")
        severity = _BANDIT_SEVERITY.get(str(item.get("issue_severity") or "").upper(), Severity.MEDIUM)
        line = int(item.get("line_number") or 1)
        issues.append(
            Issue(
                category=Category.SECURITY,
                severity=severity,
                subtype=subtype,
                line_start=line,
                line_end=int(item.get("line_range", [line])[-1] if item.get("line_range") else line),
                violated_rule=str(item.get("test_name") or test_id),
                risk_description=f"bandit[{test_id}] {item.get('issue_text') or ''}".strip(),
                fix_suggestion=str(item.get("more_info") or "参照 bandit 建议加固该处安全性。"),
                confidence=0.85,
                produced_by="bandit",
            )
        )
    return issues


def run_rules(code: str) -> List[Issue]:
    """复用项目自研规则引擎（AST + 正则）。"""
    try:
        from core.reviewer import rule_based_review
    except Exception as exc:  # pragma: no cover - 依赖导入失败时不影响其他工具
        logger.warning("无法导入规则引擎: %s", exc)
        return []
    try:
        raw = rule_based_review(code) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("规则引擎执行失败: %s", exc)
        return []
    issues: List[Issue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            issue = Issue.model_validate({**item, "produced_by": "rule_engine"})
        except Exception:
            continue
        if not issue.evidence:
            issue.evidence = [
                Evidence(
                    kind="rule",
                    tool="run_static_analysis",
                    line=issue.line_start,
                    snippet=issue.subtype,
                    detail="规则引擎命中的规则",
                )
            ]
        issues.append(issue)
    return issues


class RunStaticAnalysisTool(Tool):
    name = "run_static_analysis"
    description = (
        "对 Python 代码运行静态分析，返回客观告警清单。包含三种引擎：项目自研规则引擎、"
        "pylint、bandit。这是**审查任务的第一步**：先取得客观证据，再结合代码语义判断"
        "哪些告警真实成立、哪些属于误报。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "待分析的完整 Python 源码"},
            "engines": {
                "type": "string",
                "description": "逗号分隔的引擎列表，可选 rules/pylint/bandit，默认全部启用",
                "default": "rules,pylint,bandit",
            },
            "max_issues": {
                "type": "integer",
                "description": "最多返回多少条告警，默认 40",
                "default": 40,
            },
        },
        "required": ["code"],
    }

    def run(self, code: str, engines: str = "rules,pylint,bandit", max_issues: int = 40) -> ToolResult:
        wanted = {e.strip().lower() for e in str(engines).split(",") if e.strip()}
        collected: List[Issue] = []
        engine_stats: Dict[str, int] = {}
        engine_errors: Dict[str, str] = {}

        def _collect(name: str, items: List[Issue]) -> None:
            collected.extend(items)
            engine_stats[name] = len(items)

        if "rules" in wanted:
            _collect("rules", run_rules(code))
        if "pylint" in wanted and settings.ENABLE_PYLINT:
            try:
                _collect("pylint", run_pylint(code))
            except Exception as exc:  # noqa: BLE001
                engine_errors["pylint"] = str(exc)
        if "bandit" in wanted and settings.ENABLE_BANDIT:
            try:
                _collect("bandit", run_bandit(code))
            except Exception as exc:  # noqa: BLE001
                engine_errors["bandit"] = str(exc)

        # 去重（不同引擎常报同一位置同一问题）
        deduped: Dict[tuple[str, str, int], Issue] = {}
        for issue in collected:
            key = (issue.category.value, issue.subtype, issue.line_start)
            existing = deduped.get(key)
            if existing is None or issue.confidence > existing.confidence:
                deduped[key] = issue
        issues = sorted(deduped.values(), key=lambda i: (i.line_start, i.category.value))

        truncated = len(issues) > max_issues
        issues = issues[:max_issues]

        payload = {
            "engine_stats": engine_stats,
            "engine_errors": engine_errors,
            "total_after_dedupe": len(deduped),
            "truncated": truncated,
            "issues": [i.model_dump(mode="json") for i in issues],
            "note": (
                "以上为静态分析工具的原始告警，其中必然包含误报（例如对已有边界检查的代码"
                "仍报 Index Out of Bounds）。请结合代码上下文逐条判断，不要直接照抄。"
            ),
        }
        return ToolResult(
            tool=self.name,
            ok=True,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            data=payload,
        )


def available_engines() -> Dict[str, bool]:
    """探测各引擎可用性，供上层决定是否注册对应能力。"""
    return {
        "rules": True,
        "pylint": _module_available("pylint"),
        "bandit": _module_available("bandit"),
    }
