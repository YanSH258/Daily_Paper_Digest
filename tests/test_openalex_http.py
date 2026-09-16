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

    def test_configure_installs_api_key_and_env_override(self):
        from integrations import openalex
        old = openalex._API_KEY
        try:
            openalex.configure({"openalex": {"api_key": "test-key-123"}})
            self.assertEqual(openalex._API_KEY, "test-key-123")
            openalex.configure({"openalex": {"api_key": ""}})
            self.assertEqual(openalex._API_KEY, "")
        finally:
            openalex.set_api_key(old)

    @patch("integrations.openalex._session.get")
    def test_api_key_param_sent_only_when_configured(self, get):
        from integrations import openalex
        resp = Mock(status_code=200, headers={})
        resp.json.return_value = {"results": []}
        resp.raise_for_status.side_effect = None
        get.return_value = resp
        old = openalex._API_KEY
        try:
            openalex.configure({"openalex": {"api_key": "k123"}})
            openalex._get("/works", {})
            self.assertEqual(get.call_args.kwargs["params"]["api_key"], "k123")
            openalex.configure({"openalex": {"api_key": ""}})
            openalex._get("/works", {})
            self.assertNotIn("api_key", get.call_args.kwargs["params"])
        finally:
            openalex.set_api_key(old)

    @patch("integrations.openalex.time.sleep")
    @patch("integrations.openalex._session.get")
    def test_retry_after_then_success(self, get, sleep):
        get.side_effect = [self.response(429, {"Retry-After": "7"}), self.response(200)]
        self.assertEqual(openalex._get("/works", {}), {"results": []})
        self.assertEqual(get.call_count, 2)
        # Retry-After is respected but capped and jittered, never raw.
        waited = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertTrue(any(7 <= w <= 7 + 1 or w <= openalex.MAX_COOLDOWN_SECONDS + 1 for w in waited)
                        and max(waited) <= openalex.MAX_COOLDOWN_SECONDS + 1, waited)

    @patch("integrations.openalex.time.sleep")
    @patch("integrations.openalex._session.get")
    def test_exhausted_429_is_classified(self, get, sleep):
        get.side_effect = [self.response(429), self.response(429), self.response(429)]
        with self.assertRaisesRegex(RuntimeError, "rate_limited=True"):
            openalex._get("/works", {})
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
