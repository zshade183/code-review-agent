"""Regression tests for tool failures, evidence caching and output repair."""
import unittest
from types import SimpleNamespace

from agent.tools.base import Tool, ToolRegistry
from agent.evidence_service import EvidenceService
from agent.langgraph.workflow import _validated_output


class FlakyTool(Tool):
    name = "flaky"
    description = "Test transient failures"
    retry_safe = True
    retry_delay_s = 0
    result_fields = {"items": list}

    def __init__(self):
        self.calls = 0

    def run(self):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary")
        return self.ok("done", {"items": []})


class RecoveryTests(unittest.TestCase):
    def test_transient_retry_and_success_cache_preserve_data(self):
        tool = FlakyTool()
        service = EvidenceService(ToolRegistry([tool]), "x=1")
        result, _ = service.execute(tool.name, {}, "review")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        cached, _ = service.execute(tool.name, {}, "verification")
        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.data, {"items": []})
        self.assertEqual(tool.calls, 2)

    def test_failure_not_cached_and_mutation_not_retried(self):
        tool = FlakyTool()
        tool.mutating = True
        service = EvidenceService(ToolRegistry([tool]), "x=1")
        failed, _ = service.execute(tool.name, {}, "review")
        self.assertFalse(failed.ok)
        self.assertEqual(tool.calls, 1)
        result, _ = service.execute(tool.name, {}, "review")
        self.assertTrue(result.ok)
        self.assertFalse(result.from_cache)

    def test_invalid_tool_output_is_not_evidence(self):
        tool = FlakyTool()
        tool.run = lambda: tool.ok("bad", {"items": "wrong"})
        service = EvidenceService(ToolRegistry([tool]), "x=1")
        result, _ = service.execute(tool.name, {}, "review")
        self.assertEqual(result.error_type, "invalid_output")
        self.assertFalse(service.successful_ids())
        tool.run = lambda: {"ok": True}
        self.assertEqual(tool().error_type, "invalid_output")

    def test_bad_arguments_never_execute(self):
        tool = FlakyTool()
        tool.parameters = {"properties": {"count": {"type": "integer"}}, "required": ["count"]}
        for value in (True, 1.5, []):
            self.assertEqual(tool(count=value).error_type, "invalid_arguments")
        self.assertEqual(tool.calls, 0)

    def test_repair_once_and_reject_invalid_envelope(self):
        class Model:
            calls = 0
            output = '{"issues": []}'
            def invoke(self, messages):
                self.calls += 1
                return SimpleNamespace(content=self.output)
        model = Model()
        self.assertEqual(_validated_output(model, '{"issues": "bad"}', "issues"), {"issues": []})
        self.assertEqual(model.calls, 1)
        model.output = '{}'
        with self.assertRaises(ValueError):
            _validated_output(model, '{}', "issues")
        self.assertEqual(model.calls, 2)


if __name__ == "__main__":
    unittest.main()
