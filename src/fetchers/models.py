"""
fetchers/models.py - fetchers 包的常量与数据模型中心枢纽

此文件是 fetchers 包内所有共享常量、状态枚举与数据模型的唯一权威来源。
所有常量均支持通过环境变量覆盖（适用于 GitHub Actions / 服务器自动化部署）。
其他模块应统一从此处导入，而非在各自文件中重复定义。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

__all__ = [
    # 常量
    "MAX_FULLTEXT_CHARS",
    "MIN_FULLTEXT_LEN",
    "MIN_ABSTRACT_LEN",
    # 枚举类
    "FetchStatus",
    "BestFormat",
    "EvidenceLevel",
    # 数据模型
    "FetchResult",
]

# ── 共享常量（支持环境变量注入，保留默认值作为 fallback）────────────────
MAX_FULLTEXT_CHARS: int = int(os.environ.get("FETCHER_MAX_FULLTEXT_CHARS", "22000"))  # 全文截断上限
MIN_FULLTEXT_LEN: int   = int(os.environ.get("FETCHER_MIN_FULLTEXT_LEN",   "800"))   # 认定为"全文"的最少字符数
MIN_ABSTRACT_LEN: int   = int(os.environ.get("FETCHER_MIN_ABSTRACT_LEN",   "100"))   # 认定为"摘要"的最少字符数

# 常量合法性校验：防止因环境变量配置错误导致运行时逻辑异常
assert MAX_FULLTEXT_CHARS > MIN_FULLTEXT_LEN > MIN_ABSTRACT_LEN > 0, (
    f"常量配置非法：需满足 MAX_FULLTEXT_CHARS({MAX_FULLTEXT_CHARS}) > "
    f"MIN_FULLTEXT_LEN({MIN_FULLTEXT_LEN}) > "
    f"MIN_ABSTRACT_LEN({MIN_ABSTRACT_LEN}) > 0"
)


# ── 枚举类 ────────────────────────────────────────────────────

class FetchStatus(str, Enum):
    """全文获取状态码"""
    SUCCESS             = "success"               # 获取成功
    NO_HTML_URL         = "NO_HTML_URL"           # 无可用 HTML 链接
    HTML_FETCH_FAIL     = "HTML_FETCH_FAIL"       # HTML 抓取失败
    PDF_FETCH_FAIL      = "PDF_FETCH_FAIL"        # PDF 下载失败
    PDF_PARSE_FAIL      = "PDF_PARSE_FAIL"        # PDF 文本提取失败
    OCR_NEEDED          = "OCR_NEEDED"            # PDF 为扫描版，需 OCR
    WAITING_USER_UPLOAD = "WAITING_USER_UPLOAD"   # 等待用户手动上传


class BestFormat(str, Enum):
    """最优可用内容格式"""
    HTML_FULLTEXT   = "html_fulltext"    # HTML 全文
    PDF_TEXT        = "pdf_text"         # PDF 文本提取
    PDF_OCR         = "pdf_ocr"          # OCR 提取（框架占位，需安装 ocrmypdf/tesseract）
    ABSTRACT_ONLY   = "abstract_only"    # 仅摘要
    MANUAL_UPLOADED = "manual_uploaded"  # 用户手动上传


class EvidenceLevel(str, Enum):
    """LLM 输入证据等级，用于标注 AI 解读的可信度"""
    FULLTEXT      = "FULLTEXT"       # 已获取全文，解读可信度高
    ABSTRACT_ONLY = "ABSTRACT_ONLY"  # 仅有摘要，解读可信度受限


# ── 数据模型 ──────────────────────────────────────────────────

@dataclass
class FetchResult:
    """全文获取结果，贯穿整个分层回退流程"""
    text: str                        = ""
    best_available_format: BestFormat = BestFormat.ABSTRACT_ONLY
    fetch_status: FetchStatus        = FetchStatus.NO_HTML_URL
    error_code: str | None           = None
    source: Literal["auto", "manual"]                    = "auto"
    network_mode: Literal["public", "campus_proxy", "vpn"] = "public"
    access_path: Literal["direct", "proxy"]              = "direct"

    @property
    def has_fulltext(self) -> bool:
        """是否成功获取到全文（非纯摘要，且文本长度达到全文下限）"""
        return (
            self.best_available_format != BestFormat.ABSTRACT_ONLY
            and len(self.text) >= MIN_FULLTEXT_LEN
        )

    @property
    def evidence_level(self) -> EvidenceLevel:
        """返回 LLM 输入证据等级"""
        return EvidenceLevel.FULLTEXT if self.has_fulltext else EvidenceLevel.ABSTRACT_ONLY