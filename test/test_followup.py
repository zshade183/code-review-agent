import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.memory import MemoryStore
from agent.pipelines.review import ReviewPipeline
from agent.langgraph.mock_llm import MockChatModel


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(path=str(Path(self.temp.name) / "memory.db"))
        self.patches = [patch("agent.pipelines.review.get_shared_memory", return_value=self.memory),
                        patch("agent.langgraph.workflow.get_shared_memory", return_value=self.memory)]
        for p in self.patches:
            p.start()
        self.pipeline = ReviewPipeline(llm=MockChatModel(), project="p")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_review_then_followups_and_clear(self):
        self.pipeline.review("x = 1", session_id="s", strategy="fast", extra_context="关注安全")
        self.assertEqual(len(self.memory.session_turns("s", "p")), 2)
        captured = []
        class Model:
            def invoke(self, messages):
                captured.append(messages[-1].content)
                return SimpleNamespace(content="建议检查输入", tool_calls=[])
        self.pipeline.llm = Model()
        self.pipeline.follow_up("为什么？", "s")
        self.pipeline.follow_up("具体怎么做？", "s")
        self.assertIn("关注安全", captured[0])
        self.assertIn("建议检查输入", captured[1])
        self.assertIn("x = 1", captured[1])
        self.memory.clear_session("s", "p")
        with self.assertRaises(ValueError):
            self.pipeline.follow_up("继续", "s")

    def test_isolation_and_disabled_memory(self):
        self.pipeline.review("x = 1", session_id="s", strategy="fast", use_memory=False)
        self.assertFalse(self.memory.session_turns("s", "p"))
        self.pipeline.review("x = 1", session_id="s", strategy="fast")
        other = ReviewPipeline(llm=MockChatModel(), project="other")
        with self.assertRaises(ValueError):
            other.follow_up("继续", "s")
        with self.assertRaises(ValueError):
            self.pipeline.follow_up("继续", "different")

    def test_repeated_reviews_have_fresh_graph_state(self):
        first = self.pipeline.review("x = 1", session_id="s", strategy="fast")
        second = self.pipeline.review("x = 2", session_id="s", strategy="fast")
        self.assertEqual(len(first.events), len(second.events))
        self.assertEqual(self.memory.current_perception("p", "s").text, "x = 2")

    def test_long_history_is_bounded(self):
        self.pipeline.review("x = 1", session_id="s", strategy="fast")
        for _ in range(30):
            self.memory.remember_turn("s", "user", "长" * 9000, "p")
        captured = []
        self.pipeline.llm = SimpleNamespace(invoke=lambda messages: (captured.append(messages[-1].content) or SimpleNamespace(content="回答")))
        self.pipeline.follow_up("继续", "s")
        self.assertLess(len(captured[0]), 43000)
        self.assertLessEqual(len(self.memory.session_turns("s", "p")), 12)

    def test_api_followup_and_missing_session(self):
        self.pipeline.review("x = 1", session_id="s", strategy="fast")
        self.pipeline.llm = SimpleNamespace(invoke=lambda messages: SimpleNamespace(content="接口回答"))
        with patch("api.main._get_pipeline", return_value=self.pipeline):
            # Call handler directly so authentication configuration is independent of this regression.
            from api.main import agent_followup, FollowUpRequest
            from fastapi import HTTPException
            self.assertEqual(agent_followup(FollowUpRequest(session_id="s", question="继续", project="p"))["answer"], "接口回答")
            with self.assertRaises(HTTPException) as error:
                agent_followup(FollowUpRequest(session_id="missing", question="继续", project="p"))
            self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
