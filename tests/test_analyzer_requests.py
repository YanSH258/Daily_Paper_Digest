import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.analyzer import LLMAnalyzer


class AnalyzerRequestCountTests(unittest.TestCase):
    def make_analyzer(self, create):
        a = LLMAnalyzer.__new__(LLMAnalyzer)
        a.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        a.model = "mock"
        a.temperature = 0
        a.max_tokens = 10
        a.provider = "mock"
        a.usage = {"calls": 0, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        import threading
        a._usage_lock = threading.Lock()
        return a

    def test_timeout_retry_counts_every_request(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                raise TimeoutError("timeout")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
                usage=None,
            )
        a = self.make_analyzer(create)
        with patch("core.analyzer.time.sleep"):
            self.assertEqual(a._call_llm("x", retry=3), "ok")
        self.assertEqual(a.usage["requests"], 3)
        self.assertEqual(a.usage["calls"], 0)

    def test_failed_retry_counts_requests(self):
        def create(**kwargs):
            raise RuntimeError("broken")
        a = self.make_analyzer(create)
        with patch("core.analyzer.time.sleep"):
            with self.assertRaises(Exception):
                a._call_llm("x", retry=2)
        self.assertEqual(a.usage["requests"], 2)


if __name__ == "__main__":
    unittest.main()
