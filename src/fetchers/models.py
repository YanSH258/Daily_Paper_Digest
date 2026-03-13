"""
fetchers/models.py - 全文获取结果数据模型与状态码
"""
from dataclasses import dataclass
from typing import Optional

# ── 共享常量 ─────────────────────────────────────────────────
MAX_FULLTEXT_CHARS = 20000   # 全文截断上限（与 fetcher.py 保持一致）
MIN_FULLTEXT_LEN = 800       # 认定为"全文"的最少字符数
MIN_ABSTRACT_LEN = 100       # 认定为"摘要"的最少字符数


class FetchStatus:
    """全文获取状态码"""
    SUCCESS = "success"
    NO_HTML_URL = "NO_HTML_URL"              # 无可用 HTML 链接
    HTML_FETCH_FAIL = "HTML_FETCH_FAIL"      # HTML 抓取失败
    PDF_FETCH_FAIL = "PDF_FETCH_FAIL"        # PDF 下载失败
    PDF_PARSE_FAIL = "PDF_PARSE_FAIL"        # PDF 文本提取失败
    OCR_NEEDED = "OCR_NEEDED"               # PDF 为扫描版，需 OCR
    WAITING_USER_UPLOAD = "WAITING_USER_UPLOAD"  # 等待用户手动上传


class BestFormat:
    """最优可用内容格式"""
    HTML_FULLTEXT = "html_fulltext"
    PDF_TEXT = "pdf_text"
    PDF_OCR = "pdf_ocr"         # OCR 提取（框架占位，需安装 ocrmypdf/tesseract）
    ABSTRACT_ONLY = "abstract_only"
    MANUAL_UPLOADED = "manual_uploaded"


class EvidenceLevel:
    """LLM 输入证据等级，用于标注 AI 解读的可信度"""
    FULLTEXT = "FULLTEXT"
    ABSTRACT_ONLY = "ABSTRACT_ONLY"


@dataclass
class FetchResult:
    """全文获取结果，贯穿整个分层回退流程"""
    text: str = ""
    best_available_format: str = BestFormat.ABSTRACT_ONLY
    fetch_status: str = FetchStatus.NO_HTML_URL
    error_code: Optional[str] = None
    source: str = "auto"           # "auto" 或 "manual"
    network_mode: str = "public"   # "public" / "campus_proxy" / "vpn"
    access_path: str = "direct"    # "direct" / "proxy"

    @property
    def has_fulltext(self) -> bool:
        """是否成功获取到全文（非纯摘要）"""
        return bool(self.text) and self.best_available_format != BestFormat.ABSTRACT_ONLY

    @property
    def evidence_level(self) -> str:
        """返回 LLM 输入证据等级"""
        return EvidenceLevel.FULLTEXT if self.has_fulltext else EvidenceLevel.ABSTRACT_ONLY
