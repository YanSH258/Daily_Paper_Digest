"""MCP 服务器连接诊断与错误处理回归（本地模拟服务，localhost 临时端口）。

覆盖：认证失败文案、服务端错误透传、非 JSON 协议错误、连接失败提示、
超时上限、DPD_TOKEN 注入与不泄漏。
"""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mcp_server
from mcp_server import ApiError, _call, _timeout


def _json_bytes(payload, status=200):
    return status, json.dumps(payload).encode("utf-8")


class _FakeWorkbench(BaseHTTPRequestHandler):
    behavior = {"mode": "ok"}

    def log_message(self, *args):  # 静默访问日志
        pass

    def _respond(self):
        mode = self.behavior["mode"]
        if mode == "ok":
            status, body = _json_bytes({"ok": True})
        elif mode == "unauthorized":
            status, body = _json_bytes({"error": "unauthorized"}, 401)
        elif mode == "bad_request":
            status, body = _json_bytes({"error": "digest 配置无效: limit"}, 400)
        elif mode == "non_json":
            status, body = 200, b"<html>not the workbench</html>"
        elif mode == "hang":
            import time
            time.sleep(30)
            status, body = _json_bytes({"ok": True})
        else:  # pragma: no cover
            status, body = _json_bytes({"error": "?"}, 500)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._respond()


class McpErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkbench)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._saved = dict(os.environ)
        os.environ.pop("DPD_TOKEN", None)
        os.environ["DPD_API"] = self.base
        os.environ.pop("DPD_TIMEOUT", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_auth_failure_mentions_token_config(self):
        _FakeWorkbench.behavior["mode"] = "unauthorized"
        os.environ["DPD_TOKEN"] = "secret-token-value"
        with self.assertRaises(ApiError) as ctx:
            _call("GET", "/api/articles")
        msg = str(ctx.exception)
        self.assertIn("认证失败", msg)
        self.assertIn("DPD_TOKEN", msg)
        self.assertIn("web.api_token", msg)
        # 错误信息不回显 Token
        self.assertNotIn("secret-token-value", msg)

    def test_server_error_detail_surfaced(self):
        _FakeWorkbench.behavior["mode"] = "bad_request"
        with self.assertRaises(ApiError) as ctx:
            _call("GET", "/api/digests/preview")
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("digest 配置无效", str(ctx.exception))

    def test_non_json_response_is_protocol_error(self):
        _FakeWorkbench.behavior["mode"] = "non_json"
        with self.assertRaises(ApiError) as ctx:
            _call("GET", "/api/articles")
        self.assertIn("非 JSON", str(ctx.exception))
        self.assertIn("DPD_API", str(ctx.exception))

    def test_connection_refused_shows_actionable_hint(self):
        os.environ["DPD_API"] = "http://127.0.0.1:1"
        with self.assertRaises(ApiError) as ctx:
            _call("GET", "/api/status")
        msg = str(ctx.exception)
        self.assertIn("无法连接文献工作台", msg)
        self.assertIn("daily-paper-web", msg)
        self.assertIn("/healthz", msg)

    def test_timeout_bounded(self):
        _FakeWorkbench.behavior["mode"] = "hang"
        os.environ["DPD_TIMEOUT"] = "1"
        with self.assertRaises(ApiError) as ctx:
            _call("GET", "/api/status")
        self.assertIn("超时", str(ctx.exception))

    def test_timeout_env_clamped(self):
        os.environ["DPD_TIMEOUT"] = "99999"
        self.assertEqual(_timeout(), mcp_server.MAX_TIMEOUT)
        os.environ["DPD_TIMEOUT"] = "abc"
        self.assertEqual(_timeout(), mcp_server.TIMEOUT)

    def test_token_sent_as_header(self):
        seen = {}

        class _Capture(_FakeWorkbench):
            def _respond(self):
                seen["token"] = self.headers.get("X-API-Token")
                super()._respond()

        self.addCleanup(_FakeWorkbench.behavior.__setitem__, "mode", _FakeWorkbench.behavior["mode"])
        self.server.RequestHandlerClass = _Capture
        self.addCleanup(setattr, self.server, "RequestHandlerClass", _FakeWorkbench)
        os.environ["DPD_TOKEN"] = "secret-token-value"
        _FakeWorkbench.behavior["mode"] = "ok"
        _call("GET", "/api/status")
        self.assertEqual(seen.get("token"), "secret-token-value")


if __name__ == "__main__":
    unittest.main()
