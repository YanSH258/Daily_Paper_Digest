"""
fetchers/oa_fetcher.py - 开放获取全文链接获取器

1. get_oa_url()         : 通过 Unpaywall API 获取 OA 全文链接
2. get_openalex_abstract(): 通过 OpenAlex API 补全摘要

所需环境变量配置（可选，未设置时使用模块常量默认值）：
  UNPAYWALL_EMAIL     : 用于 Unpaywall 和 OpenAlex API 的联系邮箱（强烈建议配置）
  OA_FETCHER_TIMEOUT  : HTTP 请求超时秒数，默认 10
  OA_MAX_RETRIES      : 最大重试次数，默认 3
  OA_RETRY_BACKOFF    : 重试退避因子（秒），默认 1.0
"""
import logging
import os
import re
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块级常量（支持环境变量覆盖）
# ---------------------------------------------------------------------------
_PLACEHOLDER_PATTERN = re.compile(r"your[@\s]", re.IGNORECASE)

def _load_email_from_config() -> str:
    """尝试从 config 模块读取 email，失败时返回空字符串。"""
    try:
        from config import Config  # type: ignore
        email: str = getattr(Config, "UNPAYWALL_EMAIL", "") or getattr(Config, "USER_EMAIL", "") or ""
        return email.strip()
    except Exception:
        pass
    try:
        import config as _cfg  # type: ignore
        email = getattr(_cfg, "UNPAYWALL_EMAIL", "") or getattr(_cfg, "USER_EMAIL", "") or ""
        return email.strip()
    except Exception:
        return ""


def _resolve_email() -> str:
    """优先从环境变量读取 email，再 fallback 到 config，最后为空字符串。"""
    email: str = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if not email:
        email = _load_email_from_config()
    return email


DEFAULT_EMAIL: str = _resolve_email()
DEFAULT_TIMEOUT: int = int(os.environ.get("OA_FETCHER_TIMEOUT", "10"))
MAX_RETRIES: int = int(os.environ.get("OA_MAX_RETRIES", "3"))
RETRY_BACKOFF: float = float(os.environ.get("OA_RETRY_BACKOFF", "1.0"))

# ---------------------------------------------------------------------------
# 共享 Session 工厂
# ---------------------------------------------------------------------------
_RETRYABLE_STATUS_CODES: tuple[int, ...] = (429, 500, 502, 503, 504)


def _create_session(max_retries: int = MAX_RETRIES, backoff_factor: float = RETRY_BACKOFF) -> requests.Session:
    """创建带有重试策略的 requests.Session。

    Args:
        max_retries: 最大重试次数。
        backoff_factor: 退避因子（秒），第 n 次重试等待 backoff_factor * 2^(n-1) 秒。

    Returns:
        配置好重试策略的 requests.Session 实例。
    """
    retry_strategy: Retry = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=list(_RETRYABLE_STATUS_CODES),
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter: HTTPAdapter = HTTPAdapter(max_retries=retry_strategy)
    session: requests.Session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# 公共 API 函数
# ---------------------------------------------------------------------------

def get_oa_url(
    doi: str,
    email: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """通过 Unpaywall API 获取开放获取全文链接，完全免费。

    Args:
        doi: 文献的 DOI 字符串。
        email: 联系邮箱，默认使用模块级 DEFAULT_EMAIL。
        timeout: HTTP 请求超时秒数，默认使用模块级 DEFAULT_TIMEOUT。

    Returns:
        OA 全文 PDF 链接或 OA 页面链接；未找到时返回空字符串。
    """
    if not doi:
        return ""

    resolved_email: str = (email or DEFAULT_EMAIL or "").strip()
    if not resolved_email or _PLACEHOLDER_PATTERN.search(resolved_email):
        logger.warning(
            "get_oa_url: email 未配置或为占位符（%r），Unpaywall 查询可能失败（DOI: %s）。",
            resolved_email or "(空)",
            doi,
        )

    session: requests.Session = _create_session()
    try:
        resp: requests.Response = session.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": resolved_email},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(
                "Unpaywall 返回非200状态码 %d（DOI: %s）。",
                resp.status_code,
                doi,
            )
            return ""
        data: dict[str, Any] = resp.json()
        if not data.get("is_oa"):
            return ""
        best: dict[str, Any] = data.get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url") or ""
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning(
            "Unpaywall 网络错误，重试耗尽仍失败（DOI: %s）：%s",
            doi,
            e,
            exc_info=True,
        )
        return ""
    except Exception as e:
        logger.warning(
            "Unpaywall 查询发生未预期异常（DOI: %s）：%s",
            doi,
            e,
            exc_info=True,
        )
        return ""
    finally:
        session.close()


def get_openalex_abstract(
    doi: str,
    email: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """通过 OpenAlex API 获取结构化摘要，免费无需 API Key。

    Args:
        doi: 文献的 DOI 字符串。
        email: 联系邮箱（用于 User-Agent polite pool），默认使用模块级 DEFAULT_EMAIL。
        timeout: HTTP 请求超时秒数，默认使用模块级 DEFAULT_TIMEOUT。

    Returns:
        重建后的摘要文本；未找到时返回空字符串。
    """
    if not doi:
        return ""

    resolved_email: str = (email or DEFAULT_EMAIL or "").strip()
    if not resolved_email or _PLACEHOLDER_PATTERN.search(resolved_email):
        logger.warning(
            "get_openalex_abstract: email 未配置或为占位符（%r），"
            "将使用无 polite pool 模式请求 OpenAlex（DOI: %s）。",
            resolved_email or "(空)",
            doi,
        )
        user_agent: str = "python-requests/oa_fetcher"
    else:
        user_agent = f"mailto:{resolved_email}"

    session: requests.Session = _create_session()
    try:
        resp: requests.Response = session.get(
            f"https://api.openalex.org/works/doi:{doi}",
            params={"select": "abstract_inverted_index,title"},
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(
                "OpenAlex 返回非200状态码 %d（DOI: %s）。",
                resp.status_code,
                doi,
            )
            return ""
        data: dict[str, Any] = resp.json()
        inv_index: Optional[dict[str, list[int]]] = data.get("abstract_inverted_index")
        if not inv_index:
            return ""
        words: dict[int, str] = {}
        for word, positions in inv_index.items():
            for pos in positions:
                words[pos] = word
        return " ".join(words[i] for i in sorted(words))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning(
            "OpenAlex 网络错误，重试耗尽仍失败（DOI: %s）：%s",
            doi,
            e,
            exc_info=True,
        )
        return ""
    except Exception as e:
        logger.warning(
            "OpenAlex 查询发生未预期异常（DOI: %s）：%s",
            doi,
            e,
            exc_info=True,
        )
        return ""
    finally:
        session.close()