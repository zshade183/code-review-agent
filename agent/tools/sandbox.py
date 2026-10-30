"""受限子进程沙箱。

这是优化 Agent 的「验证器」基础：改完代码必须真的跑一次，而不是只做静态检查。

**能力与边界（务必如实了解）**：
- 已做：独立子进程 + 超时强制终止 + ``-I`` 隔离模式（忽略用户 site-packages 与环境变量）
  + 临时工作目录 + AST 静态危险调用扫描 + 输出截断
- 可选：``AGENT_SANDBOX_BACKEND=docker`` 使用无网络、只读根文件系统、内存/CPU/PID
  配额的容器。默认 ``subprocess`` 仍只适合受信任来源或 LLM 生成的小片段。

之所以仍要提供它：Agent 若无法执行代码，就只能「猜测」修复是否正确，无法形成
「提出修改 → 运行验证 → 失败则反思」的闭环。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent import settings
from agent.schema import ToolResult
from agent.tools.base import Tool

#: 静态拒绝的危险模块
DANGEROUS_MODULES = {
    "subprocess",
    "socket",
    "shutil",
    "ctypes",
    "multiprocessing",
    "pty",
    "telnetlib",
    "ftplib",
    "smtplib",
    "pickle",
    "marshal",
    "urllib",
    "http",
    "requests",
    "webbrowser",
    "winreg",
}

#: 静态拒绝的危险函数调用（模块.属性）
DANGEROUS_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "fork"),
    ("os", "execv"),
    ("os", "execvp"),
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("os", "kill"),
    ("shutil", "rmtree"),
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "check_output"),
}

#: 静态拒绝的内置函数
DANGEROUS_BUILTINS = {"eval", "exec", "compile", "__import__", "globals", "locals", "breakpoint"}

#: 违规分级。区分这两类很重要：
#: - 阻断级（真正的危险调用、语法错误）：任何情况下都不执行
#: - 警告级（引入网络/序列化等模块）：本身不危险，是否放行取决于调用场景。
#:   例如「验证用户自己写的文件能否跑通」时不应因为 ``import requests`` 就直接拒绝，
#:   而「执行 LLM 新生成的片段」时则应保持严格（默认）。
BLOCKING_VIOLATION_TYPES = {"forbidden_builtin", "forbidden_call", "syntax_error"}
WARNING_VIOLATION_TYPES = {"forbidden_import"}


@dataclass
class SandboxPolicy:
    """沙箱策略。

    ``strict=True``（默认）：出现任何违规（含 ``import requests`` 这类警告级）都拒绝执行，
    适用于执行 LLM 新生成的、应当自包含的代码片段。

    ``strict=False``：只拦截阻断级违规（危险调用 / 语法错误），警告级仅记录。
    适用于验证用户既有代码能否正常运行。
    """

    forbidden_modules: set[str] = field(default_factory=lambda: set(DANGEROUS_MODULES))
    forbidden_calls: set[Tuple[str, str]] = field(default_factory=lambda: set(DANGEROUS_CALLS))
    forbidden_builtins: set[str] = field(default_factory=lambda: set(DANGEROUS_BUILTINS))
    strict: bool = True


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool
    violations: List[Dict[str, Any]]
    blocked: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.blocked

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": round(self.duration_ms, 2),
            "timed_out": self.timed_out,
            "blocked": self.blocked,
            "violations": self.violations,
            "success": self.success,
        }


def scan_dangerous_calls(code: str, policy: Optional[SandboxPolicy] = None) -> List[Dict[str, Any]]:
    """静态扫描危险调用。返回违规清单（空列表表示未发现）。"""
    policy = policy or SandboxPolicy()
    violations: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [{"type": "syntax_error", "line": exc.lineno, "detail": f"{exc.msg}"}]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in policy.forbidden_modules:
                    violations.append(
                        {"type": "forbidden_import", "line": node.lineno, "detail": f"import {alias.name}"}
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in policy.forbidden_modules:
                violations.append(
                    {"type": "forbidden_import", "line": node.lineno, "detail": f"from {node.module} import ..."}
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in policy.forbidden_builtins:
                violations.append(
                    {"type": "forbidden_builtin", "line": node.lineno, "detail": f"{func.id}()"}
                )
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in policy.forbidden_calls
            ):
                violations.append(
                    {
                        "type": "forbidden_call",
                        "line": node.lineno,
                        "detail": f"{func.value.id}.{func.attr}()",
                    }
                )
    return violations


def run_in_subprocess(
    code: str,
    timeout: Optional[float] = None,
    stdin_data: str = "",
    policy: Optional[SandboxPolicy] = None,
    extra_files: Optional[Dict[str, str]] = None,
) -> SandboxResult:
    """在隔离子进程中执行代码。"""
    policy = policy or SandboxPolicy()
    timeout = settings.SANDBOX_TIMEOUT_S if timeout is None else float(timeout)

    violations = scan_dangerous_calls(code, policy)
    blocking = [v for v in violations if v.get("type") in BLOCKING_VIOLATION_TYPES]
    should_block = bool(blocking) or (policy.strict and bool(violations))
    if should_block:
        reason = "存在阻断级危险调用" if blocking else "静态安全检查未通过（严格模式）"
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"{reason}，已拒绝执行。",
            duration_ms=0.0,
            timed_out=False,
            violations=violations,
            blocked=True,
        )

    with tempfile.TemporaryDirectory(prefix="agent_sandbox_") as tmpdir:
        workdir = Path(tmpdir)
        target = workdir / "snippet.py"
        target.write_text(code, encoding="utf-8")
        for name, content in (extra_files or {}).items():
            path = workdir / Path(name).name
            path.write_text(content, encoding="utf-8")

        if settings.SANDBOX_BACKEND == "docker":
            mount = f"{workdir.resolve()}:/work:ro"
            cmd = [
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "-v", mount, "-w", "/work", settings.SANDBOX_DOCKER_IMAGE,
                "python", "-I", "-X", "utf8", "/work/snippet.py",
            ]
        else:
            cmd = [sys.executable, "-I", "-X", "utf8", str(target)]
        creationflags = 0
        if sys.platform == "win32":  # pragma: no cover - 平台相关
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                input=stdin_data,
                timeout=timeout,
                cwd=str(workdir),
                encoding="utf-8",
                errors="ignore",
                creationflags=creationflags,
            )
            duration_ms = (time.time() - started) * 1000.0
            return SandboxResult(
                exit_code=proc.returncode,
                stdout=_truncate(proc.stdout or ""),
                stderr=_truncate(proc.stderr or ""),
                duration_ms=duration_ms,
                timed_out=False,
                violations=violations,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"执行超时（>{timeout}s），进程已强制终止。",
                duration_ms=(time.time() - started) * 1000.0,
                timed_out=True,
                violations=violations,
            )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"沙箱启动失败：{type(exc).__name__}: {exc}",
                duration_ms=(time.time() - started) * 1000.0,
                timed_out=False,
                violations=violations,
            )


def _truncate(text: str) -> str:
    limit = settings.SANDBOX_MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [输出被截断，原始长度 {len(text)} 字符]"


class RunPythonTool(Tool):
    name = "execute_python_code"
    mutating = True  # 会真实执行代码，可能产生副作用（虽已限制在临时目录内）
    description = (
        "在受限子进程中执行一段 Python 代码并返回真实的 stdout/stderr/退出码。"
        "当需要验证某段代码的实际行为（例如某分支是否真的抛异常、修复后是否还能跑通）时使用。"
        "代码中请用 print 输出需要观察的值。"
        "默认严格模式：出现任何危险调用或网络/序列化类导入都会拒绝执行；"
        "若要验证一段本身依赖第三方库的既有代码能否跑通，可把 strict 设为 false"
        "（此时仍会拦截 os.system / subprocess / eval 等阻断级调用）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码（应包含 print 以便观察结果）"},
            "stdin": {"type": "string", "description": "传给程序的标准输入，默认空", "default": ""},
            "timeout": {"type": "number", "description": "超时秒数，默认使用全局配置", "default": 10},
            "strict": {
                "type": "boolean",
                "description": "是否启用严格模式（任何违规都拒绝执行），默认 true",
                "default": True,
            },
        },
        "required": ["code"],
    }

    def run(self, code: str, stdin: str = "", timeout: float = 10, strict: bool = True) -> ToolResult:
        if not settings.ENABLE_SANDBOX:
            return ToolResult(
                tool=self.name,
                ok=False,
                error="沙箱已被配置禁用（AGENT_ENABLE_SANDBOX=0）",
                content="沙箱未启用，无法执行代码。",
            )
        result = run_in_subprocess(
            code, timeout=timeout, stdin_data=stdin, policy=SandboxPolicy(strict=bool(strict))
        )
        return ToolResult(
            tool=self.name,
            ok=True,  # 只要沙箱本身跑起来了就算工具成功；代码报错是有效观察
            content=json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            data=result.to_dict(),
        )


class RunTestsTool(Tool):
    name = "run_tests"
    mutating = True  # 会真实执行代码
    description = (
        "把「待测代码」与「测试代码」拼接后在受限子进程中执行，用于验证修复是否真的正确。"
        "测试代码应包含 assert 或 print 判定结果。修复类任务在给出最终答案前**必须**调用本工具。"
        "默认非严格模式（允许待测代码导入第三方库，因为被验证的通常是项目既有文件），"
        "但 os.system / subprocess / eval 等阻断级调用始终会被拦截。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "solution_code": {"type": "string", "description": "待验证的实现代码"},
            "test_code": {"type": "string", "description": "测试代码，应能对 solution_code 中的函数做断言"},
            "timeout": {"type": "number", "description": "超时秒数，默认 10", "default": 10},
            "strict": {
                "type": "boolean",
                "description": "是否启用严格模式（连网络/序列化类导入也一并拦截），默认 false",
                "default": False,
            },
        },
        "required": ["solution_code", "test_code"],
    }

    def run(self, solution_code: str, test_code: str, timeout: float = 10, strict: bool = False) -> ToolResult:
        if not settings.ENABLE_SANDBOX:
            return ToolResult(
                tool=self.name,
                ok=False,
                error="沙箱已被配置禁用（AGENT_ENABLE_SANDBOX=0）",
                content="沙箱未启用，无法运行测试。",
            )
        combined = f"{solution_code}\n\n\n# ===== 测试 =====\n{test_code}\n"
        result = run_in_subprocess(
            combined, timeout=timeout, policy=SandboxPolicy(strict=bool(strict))
        )
        payload = result.to_dict()
        payload["combined_length"] = len(combined)
        return ToolResult(
            tool=self.name,
            ok=True,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            data=payload,
        )


class ScanDangerousCodeTool(Tool):
    name = "scan_dangerous_code"
    description = (
        "静态扫描代码中的危险调用（网络、子进程、文件删除、eval/exec 等），不执行代码。"
        "在执行代码前确认其安全性时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "待扫描的 Python 代码"}},
        "required": ["code"],
    }

    def run(self, code: str) -> ToolResult:
        violations = scan_dangerous_calls(code)
        payload = {"violations": violations, "safe": not violations}
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)
