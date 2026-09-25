import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.analyzer import (LLM_ACCEPT_ENCODING, LLMAnalyzer, LLMRuntimeDependencyError,
                           PROMPT_VERSION, SCORE_PROMPT_VERSION)


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

    def test_brotli_dependency_error_does_not_retry(self):
        import httpx2
        import openai
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            err = openai.APIConnectionError(
                request=httpx2.Request("POST", "https://example.test/chat/completions"))
            raise err from TypeError("process() takes no keyword arguments")
        a = self.make_analyzer(create)
        with patch("core.analyzer.time.sleep") as sleep:
            with self.assertRaises(LLMRuntimeDependencyError) as cm:
                a._call_llm("x")
        self.assertIn("brotli", str(cm.exception).lower())
        self.assertEqual(len(calls), 1)
        self.assertEqual(a.usage["requests"], 1)
        sleep.assert_not_called()

    def test_client_disables_brotli_response_encoding(self):
        with patch("core.analyzer.OpenAI") as client:
            LLMAnalyzer({"llm": {"provider": "fake", "fake": {
                "api_key": "k", "base_url": "https://example.test", "model": "m"}}})
        self.assertEqual(client.call_args.kwargs["default_headers"]["Accept-Encoding"],
                         LLM_ACCEPT_ENCODING)
        self.assertNotIn("br", LLM_ACCEPT_ENCODING.split(","))


class RelevanceRubricTests(unittest.TestCase):
    def test_scoring_request_treats_topics_independently_and_records_version(self):
        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)
        analyzer.model = "mock"
        analyzer.topics = ["Machine learning interatomic potentials", "First-principles electronic structure",
                           "Computational catalysis"]
        analyzer.feedback_examples = {"liked": [], "disliked": []}
        article = {"title": "Spin disorder in oxides", "abstract": "A first-principles electronic structure study."}
        with patch.object(analyzer, "_call_llm", return_value=(
            '{"score":7,"reason":"实质研究第一性原理电子结构",'
            '"matched_topics":["First-principles electronic structure"]}'
        )) as llm:
            result = analyzer.filter_relevance(article)
        prompt = llm.call_args.args[0]
        self.assertEqual(llm.call_args.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(llm.call_args.kwargs["max_tokens"], 4096)
        for topic in analyzer.topics:
            self.assertIn(topic, prompt)
        for rule in ("排列顺序不代表优先级", "不要求同时命中多个方向", "不能因为未涉及机器学习势",
                     "实质属于任一列出的方向", "仅出现关键词", "没有可支持的关联"):
            self.assertIn(rule, prompt)
        self.assertEqual(result["score"], 7)
        self.assertEqual(result["basis"], "abstract")
        self.assertEqual(result["prompt_version"], SCORE_PROMPT_VERSION)
        self.assertEqual(PROMPT_VERSION, "v4")

    def test_title_only_request_exposes_evidence_limit(self):
        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)
        analyzer.model = "mock"
        analyzer.topics = ["Computational catalysis"]
        analyzer.feedback_examples = {"liked": [], "disliked": []}
        with patch.object(analyzer, "_call_llm", return_value='{"score":6,"reason":"仅凭标题判断"}') as llm:
            result = analyzer.filter_relevance({"title": "A computational study of catalysis"})
        self.assertIn("不能把摘要缺失本身当作不相关的证据", llm.call_args.args[0])
        self.assertEqual(result["basis"], "title")
        self.assertEqual(result["prompt_version"], SCORE_PROMPT_VERSION)


if __name__ == "__main__":
    unittest.main()
