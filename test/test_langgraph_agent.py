"""三角色 LangGraph 审查主线测试。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agents import AgentRoleConfig, REVIEW_AGENT_CONFIGS, build_review_prompt  # noqa: E402
from agent.evidence_service import EvidenceService  # noqa: E402
from agent.executors import ToolCallingExecutor  # noqa: E402
from agent.langgraph.mock_llm import MockChatModel  # noqa: E402
from agent.langgraph.prompts import NODE_MARKER  # noqa: E402
from agent.pipelines import ReviewPipeline  # noqa: E402
from agent.schema import AdjudicationStatus  # noqa: E402
from agent.tools import build_review_workflow_registry  # noqa: E402
from agent.tools.base import ToolRegistry  # noqa: E402

CODE = '''import os

def get_user(cursor, uid):
    cursor.execute("SELECT * FROM users WHERE id = " + uid)

def run(cmd):
    os.system("echo " + cmd)
'''


def _node(messages: list[BaseMessage]) -> str:
    system = str(getattr(messages[0], "content", "") or "")
    line = next((item for item in system.splitlines() if item.startswith(NODE_MARKER)), "")
    return line[len(NODE_MARKER):].strip()


def test_full_runs_review_then_verification() -> None:
    result = ReviewPipeline(llm=MockChatModel(), use_memory=False).review(CODE, strategy="full")
    assert list(result.coverage.dimensions) == ["review", "verification"]
    assert all(item.status == "completed" for item in result.coverage.dimensions.values())
    assert result.stats["selected_roles"] == ["review", "verification"]
    events = [item["type"] for item in result.events]
    assert events.index("review_agent") < events.index("normalize")
    assert events.index("verification_plan") < events.index("verification_agent")
    assert events.index("verification_agent") < events.index("adjudication")
    assert len(result.stats["agent_traces"]) == 2
    assert result.report.issues
    assert all(item.adjudication is AdjudicationStatus.CONFIRMED for item in result.report.issues)


def test_fast_skips_verification_without_becoming_partial() -> None:
    result = ReviewPipeline(llm=MockChatModel(), use_memory=False).review(CODE, strategy="fast")
    assert result.coverage.dimensions["review"].status == "completed"
    assert result.coverage.dimensions["verification"].status == "not_required"
    assert result.coverage.partial is False
    assert result.stats["verification"]["selected"] == 0
    assert all(item.adjudication is AdjudicationStatus.NEEDS_REVIEW for item in result.report.issues)


def test_auto_verifies_high_risk_candidates() -> None:
    result = ReviewPipeline(llm=MockChatModel(), use_memory=False).review(CODE, strategy="auto")
    assert result.stats["verification"]["selected"] >= 1
    assert result.coverage.dimensions["verification"].status == "completed"


def test_review_failure_is_reported_as_partial() -> None:
    class FailingReview(MockChatModel):
        def _generate(self, messages, *args, **kwargs):  # type: ignore[override]
            if _node(messages) == "review_agent":
                raise RuntimeError("review failed")
            return super()._generate(messages, *args, **kwargs)

    result = ReviewPipeline(llm=FailingReview(), use_memory=False).review(CODE, strategy="auto")
    assert result.coverage.partial is True
    assert result.coverage.dimensions["review"].status == "error"
    assert result.coverage.dimensions["verification"].status == "not_required"
    assert not result.report.issues


def test_verification_can_refute_a_candidate() -> None:
    class RefutingModel(MockChatModel):
        def _generate(self, messages, *args, **kwargs):  # type: ignore[override]
            if _node(messages) != "verification_agent":
                return super()._generate(messages, *args, **kwargs)
            text = "\n".join(str(getattr(item, "content", "") or "") for item in messages)
            ids = sorted(set(re.findall(r'"candidate_id"\s*:\s*"([^"]+)"', text)))
            payload = {"reviews": [{
                "candidate_id": item, "verdict": "refuted",
                "reason": "测试反证", "evidence_refs": [], "code_lines": [4], "missing_context": [],
            } for item in ids]}
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content=json.dumps(payload, ensure_ascii=False))
            )])

    result = ReviewPipeline(llm=RefutingModel(), use_memory=False).review(CODE, strategy="full")
    assert result.stats["adjudication"]["rejected"] >= 1
    assert not result.report.issues


def test_review_agent_uses_whitelist_and_evidence_cache() -> None:
    class ToolCallingModel:
        def __init__(self) -> None:
            self.calls = 0
            self.schemas = []

        def bind_tools(self, schemas, **_kwargs):
            self.schemas = schemas
            return self

        def invoke(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="补充 AST", tool_calls=[{
                    "name": "get_ast_outline", "args": {"code": "wrong"}, "id": "call_1",
                }])
            return AIMessage(content='{"issues":[]}')

    model = ToolCallingModel()
    config = REVIEW_AGENT_CONFIGS["fast"]
    full = build_review_workflow_registry()
    registry = full.subset([name for name in config.tool_names if name in full])
    service = EvidenceService(registry, "x = 1\n")
    system, user = build_review_prompt(config, {}, "x = 1\n")
    result = ToolCallingExecutor(model, registry, config, service).run("x = 1\n", system, user)
    assert result.stop_reason == "final"
    assert result.tool_calls[0]["arguments"]["code"] == "x = 1\n"
    assert result.tool_evidence[0]["evidence_id"].startswith("ev-")
    assert set(item["function"]["name"] for item in model.schemas) == set(registry.names())
    first_id = result.tool_evidence[0]["evidence_id"]
    cached, record = service.execute("get_ast_outline", {"code": "x = 1\n"}, "verification")
    assert record.evidence_id == first_id
    assert cached.from_cache is True


def test_tool_budget_preserves_a_final_summary_call() -> None:
    class BudgetModel:
        def __init__(self) -> None:
            self.bound = False

        def bind_tools(self, _schemas, **_kwargs):
            self.bound = True
            return self

        def invoke(self, _messages):
            if self.bound:
                self.bound = False
                return AIMessage(content="需要工具", tool_calls=[{
                    "name": "get_ast_outline", "args": {}, "id": "denied",
                }])
            return AIMessage(content='{"issues":[]}')

    full = build_review_workflow_registry()
    registry = full.subset(["get_ast_outline"])
    config = AgentRoleConfig(
        name="review", node_name="review_agent", responsibility="test",
        tool_names=("get_ast_outline",), max_steps=1, max_tool_calls=0,
    )
    system, user = build_review_prompt(config, {}, "x = 1\n")
    result = ToolCallingExecutor(BudgetModel(), registry, config).run("x = 1\n", system, user)
    assert result.stop_reason == "budget_final"
    assert result.content == '{"issues":[]}'


def test_unified_executor_retries_finalizer() -> None:
    class FinalizerModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _messages):
            self.calls += 1
            return AIMessage(content="bad" if self.calls == 1 else "good")

    config = AgentRoleConfig(
        name="fix", node_name="fix_agent", responsibility="fix",
        tool_names=(), max_steps=3, max_tool_calls=0,
    )
    result = ToolCallingExecutor(FinalizerModel(), ToolRegistry(), config).run(
        "x = 1\n", "system", "user",
        finalizer=lambda text: ("accepted", "" if text == "good" else "invalid"),
    )
    assert result.stop_reason == "final"
    assert result.value == "accepted"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
