import unittest
from unittest.mock import Mock, patch

from integrations import openalex


class OpenAlexHttpTests(unittest.TestCase):
    def setUp(self):
        openalex.configure({"performance": {"rate_limit": {"domain_limits": {"api.openalex.org": 1000}}}})
        openalex._cooldown_until = 0.0

    def response(self, status, headers=None, payload=None):
        resp = Mock()
        resp.status_code = status
        resp.headers = headers or {}
        resp.json.return_value = payload or {"results": []}
        resp.raise_for_status.side_effect = None if status < 400 else RuntimeError(f"HTTP {status}")
        return resp

    @patch("integrations.openalex.time.sleep")
    @patch("integrations.openalex._session.get")
    def test_retry_after_then_success(self, get, sleep):
        get.side_effect = [self.response(429, {"Retry-After": "7"}), self.response(200)]
        self.assertEqual(openalex._get("/works", {}), {"results": []})
        self.assertEqual(get.call_count, 2)
        self.assertTrue(any(call.args and call.args[0] == 7 for call in sleep.call_args_list))

    @patch("integrations.openalex.time.sleep")
    @patch("integrations.openalex._session.get")
    def test_exhausted_429_is_classified(self, get, sleep):
        get.side_effect = [self.response(429), self.response(429), self.response(429)]
        with self.assertRaisesRegex(RuntimeError, "rate_limited=True"):
            openalex._get("/works", {})
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
