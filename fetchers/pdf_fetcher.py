"""
fetchers/pdf_fetcher.py - PDF 下载与文本提取

策略：
  1. 使用 requests 下载 PDF（支持代理）
  2. 优先使用 PyMuPDF (fitz) 提取文本层
  3. 若文本层内容不足（扫描版），返回 OCR_NEEDED 状态
     （OCR 执行为可插拔扩展点，需安装 ocrmypdf/tesseract）
"""
import re
import logging

import requests

from .models import FetchResult, FetchStatus, BestFormat, MAX_FULLTEXT_CHARS
from .network import get_network_info, DEFAULT_UA

logger = logging.getLogger(__name__)

MIN_TEXT_LEN = 200   # 文本层字符数低于此值视为扫描版

PDF_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/pdf,*/*;q=0.8",
}


def fetch_pdf_text(pdf_url: str, timeout: int = 30) -> FetchResult:
    """
    下载 PDF 并提取文本。

    Args:
        pdf_url: PDF 文件的直接下载链接
        timeout: 请求超时（秒）

    Returns:
        FetchResult：
          - SUCCESS + BestFormat.PDF_TEXT 时 text 中含提取文本
          - OCR_NEEDED 时 text 为空，需外部 OCR 处理
          - PDF_FETCH_FAIL / PDF_PARSE_FAIL 时携带 error_code
    """
    if not pdf_url:
        return FetchResult(
            fetch_status=FetchStatus.PDF_FETCH_FAIL,
            error_code="NO_PDF_URL",
        )

    net = get_network_info()
    proxies = net.get("proxies")

    # ── 下载 PDF ────────────────────────────────────────────────
    try:
        resp = requests.get(
            pdf_url,
            headers=PDF_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        resp.raise_for_status()

        ctype = resp.headers.get("Content-Type", "").lower()
        if "pdf" not in ctype and not pdf_url.lower().endswith(".pdf"):
            logger.warning(f"  PDF 响应内容类型异常: {ctype}")
            return FetchResult(
                fetch_status=FetchStatus.PDF_FETCH_FAIL,
                error_code=f"NOT_PDF:{ctype}",
                network_mode=net["network_mode"],
                access_path=net["access_path"],
            )

        pdf_bytes = resp.content
    except requests.exceptions.Timeout:
        logger.warning(f"  PDF 下载超时: {pdf_url}")
        return FetchResult(
            fetch_status=FetchStatus.PDF_FETCH_FAIL,
            error_code="PDF_DOWNLOAD_TIMEOUT",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )
    except Exception as e:
        logger.warning(f"  PDF 下载失败: {e}")
        return FetchResult(
            fetch_status=FetchStatus.PDF_FETCH_FAIL,
            error_code="PDF_FETCH_FAIL",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )

    # ── 提取文本 ────────────────────────────────────────────────
    return _extract_pdf_text(pdf_bytes, net)


def _extract_pdf_text(pdf_bytes: bytes, net: dict) -> FetchResult:
    """
    使用 PyMuPDF 从 PDF 字节流提取文本。
    PyMuPDF 未安装时返回 PDF_PARSE_FAIL，请 pip install PyMuPDF。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("  PyMuPDF 未安装，无法提取 PDF 文本。请运行: pip install PyMuPDF")
        return FetchResult(
            fetch_status=FetchStatus.PDF_PARSE_FAIL,
            error_code="PYMUPDF_NOT_INSTALLED",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text = [page.get_text() for page in doc]
        doc.close()

        full_text = " ".join(pages_text).strip()

        if not full_text or len(full_text) < MIN_TEXT_LEN:
            logger.info("  PDF 文本层内容不足，可能为扫描版（OCR_NEEDED）")
            return FetchResult(
                fetch_status=FetchStatus.OCR_NEEDED,
                best_available_format=BestFormat.PDF_OCR,
                error_code="OCR_NEEDED",
                network_mode=net["network_mode"],
                access_path=net["access_path"],
            )

        full_text = re.sub(r'\s+', ' ', full_text)[:MAX_FULLTEXT_CHARS]
        logger.info(f"  PDF 文本提取成功 ({len(full_text)} 字)")
        return FetchResult(
            text=full_text,
            best_available_format=BestFormat.PDF_TEXT,
            fetch_status=FetchStatus.SUCCESS,
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )
    except Exception as e:
        logger.warning(f"  PDF 文本提取异常: {e}")
        return FetchResult(
            fetch_status=FetchStatus.PDF_PARSE_FAIL,
            error_code="PDF_PARSE_FAIL",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )


def extract_pdf_bytes_to_text(pdf_bytes: bytes) -> str:
    """
    公共辅助函数：从 PDF 字节流提取纯文本字符串。
    供 manual_upload 等模块复用，避免重复实现 PyMuPDF 逻辑。

    Returns:
        提取到的文本字符串，失败或未安装 PyMuPDF 时返回空字符串。
    """
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = " ".join(page.get_text() for page in doc)
        doc.close()
        return re.sub(r'\s+', ' ', text).strip()
    except ImportError:
        logger.warning("PyMuPDF 未安装，无法提取 PDF 文本。请运行: pip install PyMuPDF")
        return ""
    except Exception as e:
        logger.warning(f"PDF 文本提取失败: {e}")
        return ""
