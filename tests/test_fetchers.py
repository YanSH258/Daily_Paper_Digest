"""
tests/test_fetchers.py - fetchers 包的最小必要测试

运行：
    python -m pytest tests/ -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# 将 src/ 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fetchers.models import FetchResult, FetchStatus, BestFormat, EvidenceLevel
from fetchers.network import get_proxies, get_network_info
from fetchers.html_fetcher import _extract_text
from fetchers.manual_upload import ManualUploader


# ─────────────────────────────────────────────────────────────
# 1. 数据模型测试
# ─────────────────────────────────────────────────────────────

class TestFetchResult(unittest.TestCase):
    def test_default_is_abstract_only(self):
        r = FetchResult()
        self.assertFalse(r.has_fulltext)
        self.assertEqual(r.evidence_level, EvidenceLevel.ABSTRACT_ONLY)
        self.assertEqual(r.best_available_format, BestFormat.ABSTRACT_ONLY)

    def test_html_fulltext_has_fulltext(self):
        r = FetchResult(
            text="long text " * 200,
            best_available_format=BestFormat.HTML_FULLTEXT,
            fetch_status=FetchStatus.SUCCESS,
        )
        self.assertTrue(r.has_fulltext)
        self.assertEqual(r.evidence_level, EvidenceLevel.FULLTEXT)

    def test_empty_text_not_has_fulltext(self):
        r = FetchResult(
            text="",
            best_available_format=BestFormat.HTML_FULLTEXT,
            fetch_status=FetchStatus.SUCCESS,
        )
        self.assertFalse(r.has_fulltext)

    def test_manual_uploaded_has_fulltext(self):
        r = FetchResult(
            text="some uploaded text " * 100,
            best_available_format=BestFormat.MANUAL_UPLOADED,
            fetch_status=FetchStatus.SUCCESS,
            source="manual",
        )
        self.assertTrue(r.has_fulltext)
        self.assertEqual(r.source, "manual")


# ─────────────────────────────────────────────────────────────
# 2. 网络检测测试
# ─────────────────────────────────────────────────────────────

class TestNetworkInfo(unittest.TestCase):
    def test_no_proxy_is_public(self):
        clean_env = {}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                  "http_proxy", "https_proxy", "no_proxy"):
            clean_env[k] = ""
        with patch.dict(os.environ, clean_env):
            # 明确删除，避免空字符串被误用
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ.pop(k, None)
            info = get_network_info()
        self.assertEqual(info["network_mode"], "public")
        self.assertEqual(info["access_path"], "direct")
        self.assertIsNone(info["proxies"])

    def test_proxy_detected(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.com:8080"},
                        clear=False):
            info = get_network_info()
        self.assertNotEqual(info["network_mode"], "public")
        self.assertEqual(info["access_path"], "proxy")
        self.assertIsNotNone(info["proxies"])

    def test_vpn_keyword_detected(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://vpn.example.com:1080"},
                        clear=False):
            info = get_network_info()
        self.assertEqual(info["network_mode"], "vpn")

    def test_campus_keyword_detected(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://ezproxy.library.edu:8080"},
                        clear=False):
            info = get_network_info()
        self.assertEqual(info["network_mode"], "campus_proxy")

    def test_get_proxies_returns_dict(self):
        with patch.dict(os.environ,
                        {"HTTP_PROXY": "http://p:8080", "NO_PROXY": "localhost"},
                        clear=False):
            p = get_proxies()
        self.assertEqual(p["http"], "http://p:8080")
        self.assertEqual(p["no_proxy"], "localhost")


# ─────────────────────────────────────────────────────────────
# 3. HTML 文本提取测试
# ─────────────────────────────────────────────────────────────

class TestExtractText(unittest.TestCase):
    # 每段文字确保足够长，合计超过 MIN_FULLTEXT_LEN=800 字符
    _LONG_PARA = "This is a detailed paragraph about chemistry methods with many technical terms. " * 4
    _SAMPLE_HTML = f"""
    <html><body>
      <div class="article-body">
        <p>{_LONG_PARA}</p>
        <p>{_LONG_PARA}</p>
        <p>{_LONG_PARA}</p>
        <p>Conclusions and future work in the field of chemistry are discussed.</p>
      </div>
    </body></html>
    """

    def test_generic_container_extraction(self):
        text = _extract_text(self._SAMPLE_HTML, {})
        self.assertGreater(len(text), 50)
        self.assertIn("chemistry", text)

    def test_publisher_selector_preferred(self):
        selectors = {"fulltext": ["div.article-body p"]}
        text = _extract_text(self._SAMPLE_HTML, selectors)
        self.assertIn("chemistry", text)

    def test_empty_html_returns_empty(self):
        text = _extract_text("<html><body></body></html>", {})
        self.assertEqual(text, "")

    def test_short_text_not_returned(self):
        html = "<html><body><p>Short.</p></body></html>"
        text = _extract_text(html, {})
        self.assertEqual(text, "")


# ─────────────────────────────────────────────────────────────
# 4. HTML 轻量请求测试（mock requests）
# ─────────────────────────────────────────────────────────────

class TestFetchHtml(unittest.TestCase):
    def test_empty_url_returns_no_html_url(self):
        from fetchers.html_fetcher import fetch_html
        result = fetch_html("")
        self.assertEqual(result.fetch_status, FetchStatus.NO_HTML_URL)

    def test_timeout_returns_html_fetch_fail(self):
        import requests as req_lib
        from fetchers.html_fetcher import fetch_html
        with patch("fetchers.html_fetcher.requests.get",
                   side_effect=req_lib.exceptions.Timeout):
            result = fetch_html("http://example.com")
        self.assertEqual(result.fetch_status, FetchStatus.HTML_FETCH_FAIL)
        self.assertIn("TIMEOUT", result.error_code)

    def test_non_html_content_type_returns_fail(self):
        from fetchers.html_fetcher import fetch_html
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/pdf"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "%PDF-1.4 fake"
        with patch("fetchers.html_fetcher.requests.get", return_value=mock_resp):
            result = fetch_html("http://example.com/paper.pdf")
        self.assertEqual(result.fetch_status, FetchStatus.HTML_FETCH_FAIL)
        self.assertIn("NOT_HTML", result.error_code)

    def test_successful_html_returns_text(self):
        from fetchers.html_fetcher import fetch_html
        long_paragraph = "This is a detailed paragraph about chemistry research. " * 20
        html_body = f"""<html><body>
            <article>
                <p>{long_paragraph}</p>
                <p>{long_paragraph}</p>
                <p>{long_paragraph}</p>
            </article>
        </body></html>"""
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html_body
        with patch("fetchers.html_fetcher.requests.get", return_value=mock_resp):
            result = fetch_html("http://example.com/article")
        self.assertEqual(result.fetch_status, FetchStatus.SUCCESS)
        self.assertEqual(result.best_available_format, BestFormat.HTML_FULLTEXT)
        self.assertGreater(len(result.text), 100)


# ─────────────────────────────────────────────────────────────
# 5. 手动上传测试
# ─────────────────────────────────────────────────────────────

class TestManualUploader(unittest.TestCase):
    def setUp(self):
        self.uploader = ManualUploader()
        self.tmp_dir = Path("/tmp/test_manual_upload")
        self.tmp_dir.mkdir(exist_ok=True)

    def test_nonexistent_file(self):
        result = self.uploader.upload("/nonexistent/path/paper.pdf")
        self.assertEqual(result.fetch_status, FetchStatus.WAITING_USER_UPLOAD)
        self.assertEqual(result.error_code, "FILE_NOT_FOUND")
        self.assertEqual(result.source, "manual")

    def test_unsupported_extension(self):
        f = self.tmp_dir / "paper.docx"
        f.write_bytes(b"fake docx content")
        result = self.uploader.upload(str(f))
        self.assertEqual(result.fetch_status, FetchStatus.WAITING_USER_UPLOAD)
        self.assertIn("UNSUPPORTED_FORMAT", result.error_code)

    def test_txt_upload_success(self):
        f = self.tmp_dir / "paper.txt"
        content = "This is a full text paper. " * 50
        f.write_text(content, encoding="utf-8")
        result = self.uploader.upload(str(f), doi="10.1021/test")
        self.assertEqual(result.fetch_status, FetchStatus.SUCCESS)
        self.assertEqual(result.best_available_format, BestFormat.MANUAL_UPLOADED)
        self.assertEqual(result.source, "manual")
        self.assertIn("This is a full text paper", result.text)

    def test_html_upload_success(self):
        f = self.tmp_dir / "paper.html"
        content = (
            "<html><body>"
            + "<p>This is a detailed research paper paragraph about chemistry. " * 30
            + "</p></body></html>"
        )
        f.write_text(content, encoding="utf-8")
        result = self.uploader.upload(str(f))
        self.assertEqual(result.fetch_status, FetchStatus.SUCCESS)
        self.assertEqual(result.best_available_format, BestFormat.MANUAL_UPLOADED)

    def test_empty_txt_fails(self):
        f = self.tmp_dir / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = self.uploader.upload(str(f))
        self.assertEqual(result.fetch_status, FetchStatus.WAITING_USER_UPLOAD)
        self.assertEqual(result.error_code, "EMPTY_CONTENT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
