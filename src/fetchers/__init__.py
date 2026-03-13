"""
fetchers/ - 分层全文获取包

优先级: HTML(requests) → HTML(browser) → PDF → OCR(框架) → 手动上传
"""

from .models import FetchResult, FetchStatus, BestFormat, EvidenceLevel
from .network import get_proxies, get_network_info
from .html_fetcher import fetch_html
from .pdf_fetcher import fetch_pdf_text
from .manual_upload import ManualUploader

__all__ = [
    "FetchResult",
    "FetchStatus",
    "BestFormat",
    "EvidenceLevel",
    "get_proxies",
    "get_network_info",
    "fetch_html",
    "fetch_pdf_text",
    "ManualUploader",
]
