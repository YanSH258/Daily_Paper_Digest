"""
fetchers/html_fetcher.py - 轻量 HTTP 请求优先的 HTML 全文获取

策略：
  1. 使用调用方传入的 RequestManager（多层级降级 + UA 轮换 + 速率限制）GET 目标 URL
     （如果未传入 RequestManager，则回退到直接 requests.get）
  2. 检查 Content-Type，确认为 HTML/XML
  3. 依次尝试：publisher 特定选择器 → 通用容器选择器 → 全页 <p> 回退
  4. 文本不足时返回 HTML_FETCH_FAIL（调用方可随后尝试浏览器 / PDF）
"""
import re
import logging
from typing import TYPE_CHECKING

import requests
from bs4 import BeautifulSoup

from .models import FetchResult, FetchStatus, BestFormat, MAX_STORED_FULLTEXT_CHARS, MIN_FULLTEXT_LEN, MIN_ABSTRACT_LEN
from .network import get_network_info, DEFAULT_UA

if TYPE_CHECKING:
    from core.request_manager import RequestManager

logger = logging.getLogger(__name__)

HTML_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# 通用容器选择器（按优先级排列）
_GENERIC_CONTAINERS = [
    "article",
    "main",
    "[role='main']",
    "div.article-body",
    "div.full-text",
    "div#full-text",
    "div.body",
    "div#body",
    "section.body",
]


def fetch_html(url: str, timeout: int = 20, selectors: dict = None,
               request_manager: "RequestManager" = None) -> FetchResult:
    """
    尝试获取页面 HTML 全文。

    优先使用传入的 RequestManager（带 UA 轮换 + 速率限制 + 多层降级），
    否则回退到直接 requests.get。

    Args:
        url:             目标 URL
        timeout:         请求超时（秒）
        selectors:       publisher 特定 CSS 选择器，格式同 PUBLISHER_SELECTORS
                         {"fulltext": [...], "abstract": [...]}
        request_manager: RequestManager 实例（可选），由调用方（JournalFetcher）传入

    Returns:
        FetchResult：包含 text、best_available_format、fetch_status、
                     network_mode、access_path 等字段
    """
    if not url:
        return FetchResult(fetch_status=FetchStatus.NO_HTML_URL, error_code="NO_HTML_URL")

    net = get_network_info()
    proxies = net.get("proxies")

    # 优先使用调用方传入的 RequestManager（带 UA 轮换和速率限制）
    if request_manager is not None:
        result = request_manager.get(url, proxies=proxies)
        if not result.ok:
            err = result.error or "HTTP_ERROR"
            if result.response is not None:
                code = result.response.status_code
                logger.warning(f"  HTML 请求 HTTP 错误 {code}: {url}")
                err = f"HTTP_{code}"
            elif "TIMEOUT" in (result.error or ""):
                logger.warning(f"  HTML 请求超时: {url}")
                err = "HTML_TIMEOUT"
            else:
                logger.warning(f"  HTML 请求失败: {result.error}")
            return FetchResult(
                fetch_status=FetchStatus.HTML_FETCH_FAIL,
                error_code=err,
                network_mode=net["network_mode"],
                access_path=net["access_path"],
            )
        resp = result.response
        ctype = resp.headers.get("Content-Type", "").lower()
        if "html" not in ctype and "xml" not in ctype:
            logger.info(f"  非 HTML/XML 内容类型: {ctype}")
            return FetchResult(
                fetch_status=FetchStatus.HTML_FETCH_FAIL,
                error_code=f"NOT_HTML:{ctype}",
                network_mode=net["network_mode"],
                access_path=net["access_path"],
            )
        text, is_fulltext = _extract_text(resp.text, selectors or {})
        if text:
            logger.info(
                f"  HTML 提取成功 ({len(text)} 字, {'全文' if is_fulltext else '仅摘要'}, {net['network_mode']})")
            return FetchResult(
                text=text,
                best_available_format=(BestFormat.HTML_FULLTEXT if is_fulltext
                                       else BestFormat.ABSTRACT_ONLY),
                fetch_status=FetchStatus.SUCCESS,
                network_mode=net["network_mode"],
                access_path=net["access_path"],
            )
        logger.info("  HTML 页面文本过短或需 JS 渲染，转交浏览器层处理")
        return FetchResult(
            fetch_status=FetchStatus.HTML_FETCH_FAIL,
            error_code="HTML_TOO_SHORT_OR_JS_GATED",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )

    # 降级：直接用 requests（无 RequestManager 时的兜底）
    try:
        resp = requests.get(
            url,
            headers=HTML_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logger.warning(f"  HTML 请求超时: {url}")
        return FetchResult(
            fetch_status=FetchStatus.HTML_FETCH_FAIL,
            error_code="HTML_TIMEOUT",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        logger.warning(f"  HTML 请求 HTTP 错误 {code}: {url}")
        return FetchResult(
            fetch_status=FetchStatus.HTML_FETCH_FAIL,
            error_code=f"HTTP_{code}",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )
    except Exception as e:
        logger.warning(f"  HTML 请求异常: {e}")
        return FetchResult(
            fetch_status=FetchStatus.HTML_FETCH_FAIL,
            error_code="HTML_FETCH_FAIL",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )

    ctype = resp.headers.get("Content-Type", "").lower()
    if "html" not in ctype and "xml" not in ctype:
        logger.info(f"  非 HTML/XML 内容类型: {ctype}")
        return FetchResult(
            fetch_status=FetchStatus.HTML_FETCH_FAIL,
            error_code=f"NOT_HTML:{ctype}",
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )

    text, is_fulltext = _extract_text(resp.text, selectors or {})
    if text:
        logger.info(
            f"  HTML 提取成功 ({len(text)} 字, {'全文' if is_fulltext else '仅摘要'}, {net['network_mode']})")
        return FetchResult(
            text=text,
            best_available_format=(BestFormat.HTML_FULLTEXT if is_fulltext
                                   else BestFormat.ABSTRACT_ONLY),
            fetch_status=FetchStatus.SUCCESS,
            network_mode=net["network_mode"],
            access_path=net["access_path"],
        )

    logger.info("  HTML 页面文本过短或需 JS 渲染，转交浏览器层处理")
    return FetchResult(
        fetch_status=FetchStatus.HTML_FETCH_FAIL,
        error_code="HTML_TOO_SHORT_OR_JS_GATED",
        network_mode=net["network_mode"],
        access_path=net["access_path"],
    )


def _extract_text(html: str, selectors: dict) -> tuple[str, bool]:
    """从 HTML 提取正文文本，返回 (text, is_fulltext)。

    按优先级尝试：
      1. publisher 全文选择器（≥MIN_FULLTEXT_LEN → 全文）
      2. publisher 摘要选择器（命中即摘要级，无论多长都不冒充全文）
      3. 通用容器选择器 / 4. 全页 <p> 回退（≥MIN_FULLTEXT_LEN → 全文）
    """
    soup = BeautifulSoup(html, "lxml")

    # 1. Publisher 全文选择器
    for sel in selectors.get("fulltext", []):
        paras = soup.select(sel)
        if len(paras) >= 3:
            text = re.sub(r'\s+', ' ', " ".join(p.get_text(strip=True) for p in paras)).strip()
            if len(text) >= MIN_FULLTEXT_LEN:
                return text[:MAX_STORED_FULLTEXT_CHARS], True

    # 2. Publisher 摘要选择器（作为降级）：摘要选择器命中的就是摘要，
    #    不能因为"够长"就当成全文——那会伪造证据等级并阻断浏览器全文回退
    for sel in selectors.get("abstract", []):
        paras = soup.select(sel)
        if paras:
            text = re.sub(r'\s+', ' ', " ".join(p.get_text(strip=True) for p in paras)).strip()
            if len(text) >= MIN_ABSTRACT_LEN:
                return text[:MAX_STORED_FULLTEXT_CHARS], False

    # 3. 通用容器选择器
    for container_sel in _GENERIC_CONTAINERS:
        container = soup.select_one(container_sel)
        if container:
            text = re.sub(r'\s+', ' ', container.get_text(" ", strip=True)).strip()
            if len(text) >= MIN_FULLTEXT_LEN:
                return text[:MAX_STORED_FULLTEXT_CHARS], True

    # 4. 全页 <p> 回退
    paras = soup.find_all("p")
    if paras:
        text = re.sub(r'\s+', ' ', " ".join(p.get_text(strip=True) for p in paras)).strip()
        if len(text) >= MIN_FULLTEXT_LEN:
            return text[:MAX_STORED_FULLTEXT_CHARS], True

    return "", False
