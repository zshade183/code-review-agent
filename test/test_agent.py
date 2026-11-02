"""正式 agent 主线的核心回归测试。"""

from __future__ import annotations

import tempfile
import traceback
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.langgraph.llm import create_llm
from agent.pipelines import FixPipeline, ReviewPipeline
from agent.schema import Category, Issue, ReviewReport, Severity
from agent.tools import (
    build_fix_agent_registry,
    build_full_registry,
    build_harness_registry,
    build_review_registry,
    build_review_workflow_registry,
    build_verification_registry,
)


VULNERABLE_CODE = '''import os

def get_user(cursor, uid):
    cursor.execute("SELECT * FROM users WHERE id = " + uid)

def run(cmd):
    os.system("echo " + cmd)
'''


def test_schema_normalizes_and_deduplicates() -> None:
    low = Issue(category="安全漏洞", severity="高", subtype="SQL Injection", line_start=4, confidence=0.4)
    high = Issue(category=Category.SECURITY, severity=Severity.HIGH, subtype="SQL Injection", line_start=4, confidence=0.9)
    report = ReviewReport(review_summary="x", issues=[low, high]).dedupe()
    assert len(report.issues) == 1
    assert report.issues[0].confidence == 0.9
    assert report.to_legacy()["issues"][0]["risk_level"] == "高"

    # 模型自由填写的 evidence.kind / line 必须被归一，而不是让整条结论校验失败
    tolerant = Issue.model_validate({
        "category": "Security Vulnerability",
        "severity": "High",
        "subtype": "SQL Injection",
        "line_number": "5",
        "evidence": [{"kind": "tool", "tool": "run_static_analysis", "line": "第 5 行", "snippet": "x"}],
    })
    assert tolerant.evidence[0].kind == "tool_output"
    assert tolerant.evidence[0].line == 5


def test_tool_permissions_are_separated() -> None:
    from agent.agents import FIX_AGENT_CONFIG, REVIEW_AGENT_CONFIGS, VERIFICATION_AGENT_CONFIG
    from agent.tools import FIX_AGENT_TOOL_NAMES, REVIEW_TOOL_NAMES, VERIFICATION_TOOL_NAMES

    review = build_review_registry()
    verification = build_verification_registry()
    fix = build_fix_agent_registry()
    harness = build_harness_registry()
    workflow = build_review_workflow_registry()
    assert "apply_patch" not in review
    assert "execute_python_code" not in review
    assert "run_static_analysis" in review
    assert "run_static_analysis" not in verification
    assert "check_syntax" in verification
    assert "create_patch" not in fix
    assert "apply_patch" not in fix
    assert "check_signature" in fix
    assert "get_ast_outline" not in harness
    assert "check_signature" in harness
    assert all(not (workflow.get(name) and workflow.get(name).mutating) for name in workflow.names())
    assert REVIEW_AGENT_CONFIGS["auto"].tool_names == REVIEW_TOOL_NAMES
    assert VERIFICATION_AGENT_CONFIG.tool_names == VERIFICATION_TOOL_NAMES
    assert FIX_AGENT_CONFIG.tool_names == FIX_AGENT_TOOL_NAMES


def test_fix_harness_owns_delivery_and_fallback() -> None:
    from agent.fixing.harness import FixHarness
    from agent.tools.ast_tools import CheckSignatureTool
    from agent.tools.base import ToolRegistry

    original = "def add(a, b):\n    return a + b\n"
    harness = FixHarness(ToolRegistry([CheckSignatureTool()]))
    failed = harness.validate(original, "def add(:\n")
    report = ReviewReport(review_summary="", issues=[])
    delivery = harness.deliver(original, "def add(:\n", failed, attempts=1, report=report)
    assert failed.accepted is False
    assert delivery.optimized_code == original
    assert delivery.patch == ""
    assert delivery.used_fallback is True


def test_workflow_adapter_is_not_an_agent_tool() -> None:
    from agent.adapters.mcp import ReviewWorkflowTool
    from agent.executors import ToolCallingExecutor

    assert "review_code" not in build_full_registry()
    assert ReviewWorkflowTool.__module__ == "agent.adapters.mcp.review"
    assert ToolCallingExecutor.__module__ == "agent.executors.tool_calling"


def test_langgraph_pipeline_is_the_formal_entry() -> None:
    pipeline = ReviewPipeline(llm=create_llm("mock"), use_memory=False)
    result = pipeline.review(VULNERABLE_CODE, file_path="demo.py", strategy="full")
    assert result.stats["engine"] == "langgraph"
    assert result.reviewed_dimensions
    assert all(item.status == "completed" for item in result.coverage.dimensions.values())
    assert result.events
    assert result.to_dict()["report"] == result.report.to_legacy()


def test_pipeline_accepts_source_without_real_file() -> None:
    result = ReviewPipeline(llm=create_llm("mock"), use_memory=False).review("x = 1\n")
    assert result.report.review_summary
    assert result.coverage.partial is False


def test_fix_pipeline_keeps_review_and_mutation_separate() -> None:
    issue = Issue(
        category=Category.BUG,
        severity=Severity.MEDIUM,
        subtype="Type Error",
        line_start=1,
    )
    report = ReviewReport(review_summary="r", issues=[issue])
    plan = FixPipeline().plan("x = 1\n", report)
    assert len(plan.issues) == 1


def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    passed = 0
    failures = []
    for name, func in tests:
        try:
            if "tmp_dir" in func.__code__.co_varnames[: func.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as tmp:
                    func(Path(tmp))
            else:
                func()
        except Exception:
            failures.append((name, traceback.format_exc()))
            print(f"[FAIL] {name}")
        else:
            passed += 1
            print(f"[ OK ] {name}")
    print(f"\n{passed}/{len(tests)} 通过")
    for name, detail in failures:
        print(f"\n===== {name} =====\n{detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
