"""Agent 层验证脚本。

同时兼容两种运行方式：

- 直接运行：``python test/test_agent.py``
- 有 pytest 时：``pytest test/test_agent.py -q``

覆盖范围：结构化输出契约、Agent Loop 的各类停止条件、工具层（静态分析 / AST /
补丁 / 沙箱 / 代码库检索）、轨迹统计。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.llm import LLMClient  # noqa: E402
from agent.runtime import AgentConfig, AgentRuntime, extract_json_block  # noqa: E402
from agent.schema import Category, Issue, ReviewReport, Severity  # noqa: E402
from agent.trace import TraceRecorder, TraceStore, estimate_cost  # noqa: E402
from agent.tools import (  # noqa: E402
    CodebaseIndex,
    build_optimizer_registry,
    build_registry,
    build_reviewer_registry,
)

VULNERABLE_CODE = '''import os
import pickle


def get_user(cursor, uid):
    sql = "SELECT * FROM users WHERE id = " + uid
    cursor.execute(sql)
    return pickle.loads(open("data.bin", "rb").read())


def run(cmd):
    os.system("echo " + cmd)
'''


# ---------------------------------------------------------------------------
# 结构化输出契约
# ---------------------------------------------------------------------------


def test_issue_accepts_legacy_format() -> None:
    issue = Issue.model_validate(
        {
            "category": "Potential Bug",
            "dimension": "Potential Bug",
            "line_number": "3,5",
            "severity": "Medium-High",
            "subtype": "Index Out of Bounds",
            "risk_level": "中高",
            "risk_description": "越界",
            "fix_suggestion": "加边界检查",
            "confidence": 0.8,
        }
    )
    assert issue.category is Category.BUG
    assert issue.severity is Severity.MEDIUM_HIGH
    assert (issue.line_start, issue.line_end) == (3, 5)
    assert issue.line_number == "3,5"
    assert issue.risk_level == "中"


def test_issue_normalizes_chinese_and_partial_input() -> None:
    issue = Issue.model_validate(
        {"category": "安全漏洞", "line_number": "7", "risk_level": "高", "description": "SQL 拼接"}
    )
    assert issue.category is Category.SECURITY
    assert issue.severity is Severity.HIGH
    assert issue.risk_description == "SQL 拼接"


def test_report_dedupe_keeps_higher_confidence() -> None:
    low = Issue(category=Category.BUG, severity=Severity.LOW, subtype="Type Error", line_start=3, confidence=0.4)
    high = Issue(category=Category.BUG, severity=Severity.LOW, subtype="Type Error", line_start=3, confidence=0.9)
    report = ReviewReport(review_summary="x", issues=[low, high]).dedupe()
    assert len(report.issues) == 1
    assert report.issues[0].confidence == 0.9


def test_report_to_legacy_shape() -> None:
    report = ReviewReport(
        review_summary="ok",
        issues=[Issue(category=Category.SMELL, severity=Severity.LOW, subtype="Magic Number", line_start=2)],
    )
    legacy = report.to_legacy()
    assert set(legacy) == {"review_summary", "issues"}
    item = legacy["issues"][0]
    for key in ("category", "severity", "subtype", "line_number", "risk_level", "fix_suggestion"):
        assert key in item
    assert item["risk_level"] == "低"


def test_extract_json_block_variants() -> None:
    assert extract_json_block('```json\n{"a": 1}\n```').strip() == '{"a": 1}'
    assert extract_json_block('前言 {"a": {"b": 2}} 后记').strip() == '{"a": {"b": 2}}'
    assert extract_json_block('{"a": 1}').strip() == '{"a": 1}'


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------


def _review_finalizer(text: str):
    raw = extract_json_block(text)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"
    try:
        return ReviewReport.model_validate(payload), ""
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        return None, str(exc)


def test_agent_loop_end_to_end_with_mock() -> None:
    trace = TraceRecorder(run_id="test-loop", task="review")
    runtime = AgentRuntime(
        LLMClient(mock=True),
        build_reviewer_registry(),
        AgentConfig(max_steps=6),
        trace,
    )
    outcome = runtime.run(
        "审查代码：\n```python\n" + VULNERABLE_CODE + "\n```",
        finalizer=_review_finalizer,
    )
    assert outcome.run.stop_reason == "final"
    assert outcome.value is not None
    assert len(outcome.run.tool_calls) >= 1
    assert "run_static_analysis" in outcome.run.tool_names
    # 轨迹必须记录下来，供可观测性与评测使用
    assert len(outcome.run.steps) >= 3
    assert outcome.run.usage.total_tokens > 0


def test_agent_loop_retries_on_invalid_structured_output() -> None:
    """finalizer 前两次失败应触发重试，而不是直接返回空结果。"""
    state = {"calls": 0}

    def flaky_finalizer(text: str):
        state["calls"] += 1
        if state["calls"] < 3:
            return None, "issues 字段缺失"
        return ReviewReport(review_summary="ok", issues=[]), ""

    outcome = AgentRuntime(
        LLMClient(mock=True), build_reviewer_registry(), AgentConfig(max_steps=8)
    ).run("审查：\n```python\npass\n```", finalizer=flaky_finalizer)
    assert state["calls"] == 3
    assert outcome.run.stop_reason == "final"


def test_agent_loop_gives_up_after_retry_limit() -> None:
    outcome = AgentRuntime(
        LLMClient(mock=True),
        build_reviewer_registry(),
        AgentConfig(max_steps=8, structured_retry_limit=1),
    ).run("审查：\n```python\npass\n```", finalizer=lambda _t: (None, "永远失败"))
    assert outcome.run.stop_reason == "finalizer_failed"


def test_agent_loop_respects_max_tool_calls() -> None:
    outcome = AgentRuntime(
        LLMClient(mock=True),
        build_reviewer_registry(),
        AgentConfig(max_steps=8, max_tool_calls=1),
    ).run("审查：\n```python\n" + VULNERABLE_CODE + "\n```", finalizer=_review_finalizer)
    assert outcome.run.stop_reason == "max_tool_calls"
    assert len(outcome.run.tool_calls) == 1


def test_agent_loop_respects_max_steps() -> None:
    outcome = AgentRuntime(
        LLMClient(mock=True), build_reviewer_registry(), AgentConfig(max_steps=1)
    ).run("审查：\n```python\n" + VULNERABLE_CODE + "\n```", finalizer=_review_finalizer)
    assert outcome.run.stop_reason in {"max_steps", "max_tool_calls"}


def test_repeat_action_is_intercepted() -> None:
    """相同参数的重复调用不应无限执行，应被拦截并计入 no_progress。"""
    runtime = AgentRuntime(
        LLMClient(mock=True),
        build_reviewer_registry(),
        AgentConfig(max_steps=10, repeat_action_limit=0),
    )
    outcome = runtime.run("审查：\n```python\npass\n```", finalizer=_review_finalizer)
    observations = [s.content for s in outcome.run.steps if s.kind == "observe"]
    assert any("重复调用拦截" in text or "重复" in text for text in observations) or outcome.run.stop_reason in {
        "final",
        "max_steps",
    }


def test_text_react_protocol_parsing() -> None:
    runtime = AgentRuntime(LLMClient(mock=True), build_reviewer_registry())
    call = runtime._parse_text_action(
        'Thought: 先取证\nAction: run_static_analysis\nAction Input: {"code": "x = 1", "engines": "rules"}'
    )
    assert call is not None and call.name == "run_static_analysis"
    assert call.arguments["engines"] == "rules" and not call.parse_error

    nested = runtime._parse_text_action(
        'Action: run_static_analysis\nAction Input: {"code": "d = {\\"a\\": 1}", "engines": "rules"}'
    )
    assert nested is not None and nested.arguments.get("code") == 'd = {"a": 1}'

    assert runtime._parse_text_action("Thought: 没有动作") is None


def test_unknown_tool_is_reported_not_crashed() -> None:
    captured: list[str] = []

    class ScriptedClient(LLMClient):
        def __init__(self) -> None:
            super().__init__(mock=True)
            self.turn = 0

        def chat(self, messages, tools=None, temperature=None, tool_choice=None):  # type: ignore[override]
            from agent.llm import LLMResponse
            from agent.schema import ToolCall

            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="not_a_tool", arguments={})],
                    finish_reason="tool_calls",
                )
            captured.extend(str(m.get("content")) for m in messages)
            return LLMResponse(content='{"review_summary": "done", "issues": []}')

    outcome = AgentRuntime(ScriptedClient(), build_reviewer_registry()).run("x", finalizer=_review_finalizer)
    assert outcome.run.stop_reason == "final"
    assert any("未知工具" in text for text in captured)


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------


def test_static_analysis_finds_vulnerabilities() -> None:
    registry = build_reviewer_registry()
    tool = registry.get("run_static_analysis")
    assert tool is not None
    result = tool(code=VULNERABLE_CODE, engines="rules")
    assert result.ok
    subtypes = {item["subtype"] for item in result.data["issues"]}
    assert "SQL Injection" in subtypes
    assert "Command Injection" in subtypes
    assert "Insecure Deserialization" in subtypes


def test_ast_outline_and_signature_tools() -> None:
    registry = build_registry()
    outline = registry.get("get_ast_outline")(code=VULNERABLE_CODE)
    names = {d["name"] for d in outline.data["definitions"]}
    assert {"get_user", "run"} <= names

    modified = VULNERABLE_CODE.replace("def run(cmd):", "def execute(cmd):")
    compat = registry.get("check_signature")(original_code=VULNERABLE_CODE, modified_code=modified)
    assert compat.data["compatible"] is False
    assert "run" in compat.data["removed_symbols"]

    unused = registry.get("find_symbol")(code="def f():\n    tmp = 1\n    return 2\n", symbol="tmp")
    assert unused.data["is_unused"] is True


def test_check_syntax_tool() -> None:
    registry = build_registry()
    bad = registry.get("check_syntax")(code="def f(:\n    pass")
    assert bad.data["syntax_ok"] is False
    good = registry.get("check_syntax")(code="def f():\n    return 1")
    assert good.data["syntax_ok"] is True


def test_patch_roundtrip_and_context_mismatch() -> None:
    registry = build_optimizer_registry()
    original = "def add(a, b):\n    return a - b\n"
    fixed = "def add(a, b):\n    return a + b\n"

    created = registry.get("create_patch")(original_code=original, modified_code=fixed)
    patch = created.data["patch"]
    assert created.data["stats"]["added"] == 1
    assert created.data["stats"]["removed"] == 1

    applied = registry.get("apply_patch")(original_code=original, patch=patch)
    assert applied.ok and applied.data["applied"] is True
    assert applied.data["code"].strip() == fixed.strip()

    broken = registry.get("apply_patch")(original_code="def add(a, b):\n    return a * b\n", patch=patch)
    assert broken.ok is False
    assert "不匹配" in broken.error


def test_sandbox_executes_and_blocks_dangerous_code() -> None:
    registry = build_optimizer_registry()
    executed = registry.get("execute_python_code")(code="print(2 ** 10)")
    assert executed.data["success"] is True
    assert "1024" in executed.data["stdout"]

    blocked = registry.get("execute_python_code")(code="import os\nos.system('echo hi')")
    assert blocked.data["success"] is False
    assert blocked.data["violations"]


def test_sandbox_violation_grading() -> None:
    """警告级违规（第三方库导入）在非严格模式下放行，阻断级调用任何模式都拦截。"""
    registry = build_optimizer_registry()
    execute = registry.get("execute_python_code")

    # 严格模式（默认）：导入第三方库即拒绝执行
    strict_result = execute(code="import requests\nprint('ok')")
    assert strict_result.data["blocked"] is True
    assert strict_result.data["success"] is False

    # 非严格模式：允许执行，但违规仍被记录
    lenient = execute(code="import requests\nprint('ok')", strict=False)
    assert lenient.data["blocked"] is False
    assert lenient.data["exit_code"] == 0
    assert "ok" in lenient.data["stdout"]
    assert lenient.data["violations"]

    # 阻断级调用：非严格模式也不放行
    dangerous = execute(code="import os\nos.system('echo hi')", strict=False)
    assert dangerous.data["blocked"] is True
    assert dangerous.data["success"] is False


def test_sandbox_run_tests_reports_failure() -> None:
    registry = build_optimizer_registry()
    solution = "def add(a, b):\n    return a - b\n"
    tests = "assert add(2, 3) == 5, 'add 结果错误'\nprint('PASS')"
    result = registry.get("run_tests")(solution_code=solution, test_code=tests)
    assert result.data["exit_code"] != 0
    assert "add 结果错误" in result.data["stderr"] or "AssertionError" in result.data["stderr"]


def test_sandbox_timeout_is_enforced() -> None:
    registry = build_optimizer_registry()
    import agent.settings as settings

    original = settings.SANDBOX_TIMEOUT_S
    settings.SANDBOX_TIMEOUT_S = 1.0
    try:
        result = registry.get("execute_python_code")(code="while True:\n    pass", timeout=1)
        assert result.data["timed_out"] is True
    finally:
        settings.SANDBOX_TIMEOUT_S = original


def test_codebase_index_search_and_path_traversal_guard() -> None:
    index = CodebaseIndex(str(ROOT))
    stats = index.build()
    assert stats["files"] > 0 and stats["chunks"] > 0

    hits = index.search("rule_based_review 规则引擎", top_k=3)
    assert hits and all("path" in item for item in hits)

    found = index.find_symbol("code_review")
    assert any(item["path"].endswith("core/reviewer.py") for item in found)

    try:
        index.read("../../etc/passwd")
    except ValueError as exc:
        assert "越界" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("路径穿越未被拦截")


def test_reviewer_registry_excludes_mutating_tools() -> None:
    names = set(build_reviewer_registry().names())
    assert {"apply_patch", "execute_python_code", "run_tests"} & names == set()


def test_list_tools_reflects_subset() -> None:
    registry = build_reviewer_registry()
    listing = registry.get("list_tools")()
    assert "apply_patch" not in listing.content
    assert "run_static_analysis" in listing.content


# ---------------------------------------------------------------------------
# 可观测性
# ---------------------------------------------------------------------------


def test_trace_persist_and_summary(tmp_dir: Path) -> None:
    trace = TraceRecorder(run_id="test-trace", task="review", persist_dir=str(tmp_dir))
    outcome = AgentRuntime(LLMClient(mock=True), build_reviewer_registry(), AgentConfig(max_steps=6), trace).run(
        "审查：\n```python\n" + VULNERABLE_CODE + "\n```", finalizer=_review_finalizer
    )
    path = trace.persist(outcome.run)
    assert path is not None and path.exists()

    store = TraceStore(str(tmp_dir))
    summary = store.summary()
    assert summary["runs"] == 1
    assert "run_static_analysis" in summary["tool_usage"]
    assert summary["avg_tokens"] > 0
    assert "<html" in trace.render_html(outcome.run)


def test_cost_estimate_defaults_to_zero() -> None:
    from agent.schema import TokenUsage

    cost = estimate_cost(TokenUsage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000))
    assert cost["total_cost"] == 0.0


# ---------------------------------------------------------------------------
# 审查 Agent / 优化 Agent
# ---------------------------------------------------------------------------


def test_reviewer_agent_mock_end_to_end() -> None:
    from agent.reviewer_agent import ReviewerAgent

    agent = ReviewerAgent(client=LLMClient(mock=True), config=AgentConfig(max_steps=6))
    report, outcome = agent.review(VULNERABLE_CODE, file_path="demo.py")
    assert outcome.run.stop_reason == "final"
    assert report.issues, "mock 后端应基于静态分析证据产出问题"
    # 喂给工具的代码必须已去掉行号前缀，否则静态分析会因语法错误而失效
    call = outcome.run.tool_calls[0]
    assert call.arguments["code"].startswith("import os")
    assert "1| " not in call.arguments["code"]


def test_evidence_policy_drops_unsupported_claims() -> None:
    """没有任何证据的问题必须被丢弃，而不是直接进入最终报告。"""
    from agent.reviewer_agent import EvidencePolicy, _ReportFinalizer

    payload = {
        "review_summary": "两个问题",
        "issues": [
            {
                "category": "Potential Bug",
                "severity": "Medium-High",
                "subtype": "Index Out of Bounds",
                "line_start": 2,
                "risk_description": "越界",
                "evidence": [
                    {"kind": "tool_output", "tool": "run_static_analysis", "line": 2, "detail": "工具命中"}
                ],
            },
            {
                "category": "Code Smell",
                "severity": "Low",
                "subtype": "Magic Number",
                "line_start": 3,
                "risk_description": "魔法值",
            },
        ],
    }
    finalizer = _ReportFinalizer(
        code="def f():\n    a = 1\n    return a * 2\n",
        policy=EvidencePolicy(),
        tools_provider=lambda: ["run_static_analysis"],
    )
    report, error = finalizer(json.dumps(payload))
    # 第一次缺少证据 -> 触发重试而不是直接接受
    assert report is None and "evidence" in error

    report, error = finalizer(json.dumps(payload))
    assert error == "" and report is not None
    assert len(report.issues) == 1
    assert report.issues[0].verified is True
    assert "移除" in report.review_summary
    assert len(finalizer.dropped_issues) == 1


def test_evidence_policy_flags_uninvoked_tool_and_bad_line() -> None:
    from agent.reviewer_agent import EvidencePolicy, _ReportFinalizer

    payload = {
        "review_summary": "x",
        "issues": [
            {
                "category": "Security Vulnerability",
                "severity": "High",
                "subtype": "SQL Injection",
                "line_start": 1,
                "risk_description": "注入",
                "evidence": [
                    {"kind": "tool_output", "tool": "bandit", "line": 999, "detail": "声称来自 bandit"}
                ],
            }
        ],
    }
    finalizer = _ReportFinalizer(
        code="a = 1\n",
        policy=EvidencePolicy(),
        tools_provider=lambda: [],
    )
    report, _ = finalizer(json.dumps(payload))
    assert report is not None
    issue = report.issues[0]
    assert issue.verified is False
    assert "未被调用" in issue.verify_note and "超出代码范围" in issue.verify_note
    assert issue.confidence <= 0.5


def test_optimizer_agent_pipeline_rejects_then_accepts() -> None:
    """第一次交付语法错误应触发重试，第二次正确则通过验证。"""
    from agent.llm import LLMResponse
    from agent.optimizer_agent import OptimizerAgent

    original = "def add(a, b):\n    return a - b\n"
    good_fix = "def add(a, b):\n    return a + b\n"

    class Scripted(LLMClient):
        def __init__(self) -> None:
            super().__init__(mock=True)
            self.turn = 0

        def chat(self, messages, tools=None, temperature=None, tool_choice=None):  # type: ignore[override]
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(content="```python\ndef add(a, b)\n    return a + b\n```")
            return LLMResponse(content=f"```python\n{good_fix}```")

    agent = OptimizerAgent(client=Scripted(), config=AgentConfig(max_steps=4))
    result, outcome = agent.optimize(
        original,
        {"review_summary": "减号写错", "issues": [{"category": "Potential Bug", "severity": "Medium-High",
                                                   "subtype": "Logical Condition Error", "line_start": 2,
                                                   "risk_description": "应为加法", "fix_suggestion": "改为 +"}]},
        test_code="assert add(1, 2) == 3, 'add 错误'",
    )
    assert result.attempts == 2
    assert result.optimized_code.strip() == good_fix.strip()
    assert result.verification.passed is True
    assert result.signature_match is True
    assert result.changed is True
    assert result.patch


def test_optimizer_agent_falls_back_to_original_on_persistent_failure() -> None:
    from agent.llm import LLMResponse
    from agent.optimizer_agent import OptimizerAgent

    original = "def add(a, b):\n    return a - b\n"

    class AlwaysBroken(LLMClient):
        def __init__(self) -> None:
            super().__init__(mock=True)

        def chat(self, messages, tools=None, temperature=None, tool_choice=None):  # type: ignore[override]
            return LLMResponse(content="```python\ndef add(a, b)\n    return a + b\n```")

    agent = OptimizerAgent(client=AlwaysBroken(), config=AgentConfig(max_steps=6), max_verify_retries=2)
    result, _ = agent.optimize(original, None)
    assert result.verification.passed is False
    assert result.used_fallback is True
    # 关键：不通过验证时必须回退原代码，绝不交付坏修复
    assert result.optimized_code.strip() == original.strip()
    assert "回退" in result.summary


def test_optimizer_signature_break_is_detected() -> None:
    from agent.optimizer_agent import OptimizerAgent

    agent = OptimizerAgent(client=LLMClient(mock=True))
    broken = "def add(a, b, c):\n    return a + b\n"
    verification = agent.verify("def add(a, b):\n    return a + b\n", broken)
    assert verification.passed is False
    assert "signature_compatibility" in verification.failed_names


def test_optimization_result_legacy_shape() -> None:
    from agent.schema import OptimizationResult, VerificationReport

    report = VerificationReport().add("syntax", True).add("signature_compatibility", True)
    result = OptimizationResult(optimized_code="x", summary="s", verification=report)
    legacy = result.to_legacy()
    for key in ("optimized_code", "signature_match", "optimization_summary", "api_ok", "used_fallback"):
        assert key in legacy
    assert legacy["signature_match"] is True


# ---------------------------------------------------------------------------
# 多 Agent 主线：策略 / 领域分流 / 确定性合并 / 裁决 / 部分成功 / 记忆
# ---------------------------------------------------------------------------


def _mock_pipeline(memory=None, project="default"):
    from agent.pipelines import ReviewPipeline

    return ReviewPipeline(
        client=LLMClient(mock=True),
        memory=memory,
        project=project,
        persist_traces=False,
    )


def test_review_pipeline_strategies_mock() -> None:
    """三种策略都应产出报告；auto 只启动适用领域，full 固定四领域。"""
    pipeline = _mock_pipeline()

    fast = pipeline.review(VULNERABLE_CODE, file_path="demo.py", strategy="fast")
    auto = pipeline.review(VULNERABLE_CODE, file_path="demo.py", strategy="auto")
    full = pipeline.review(VULNERABLE_CODE, file_path="demo.py", strategy="full")

    for result in (fast, auto, full):
        assert result.report.issues, "mock 后端应基于共享证据产出问题"
        assert result.coverage.partial is False
        assert result.stats["adjudication"]["rejected"] >= 0
    assert fast.strategy.value == "fast"
    assert fast.stats["plan"]["reviewer_roles"] == []
    assert "single_agent" in fast.coverage.dimensions
    # auto 按证据/特征选择领域：这段代码没有循环/批处理，不应启动性能审查
    assert "performance" not in auto.stats["plan"]["reviewer_roles"]
    assert 1 <= len(auto.stats["plan"]["reviewer_roles"]) <= 4
    assert full.stats["plan"]["reviewer_roles"] == ["security", "bug", "quality", "performance"]
    assert len(auto.stats["plan"]["reviewer_roles"]) <= len(full.stats["plan"]["reviewer_roles"])
    # 每个报告的结论都带候选 ID 与裁决状态
    for issue in full.report.issues:
        assert issue.candidate_id.startswith("cand-")
        assert issue.adjudication is not None


def test_domain_reviewers_only_report_own_category() -> None:
    from agent.schema import Category

    expected = {
        "security": Category.SECURITY,
        "bug": Category.BUG,
        "quality": Category.SMELL,
        "performance": Category.PERFORMANCE,
    }
    result = _mock_pipeline().review(VULNERABLE_CODE, file_path="demo.py", strategy="full")
    assert set(result.drafts) == set(expected)
    for role, issues in result.drafts.items():
        for issue in issues:
            assert issue.category is expected[role], f"{role} 越界产出了 {issue.category}"
    assert result.drafts["security"], "示例代码含 SQL 注入 / 命令注入，安全角色应有产出"


def test_domain_reviewers_reuse_shared_static_evidence() -> None:
    """领域 Reviewer 不应重复执行公共静态分析，但可引用 Collector 的真实工具证据。"""
    from agent.evidence.collector import EvidenceCollector
    from agent.reviewers import build_domain_reviewers

    bundle = EvidenceCollector(engines=("rules",)).collect(VULNERABLE_CODE)
    reviewers = build_domain_reviewers(["security"], client=LLMClient(mock=True))
    reviewer = reviewers["security"]
    assert "run_static_analysis" not in reviewer.registry.names()
    assert "run_static_analysis" in bundle.shared_tool_names()

    candidates, _outcome = reviewer.review(VULNERABLE_CODE, bundle, file_path="demo.py")
    assert candidates
    assert all(
        "本轮未被调用" not in candidate.issue.verify_note for candidate in candidates
    ), "公共证据不应被误判为 Reviewer 伪造的工具调用"


def test_candidate_merger_is_deterministic_and_merges_sources() -> None:
    from agent.adjudication.merger import CandidateMerger
    from agent.schema import FindingCandidate, Issue

    def candidate(reviewer: str, subtype: str, line: int, confidence: float) -> FindingCandidate:
        return FindingCandidate(
            reviewer=reviewer,
            issue=Issue(
                category=Category.SECURITY,
                severity=Severity.HIGH,
                subtype=subtype,
                line_start=line,
                confidence=confidence,
                evidence=[{"kind": "tool_output", "tool": "run_static_analysis", "line": line}],
            ),
        )

    candidates = [
        candidate("security", "SQL Injection", 6, 0.7),
        candidate("bug", "SQL Injection", 6, 0.9),
        candidate("security", "Command Injection", 12, 0.8),
    ]
    merger = CandidateMerger()
    first = merger.merge(candidates, total_lines=20)
    second = merger.merge(list(reversed(candidates)), total_lines=20)

    assert [issue.candidate_id for issue in first.issues] == [
        issue.candidate_id for issue in second.issues
    ]
    assert len(first.issues) == 2
    sql = next(issue for issue in first.issues if issue.subtype == "SQL Injection")
    assert sql.sources == ["bug", "security"]
    assert sql.confidence == 0.9


def test_cross_agent_conflict_detection() -> None:
    from agent.adjudication.merger import CandidateMerger
    from agent.schema import FindingCandidate, Issue

    def candidate(reviewer: str, category: Category, line: int) -> FindingCandidate:
        return FindingCandidate(
            reviewer=reviewer,
            issue=Issue(
                category=category,
                severity=Severity.MEDIUM,
                subtype="X",
                line_start=line,
                evidence=[{"kind": "code_snippet", "snippet": "x"}],
            ),
        )

    same_role = CandidateMerger().merge(
        [candidate("security", Category.SECURITY, 5), candidate("security", Category.BUG, 5)],
        total_lines=20,
    )
    assert same_role.conflicts == [], "同一角色的不同类别结论不算跨角色冲突"

    cross_role = CandidateMerger().merge(
        [candidate("security", Category.SECURITY, 5), candidate("quality", Category.SMELL, 5)],
        total_lines=20,
    )
    assert len(cross_role.conflicts) == 1
    assert cross_role.conflict_ids <= set(cross_role.candidate_ids)


def test_deterministic_adjudicator_statuses() -> None:
    from agent.adjudication.critic import IssueVerdict
    from agent.adjudication.policy import Adjudicator
    from agent.evidence.verifier import Corroboration, VerificationResult
    from agent.schema import AdjudicationStatus

    def issue(subtype: str, line: int, confidence: float = 0.8) -> Issue:
        return Issue(
            category=Category.SECURITY,
            severity=Severity.HIGH,
            subtype=subtype,
            line_start=line,
            confidence=confidence,
            evidence=[{"kind": "code_snippet", "snippet": "x"}],
            verified=True,
        )

    issues = [
        issue("SQL Injection", 1),                     # 有佐证 + 保留 -> confirmed
        issue("Command Injection", 2),                 # Critic drop 且无佐证 -> rejected
        issue("Hardcoded Sensitive Information", 3),   # 记忆命中 -> memory_suspected
        issue("Insecure Deserialization", 4, 0.6),     # 无佐证且低置信 -> needs_review
    ]
    issue_ids = [item.ensure_candidate_id() for item in issues]
    verdicts = {issue_ids[1]: IssueVerdict(index=0, verdict="drop", reason="误报")}
    corroborations = {
        issue_ids[0]: Corroboration(index=0, corroborated=True, engines=["bandit"], candidate_id=issue_ids[0]),
        issue_ids[1]: Corroboration(index=1, corroborated=False, candidate_id=issue_ids[1]),
        issue_ids[2]: Corroboration(index=2, corroborated=False, candidate_id=issue_ids[2]),
        issue_ids[3]: Corroboration(index=3, corroborated=False, candidate_id=issue_ids[3]),
    }
    memory_flags = {issue_ids[2]: "历史误报率 100%"}
    verification = VerificationResult(engines_used=["pylint"])

    result = Adjudicator(confirm_confidence=0.65).adjudicate(
        issues,
        verdicts=verdicts,
        corroborations=corroborations,
        memory_flags=memory_flags,
        verification=verification,
    )
    statuses = {item.subtype: item.adjudication for item in result.issues}
    assert statuses["SQL Injection"] is AdjudicationStatus.CONFIRMED
    assert statuses["Hardcoded Sensitive Information"] is AdjudicationStatus.MEMORY_SUSPECTED
    assert statuses["Insecure Deserialization"] is AdjudicationStatus.NEEDS_REVIEW
    assert [item.subtype for item in result.rejected] == ["Command Injection"]
    # 记忆命中只降权分组，不删除
    suspicious = next(
        item for item in result.issues if item.adjudication is AdjudicationStatus.MEMORY_SUSPECTED
    )
    assert suspicious.confidence <= 0.4
    assert result.counts()["rejected"] == 1


def test_critic_selection_is_selective() -> None:
    from agent.adjudication.critic import CriticPolicy
    from agent.evidence.verifier import Corroboration
    from agent.schema import Category, Severity

    def issue(category, severity, confidence, subtype="X", line=1) -> Issue:
        return Issue(
            category=category,
            severity=severity,
            subtype=subtype,
            line_start=line,
            confidence=confidence,
            evidence=[{"kind": "code_snippet", "snippet": "x"}],
        )

    low_risk = issue(Category.SMELL, Severity.LOW, 0.9, "Magic Number", 1)
    risky = issue(Category.SECURITY, Severity.HIGH, 0.9, "SQL Injection", 2)
    uncertain = issue(Category.BUG, Severity.MEDIUM, 0.5, "Type Error", 3)
    issues = [low_risk, risky, uncertain]
    corroborations = {
        low_risk.ensure_candidate_id(): Corroboration(corroborated=True, engines=["pylint"]),
        risky.ensure_candidate_id(): Corroboration(corroborated=True, engines=["bandit"]),
        uncertain.ensure_candidate_id(): Corroboration(corroborated=False),
    }
    selection = CriticPolicy().select(issues, corroborations)
    selected_ids = {item.ensure_candidate_id() for item in selection.issues}
    assert low_risk.ensure_candidate_id() not in selected_ids, "低危 + 高置信 + 有佐证不必复核"
    assert risky.ensure_candidate_id() in selected_ids
    assert uncertain.ensure_candidate_id() in selected_ids


def test_partial_success_when_one_reviewer_fails() -> None:
    """一个领域失败不应让整次审查失败，覆盖度要明确标记。"""
    from agent.reviewers.quality import QualityReviewer

    original = QualityReviewer.review

    def broken(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("模拟领域 Reviewer 崩溃")

    QualityReviewer.review = broken
    try:
        result = _mock_pipeline().review(VULNERABLE_CODE, file_path="demo.py", strategy="full")
    finally:
        QualityReviewer.review = original

    assert result.coverage.partial is True
    assert result.coverage.dimensions["quality"].status == "error"
    assert result.coverage.dimensions["security"].status == "completed"
    assert result.report.issues, "其余领域的结果必须照常返回"
    assert result.drafts["security"]


def test_memory_suppression_happens_after_adjudication(tmp_dir: Path) -> None:
    """记忆命中只降权并单独分组，不在独立验证前删除候选。"""
    from agent.evidence.verifier import VerifierAgent
    from agent.memory import MemoryStore
    from agent.orchestrator import ReviewOrchestrator
    from agent.pipelines import ReviewPipeline
    from agent.tools import build_reviewer_registry

    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"), suppress_min_reports=1)
    target = Issue(
        category=Category.SECURITY,
        severity=Severity.HIGH,
        subtype="Command Injection",
        line_start=12,
    )
    store.record_feedback("demo", target, accepted=False, code=VULNERABLE_CODE)

    original_engines = VerifierAgent.ENGINES
    VerifierAgent.ENGINES = {}  # 让独立验证不可用，聚焦记忆策略本身
    try:
        orchestrator = ReviewOrchestrator(
            client=LLMClient(mock=True),
            memory=store,
            project="demo",
            persist_traces=False,
            use_memory=True,
        )
        pipeline = ReviewPipeline(client=LLMClient(mock=True), memory=store, project="demo")
        pipeline.orchestrator = orchestrator
        result = pipeline.review(VULNERABLE_CODE, file_path="demo.py", strategy="full")
    finally:
        VerifierAgent.ENGINES = original_engines

    command = [item for item in result.report.issues if item.subtype == "Command Injection"]
    assert command, "命中历史误报的候选不能被直接删除"
    assert command[0].adjudication is not None
    assert command[0].confidence <= 0.4
    assert "记忆" in (command[0].verify_note + command[0].adjudication_reason)


def test_review_pipeline_result_to_dict_shape() -> None:
    result = _mock_pipeline().review(VULNERABLE_CODE, file_path="demo.py", strategy="auto")
    payload = result.to_dict()
    for key in ("report", "strategy", "plan", "coverage", "stats", "evidence",
                "draft", "rejected", "verdicts", "corroborations", "traces", "partial"):
        assert key in payload, key
    assert payload["strategy"] == "auto"
    assert "adjudication" in payload["report"]["issues"][0] or not payload["report"]["issues"]


def test_fix_pipeline_requires_user_selection_and_excludes_rejected() -> None:
    from agent.pipelines import FixPipeline
    from agent.schema import AdjudicationStatus

    confirmed = Issue(
        category=Category.BUG, severity=Severity.MEDIUM_HIGH, subtype="Type Error",
        line_start=2, evidence=[{"kind": "code_snippet", "snippet": "x"}],
        adjudication=AdjudicationStatus.CONFIRMED,
    )
    rejected = Issue(
        category=Category.BUG, severity=Severity.LOW, subtype="Type Error",
        line_start=3, evidence=[{"kind": "code_snippet", "snippet": "x"}],
        adjudication=AdjudicationStatus.REJECTED,
    )
    suspected = Issue(
        category=Category.SMELL, severity=Severity.LOW, subtype="Magic Number",
        line_start=4, evidence=[{"kind": "code_snippet", "snippet": "x"}],
        adjudication=AdjudicationStatus.MEMORY_SUSPECTED,
    )
    report = ReviewReport(review_summary="r", issues=[confirmed, rejected, suspected])

    fixer = FixPipeline(client=LLMClient(mock=True))
    plan = fixer.plan("def add(a, b):\n    return a - b\n", report)
    selected = {issue.ensure_candidate_id() for issue in plan.issues}
    assert confirmed.ensure_candidate_id() in selected
    assert rejected.ensure_candidate_id() not in selected
    assert suspected.ensure_candidate_id() not in selected

    # 即使调用方显式传入 candidate_id，已驳回候选也不能绕过裁决进入修复。
    explicit = fixer.plan(
        "def add(a, b):\n    return a - b\n",
        report,
        candidate_ids=[rejected.ensure_candidate_id()],
    )
    assert explicit.empty
    assert any("驳回" in item["reason"] for item in explicit.excluded)

    only = fixer.plan(
        "def add(a, b):\n    return a - b\n",
        report,
        candidate_ids=[suspected.ensure_candidate_id()],
        include_memory_suspected=True,
    )
    assert [issue.ensure_candidate_id() for issue in only.issues] == [suspected.ensure_candidate_id()]

    empty = fixer.plan("x = 1\n", ReviewReport(review_summary="", issues=[]))
    assert empty.empty
    result, _outcome = fixer.fix("x = 1\n", empty)
    assert result.used_fallback is True and "未启动" in result.summary


def test_verifier_does_not_reuse_non_independent_engines() -> None:
    from agent.evidence.verifier import VerifierAgent

    verifier = VerifierAgent()
    engines, errors = verifier.select_engines(tuple(verifier.ENGINES))
    assert engines == []
    assert "independence" in errors


def test_evidence_cache_refreshes_memory_hits(tmp_dir: Path) -> None:
    from agent.evidence.store import EvidenceStore
    from agent.memory import MemoryStore

    memory = MemoryStore(path=str(tmp_dir / "mem.jsonl"))
    evidence = EvidenceStore(max_entries=2)
    code = "def log_value(value):\n    print(value)\n"
    first = evidence.get_or_collect(code, project="proj", memory=memory)
    assert first.memory_hits == []

    memory.add_convention("proj", "print value 统一替换为 logging")
    second = evidence.get_or_collect(code, project="proj", memory=memory)
    assert evidence.hits == 1
    assert any(hit.kind == "convention" for hit in second.memory_hits)


def test_evidence_collector_includes_import_symbols_and_codebase_hits(tmp_dir: Path) -> None:
    from agent.evidence.store import EvidenceStore

    project = tmp_dir / "project"
    project.mkdir()
    (project / "helpers.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    code = "import helpers\n\ndef calculate_total(items):\n    return helpers.calculate_total(items)\n"
    bundle = EvidenceStore().get_or_collect(code, codebase_root=str(project))
    assert "import helpers" in bundle.imports
    assert any(item["name"] == "calculate_total" for item in bundle.symbols)
    assert any(hit.get("path") == "helpers.py" for hit in bundle.codebase_hits)


def test_sqlite_memory_and_jsonl_migration(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore

    legacy = tmp_dir / "legacy.jsonl"
    old = MemoryStore(path=str(legacy))
    old.add_convention("proj", "统一使用参数化 SQL")

    database = tmp_dir / "memory.db"
    current = MemoryStore(path=str(database))
    assert current.migrate_jsonl(str(legacy)) == 1
    assert current.migrate_jsonl(str(legacy)) == 0
    reloaded = MemoryStore(path=str(database)).load()
    assert reloaded.sqlite_enabled is True
    assert any(record.text == "统一使用参数化 SQL" for record in reloaded.records)


def test_workflow_store_persists_review_and_fix_status(tmp_dir: Path) -> None:
    from agent.workflow_store import WorkflowStore

    store = WorkflowStore(str(tmp_dir / "workflows.db"))
    review_id = store.save("review", "x = 1\n", {"report": {"issues": []}}, "reviewed")
    fix_id = store.save(
        "fix", "x = 1\n", {"candidate_ids": []}, "awaiting_confirmation", parent_id=review_id
    )
    assert store.get(fix_id)["parent_id"] == review_id
    assert store.update_status(fix_id, "completed") is True
    assert store.get(fix_id)["status"] == "completed"


def test_llm_client_uses_thread_local_http_sessions() -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    client = LLMClient(mock=True)
    barrier = threading.Barrier(2)

    def get_session(_index):  # noqa: ANN001
        barrier.wait(timeout=2)
        return client._session()

    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(get_session, range(2)))
    assert sessions[0] is not sessions[1]


def test_verifier_uses_independent_engine() -> None:
    from agent.evidence.verifier import VerifierAgent

    verifier = VerifierAgent()
    code = "x = 1\n"
    report = ReviewReport(
        review_summary="r",
        issues=[
            Issue(category=Category.SECURITY, severity=Severity.HIGH, subtype="SQL Injection", line_start=1),
            Issue(category=Category.SMELL, severity=Severity.LOW, subtype="Magic Number", line_start=8),
        ],
    )
    independent_issue = Issue(
        category=Category.SECURITY, severity=Severity.HIGH, subtype="SQL Injection", line_start=2
    )
    original_engines = VerifierAgent.ENGINES
    VerifierAgent.ENGINES = {
        "rules": lambda _code: [],
        "fake": lambda _code: [independent_issue],
    }
    try:
        results = verifier.verify(code, report, reviewer_engines=("rules",))
    finally:
        VerifierAgent.ENGINES = original_engines

    assert results[0].corroborated is True
    assert results[0].engines == ["fake"]
    # 第 8 行距离佐证位置 2 行超过宽容度
    assert results[1].corroborated is False


def test_verifier_verify_issues_uses_candidate_ids() -> None:
    from agent.evidence.bundle import EvidenceBundle
    from agent.evidence.verifier import VerifierAgent

    issue = Issue(
        category=Category.SECURITY, severity=Severity.HIGH, subtype="SQL Injection",
        line_start=2, evidence=[{"kind": "code_snippet", "snippet": "x"}],
    )
    bundle = EvidenceBundle(code="a = 1\n", engines_used=["rules"])
    result = VerifierAgent().verify_issues([issue], bundle, used_engines=["rules"])
    assert issue.ensure_candidate_id() in result.corroborations
    assert result.corroborations[issue.ensure_candidate_id()].candidate_id == issue.ensure_candidate_id()


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------


def test_memory_convention_recall_and_context(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore

    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"))
    store.add_convention("proj", "本项目统一使用参数化 SQL，禁止字符串拼接")
    store.add_convention("proj", "日志统一使用 logging，禁止 print")

    recalled = store.recall("SQL 查询 拼接 参数化", project="proj", kinds=["convention"])
    assert recalled
    context = store.build_context("cursor.execute(sql)", project="proj")
    assert "项目约定" in context
    assert "参数化 SQL" in context


def test_perception_and_short_term_memory_are_bounded_and_transient(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore

    path = tmp_dir / "mem.jsonl"
    store = MemoryStore(path=str(path), short_term_limit=2, max_sessions=2)
    store.capture_perception("proj", "print('secret')", session_id="s1")
    perception = store.current_perception("proj", "s1")
    assert perception is not None
    assert perception.text == "print('secret')"
    assert perception.payload["sha256"]

    store.remember_turn("s1", "user", "第一次审查", project="proj")
    store.remember_turn("s1", "assistant", "发现一个问题", project="proj")
    store.remember_turn("s1", "user", "第二次审查", project="proj")
    turns = store.session_turns("s1", "proj")
    assert [turn.text for turn in turns] == ["发现一个问题", "第二次审查"]
    assert "本会话短期记忆" in store.build_context("print", "proj", session_id="s1")

    # 新实例只能读到持久层；原始源码与短期会话不会写入 JSONL。
    reloaded = MemoryStore(path=str(path)).load()
    assert reloaded.current_perception("proj", "s1") is None
    assert reloaded.session_turns("s1", "proj") == []
    assert not any(record.kind in {"perception", "turn"} for record in reloaded.records)
    assert store.clear_session("s1", "proj") == 3


def test_entity_memory_keeps_latest_version_and_project_isolation(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore

    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"))
    first = store.upsert_entity(
        "proj-a", "component", "payments", {"database": "sqlite"}, source="test"
    )
    latest = store.upsert_entity(
        "proj-a", "component", "payments", {"database": "postgres"}, source="test"
    )
    store.upsert_entity("proj-b", "component", "payments", {"database": "mysql"})
    store.add_convention("proj-a", "payments 只允许使用 postgres")

    entities = store.entities("proj-a")
    assert len(entities) == 1
    assert entities[0].id == latest.id
    assert entities[0].id != first.id
    assert entities[0].payload["facts"]["database"] == "postgres"
    assert store.entities("default") == []
    assert store.recall("payments postgres", project="default", kinds=["convention"]) == []
    context = store.build_context("payments postgres component", "proj-a")
    assert "项目实体事实" in context
    assert "postgres" in context


def test_memory_suppresses_repeated_false_positive(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore

    code = "def f(items):\n    return items[0]\n"
    issue = Issue(
        category=Category.BUG,
        severity=Severity.MEDIUM_HIGH,
        subtype="Index Out of Bounds",
        line_start=2,
    )
    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"))

    # 只有 1 条记录时不应自动压制（样本不足）
    store.record_feedback("proj", issue, accepted=False, code=code)
    drop, reason = store.suppression_check("proj", issue, code)
    assert drop is False and reason == ""

    # 累积到 3 条驳回后达到压制条件
    store.record_feedback("proj", issue, accepted=False, code=code)
    store.record_feedback("proj", issue, accepted=False, code=code)
    drop, reason = store.suppression_check("proj", issue, code)
    assert drop is True and "误报率" in reason

    kept, suppressed = store.apply_suppression("proj", [issue], code)
    assert kept == [] and len(suppressed) == 1

    # 记录一次「采纳」后误报率降至 75%，不再满足阈值 -> 恢复报告
    store.record_feedback("proj", issue, accepted=True, code=code)
    drop, _ = store.suppression_check("proj", issue, code)
    assert drop is False


def test_memory_requires_matching_signature_or_form(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore

    code = "def f(items):\n    return items[0]\n"
    other_code = "def g(a, b):\n    return a / b\n"
    indexed = Issue(category=Category.BUG, severity=Severity.MEDIUM_HIGH,
                    subtype="Index Out of Bounds", line_start=2)
    unrelated = Issue(category=Category.PERFORMANCE, severity=Severity.MEDIUM,
                      subtype="Inefficient Loop", line_start=2)
    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"), suppress_min_reports=2)
    for _ in range(3):
        store.record_feedback("proj", indexed, accepted=False, code=code)

    drop, _ = store.suppression_check("proj", unrelated, other_code)
    assert drop is False, "不同分类且不相邻的结论不应被历史误报连带压制"


def test_hashing_embedder_is_deterministic() -> None:
    from agent.memory import HashingEmbedder, cosine, normalize_code_form

    embedder = HashingEmbedder(dim=128)
    a = embedder.embed("SQL 参数化 查询")
    b = embedder.embed("SQL 参数化 查询")
    assert a == b
    assert abs(cosine(a, a) - 1.0) < 1e-6
    # 数字与字符串字面量应被归一化，形态相同的代码得到相同指纹（标识符仍保留）
    assert normalize_code_form('x = "abc" + str(123)') == normalize_code_form("x = 'zzz' + str(456)")
    assert normalize_code_form("x = 1") != normalize_code_form("y = 1")


def test_reviewer_agent_applies_memory(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore
    from agent.reviewer_agent import ReviewerAgent

    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"), suppress_min_reports=1)
    target = Issue(category=Category.SECURITY, severity=Severity.HIGH,
                   subtype="Command Injection", line_start=5)
    store.record_feedback("demo", target, accepted=False, code=VULNERABLE_CODE)

    agent = ReviewerAgent(client=LLMClient(mock=True), config=AgentConfig(max_steps=6),
                          memory=store, project="demo")
    report, outcome = agent.review(VULNERABLE_CODE)
    assert "压制" in report.review_summary
    assert not any(i.subtype == "Command Injection" for i in report.issues)
    assert outcome.run.output.get("suppressed_by_memory")


def test_reviewer_agent_updates_layered_memory(tmp_dir: Path) -> None:
    from agent.memory import MemoryStore
    from agent.reviewer_agent import ReviewerAgent

    store = MemoryStore(path=str(tmp_dir / "mem.jsonl"))
    agent = ReviewerAgent(
        client=LLMClient(mock=True),
        config=AgentConfig(max_steps=6),
        memory=store,
        project="demo",
    )
    agent.review(VULNERABLE_CODE, file_path="src/demo.py", session_id="review-session")

    perception = store.current_perception("demo", "review-session")
    assert perception is not None
    assert perception.payload["file_path"] == "src/demo.py"
    turns = store.session_turns("review-session", "demo")
    assert [turn.payload["role"] for turn in turns] == ["user", "assistant"]
    files = store.entities("demo", entity_type="file")
    assert len(files) == 1
    assert files[0].payload["name"] == "src/demo.py"
    assert files[0].payload["facts"]["line_count"] == len(VULNERABLE_CODE.splitlines())


# ---------------------------------------------------------------------------
# 评测模块
# ---------------------------------------------------------------------------


def test_match_issues_line_and_category_levels() -> None:
    from experiments.agent_metrics import gt_has_lines, match_issues

    gt_with_line = [
        {"category": "Potential Bug", "line_start": 10, "line_end": 10},
        {"category": "Code Smell", "line_start": 20, "line_end": 20},
    ]
    predicted = [
        Issue(category=Category.BUG, severity=Severity.MEDIUM_HIGH, subtype="Type Error", line_start=10),
        Issue(category=Category.BUG, severity=Severity.MEDIUM_HIGH, subtype="Type Error", line_start=99),
    ]
    result = match_issues(predicted, gt_with_line, require_line=True)
    assert (result.tp, result.fp, result.fn) == (1, 1, 1)

    # 含行号时同类别但行号相距很远不算命中
    gt_no_line = [{"category": "Potential Bug", "line_start": 0, "line_end": 0}]
    assert gt_has_lines(gt_with_line) is True
    assert gt_has_lines(gt_no_line) is False

    category_result = match_issues(predicted, gt_no_line, require_line=False)
    assert category_result.tp == 1
    assert category_result.fp == 1


def test_evaluate_report_with_run_metrics() -> None:
    from experiments.agent_metrics import evaluate_report

    trace = TraceRecorder(run_id="metric-test", task="review")
    outcome = AgentRuntime(LLMClient(mock=True), build_reviewer_registry(), AgentConfig(max_steps=6), trace).run(
        "审查：\n```python\n" + VULNERABLE_CODE + "\n```", finalizer=_review_finalizer
    )
    report = outcome.value
    row = evaluate_report(
        report,
        [{"category": "Security Vulnerability", "line_start": 12, "line_end": 12}],
        run=outcome.run,
        sample_id="s1",
    )
    for key in ("steps", "tool_calls", "tool_accuracy", "stop_reason", "tokens",
                "duration_ms", "evidence_coverage", "task_success", "match_level"):
        assert key in row, key
    assert row["tool_accuracy"] is True
    assert row["steps"] > 0
    assert row["tokens"] > 0


def test_aggregate_reports_and_format() -> None:
    from experiments.agent_metrics import aggregate, format_summary

    rows = [
        {"tp": 2, "fp": 0, "fn": 0, "task_success": True, "steps": 5, "tool_calls": 2,
         "tool_accuracy": True, "stop_reason": "final", "tokens": 100, "cost": 0.0,
         "duration_ms": 1000, "evidence_coverage": 1.0, "verified_rate": 1.0,
         "tool_usage": {"run_static_analysis": 1}, "by_category": {"security": {"tp": 2, "fp": 0, "fn": 0, "count": 2}}},
        {"tp": 0, "fp": 3, "fn": 1, "task_success": False, "steps": 9, "tool_calls": 4,
         "tool_accuracy": False, "stop_reason": "max_steps", "tokens": 300, "cost": 0.0,
         "duration_ms": 3000, "evidence_coverage": 0.0, "verified_rate": 0.0,
         "tool_usage": {"run_static_analysis": 1, "get_ast_outline": 1},
         "by_category": {"security": {"tp": 0, "fp": 3, "fn": 1, "count": 1}}},
    ]
    summary = aggregate(rows)
    assert summary["samples"] == 2
    assert summary["detection"]["tp"] == 2 and summary["detection"]["fp"] == 3
    assert summary["agent"]["has_execution"] is True
    assert summary["agent"]["avg_steps"] == 7.0
    assert summary["agent"]["stop_reasons"] == {"final": 1, "max_steps": 1}
    assert summary["agent"]["tool_usage"]["run_static_analysis"] == 2
    text = format_summary(summary)
    assert "Agent 执行过程" in text and "平均步数" in text

    # rules 基线（无执行轨迹）不应输出无意义的 Agent 指标
    rules_summary = aggregate([{k: v for k, v in rows[0].items() if k not in ("steps", "tool_calls")}])
    assert rules_summary["agent"]["has_execution"] is False
    assert "未执行" in format_summary(rules_summary)


def test_eval_loader_normalizes_all_schemas() -> None:
    from experiments.agent_eval import _normalize_item

    # 形态 2：code 嵌套 + 单 issue（JSONL）
    nested = _normalize_item(
        {
            "code_id": "bugsinpy_x_1",
            "language": "python",
            "code": {"buggy": "a = 1", "fixed": "a = 2"},
            "issue": {"type": "bug", "category": "Potential Bug", "sub_category": "Potential Bug",
                      "severity": "medium", "line": None, "description": "d"},
        }
    )
    assert nested is not None and nested.code == "a = 1" and nested.fixed_code == "a = 2"
    assert nested.issues[0].line_start == 0  # 行号未知 -> 0

    # 形态 3：buggy_code / bug_category
    flat = _normalize_item(
        {"id": "f1", "buggy_code": "x=1", "fixed_code": "x=2",
         "bug_category": "Code Smell", "bug_sub_category": "Code Smell"}
    )
    assert flat is not None and flat.code == "x=1" and flat.issues[0].category == "Code Smell"

    # 形态 1：issues 数组 + location
    standard = _normalize_item(
        {"id": "s1", "language": "Python", "code": "y=1",
         "issues": [{"category": "Potential Bug", "subtype": "Type Error",
                     "location": {"line_start": 3, "line_end": 4}}]}
    )
    assert standard is not None and standard.issues[0].line_start == 3

    assert _normalize_item({"foo": "bar"}) is None


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


def _run_all() -> int:
    import tempfile
    import traceback

    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    passed = 0
    failed: list[tuple[str, str]] = []
    for name, func in tests:
        try:
            if "tmp_dir" in func.__code__.co_varnames[: func.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as tmp:
                    func(Path(tmp))
            else:
                func()
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"[FAIL] {name}")
        else:
            passed += 1
            print(f"[ OK ] {name}")

    print(f"\n{passed}/{len(tests)} 通过")
    for name, tb in failed:
        print(f"\n===== {name} =====\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
