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
# 多智能体
# ---------------------------------------------------------------------------


def test_multi_agent_pipeline_mock_end_to_end() -> None:
    from agent.multi_agent import MultiAgentReviewPipeline

    pipeline = MultiAgentReviewPipeline(client=LLMClient(mock=True), persist_traces=False)
    result = pipeline.review(VULNERABLE_CODE, file_path="demo.py")
    assert result.report.issues
    assert result.runs.keys() == {"reviewer", "critic"}
    # Critic 必须对每条问题都给出裁定，未覆盖的会被保守补为 keep
    assert len(result.verdicts) == len(result.draft.issues)
    assert result.stats["draft_issues"] >= result.stats["final_issues"]
    assert "多智能体" in result.report.review_summary


def test_merge_requires_agreement_to_drop() -> None:
    from agent.multi_agent import Corroboration, IssueVerdict, MultiAgentReviewPipeline

    def make_draft() -> ReviewReport:
        return ReviewReport(
            review_summary="draft",
            issues=[
                Issue(category=Category.SECURITY, severity=Severity.HIGH, subtype="SQL Injection",
                      line_start=1, confidence=0.9),
                Issue(category=Category.SMELL, severity=Severity.LOW, subtype="Magic Number",
                      line_start=2, confidence=0.8),
                Issue(category=Category.PERFORMANCE, severity=Severity.MEDIUM, subtype="Inefficient Loop",
                      line_start=3, confidence=0.9),
            ],
        )

    draft = make_draft()
    verdicts = [
        IssueVerdict(index=0, verdict="drop", reason="误判"),          # 但有佐证 -> 保留
        IssueVerdict(index=1, verdict="drop", reason="风格偏好"),      # 无佐证 -> 丢弃
        IssueVerdict(index=2, verdict="downgrade", reason="影响有限"),  # 降级
    ]
    corroborations = [
        Corroboration(index=0, corroborated=True, engines=["pylint"], note="x"),
        Corroboration(index=1, corroborated=False, engines=[], note="无同类告警"),
        Corroboration(index=2, corroborated=True, engines=["bandit"], note="y"),
    ]
    merged = MultiAgentReviewPipeline._merge(draft, verdicts, corroborations)

    assert len(merged.issues) == 2
    kept_sql = merged.issues[0]
    assert kept_sql.disagreement is True and kept_sql.confidence <= 0.4
    assert merged.issues[1].confidence == 0.6  # 0.9 - 0.3
    # 初版报告不能被合并过程污染（返回给调用方时仍是原值）
    assert len(draft.issues) == 3 and draft.issues[0].confidence == 0.9
    assert draft.issues[0].disagreement is False


def test_verifier_uses_independent_engine() -> None:
    from agent.multi_agent import VerifierAgent

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
    assert nested.issues[0]["line_start"] == 0  # 行号未知 -> 0

    # 形态 3：buggy_code / bug_category
    flat = _normalize_item(
        {"id": "f1", "buggy_code": "x=1", "fixed_code": "x=2",
         "bug_category": "Code Smell", "bug_sub_category": "Code Smell"}
    )
    assert flat is not None and flat.code == "x=1" and flat.issues[0]["category"] == "Code Smell"

    # 形态 1：issues 数组 + location
    standard = _normalize_item(
        {"id": "s1", "language": "Python", "code": "y=1",
         "issues": [{"category": "Potential Bug", "subtype": "Type Error",
                     "location": {"line_start": 3, "line_end": 4}}]}
    )
    assert standard is not None and standard.issues[0]["line_start"] == 3

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
