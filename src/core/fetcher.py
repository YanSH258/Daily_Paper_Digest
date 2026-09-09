"""
fetcher.py - RSS 抓取 + 分层全文获取模块

全文获取优先级：
  0. Unpaywall OA 链接（开放获取）
  1. HTML 轻量请求（RequestManager）
  2. 浏览器渲染（DrissionPage，支持出版商特定交互）
  3. OpenAlex 摘要补全（兜底）

出版商识别通过 DOI 前缀完成，解析逻辑参考 sisyphus 项目。
"""
import re
import ssl
import json
import time
import logging
import threading
import concurrent.futures
import feedparser
import requests
import urllib.request
from urllib.parse import urlparse, quote
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional
from bs4 import BeautifulSoup, Tag

from fetchers import fetch_html, FetchResult, FetchStatus, BestFormat
from fetchers.models import MAX_FULLTEXT_CHARS, MAX_STORED_FULLTEXT_CHARS  # 统一使用 fetchers.models 中的常量
from fetchers.network import get_proxies
from fetchers.oa_fetcher import get_oa_url, get_openalex_abstract
from core.request_manager import RequestManager

logger = logging.getLogger(__name__)

# ── DOI 前缀 → 出版商映射（参考 sisyphus）──────────────────────
DOI_PUBLISHER_MAP = {
    "10.1039": "RSC",   # R13: 删除重复的 10.1039 条目，保留一个
    "10.1021": "ACS",
    "10.1002": "Wiley",
    "10.1016": "Elsevier",
    "10.1038": "Nature",
    "10.1063": "AIP",
    "10.1103": "APS",
    "10.1107": "IUCr",
    "10.1126": "Science",
    "10.1134": "Springer",
    "10.1140": "Springer",
    "10.1007": "Springer",
    "10.3390": "MDPI",
    "10.26434": "ChemRxiv",
    "10.48550": "arXiv",
}

# ── URL 主机名 → 出版商映射（Web 端导入订阅时自动识别）────────
PUBLISHER_BY_HOST = {
    "pubs.acs.org": "ACS",
    "acs.org": "ACS",
    "onlinelibrary.wiley.com": "Wiley",
    "wiley.com": "Wiley",
    "nature.com": "Nature",
    "link.springer.com": "Springer",
    "springer.com": "Springer",
    "sciencedirect.com": "Elsevier",
    "elsevier.com": "Elsevier",
    "feeds.rsc.org": "RSC",
    "pubs.rsc.org": "RSC",
    "rsc.org": "RSC",
    "pubs.aip.org": "AIP",
    "aip.org": "AIP",
    "feeds.aps.org": "APS",
    "aps.org": "APS",
    "iopscience.iop.org": "IOP",
    "iop.org": "IOP",
    "mdpi.com": "MDPI",
    "science.org": "Science",
    "sciencemag.org": "Science",
    "arxiv.org": "arXiv",
    "chemrxiv.org": "ChemRxiv",
}


def detect_publisher_from_url(url: str) -> str:
    """根据 RSS URL 的主机名识别出版商，无法识别时返回 DEFAULT。"""
    if not url:
        return "DEFAULT"
    host = (urlparse(url).netloc or "").lower()
    for key, publisher in PUBLISHER_BY_HOST.items():
        if host == key or host.endswith("." + key):
            return publisher
    return "DEFAULT"


# ── CSS 选择器（轻量请求用）────────────────────────────────────
PUBLISHER_SELECTORS = {
    "ACS": {
        "fulltext": ["div.article_content p", "div.hlFld-Fulltext p"],
        "abstract": ["div.article_abstract p", "div.abstractSection p", "p.articleBody_abstractText"],
    },
    "Wiley": {
        "fulltext": ["section.article-section__content p", "div.article-section__content p"],
        "abstract": ["div.abstract-group p", "section.article-section__abstract p"],
    },
    "Nature": {
        "fulltext": ["div.c-article-body p", "div#article-body p"],
        "abstract": ["div#Abs1-content p", "div.c-article-section__content p"],
    },
    "AIP": {
        "fulltext": ["div[class*='body'] p", "div.article-content p"],
        "abstract": ["section[class*='abstract'] p", "div.abstract p"],
    },
    "APS": {
        "fulltext": [
            "div.article-body-blk p",
            "section.article-body p",
            "div[class*='article-text'] p",
            "div.prose p",
        ],
        "abstract": [
            "div.abstract-content p",
            "section[class*='abstract'] p",
            "div.article-lead-blk p",
        ],
    },
    "Elsevier": {
        "fulltext": ["div#body p", "div.Body p", "article p", "div.article-wrapper p"],
        "abstract": ["div.Abstracts p", "div.abstract p"],
    },
    "IOP": {
        "fulltext": ["div.article-text p", "div.wd-jnl-art-body p"],
        "abstract": ["div.article-text-abstract p"],
    },
    "RSC": {
        "fulltext": ["div.capsule__column-wrapper p", "div.article__content p"],
        "abstract": ["div.capsule__column-wrapper p", "div.abstract p"],
    },
    "Springer": {
        "fulltext": ["div.c-article-body p", "div#body p"],
        "abstract": ["div#Abs1-content p", "section.Abstract p"],
    },
    "MDPI": {
        "fulltext": ["div.html-body p", "section.html-body p"],
        "abstract": ["div.art-abstract p"],
    },
    "Science": {
        "fulltext": ["div.article__body p", "div.section p"],
        "abstract": ["div.article__lead p", "section.abstract p"],
    },
}

RSS_ONLY_PUBLISHERS = set()

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
WILEY_RSS_HEADERS  = {**RSS_HEADERS, "Referer": "https://onlinelibrary.wiley.com/"}
NATURE_RSS_HEADERS = {**RSS_HEADERS, "Referer": "https://www.nature.com/"}

# R2: MAX_FULLTEXT_CHARS 已从 fetchers.models 导入，不再本地定义
# MAX_ABSTRACT_CHARS 在 fetchers.models 中不存在，保留本地定义
MAX_ABSTRACT_CHARS = 3000

# _page_cache 大小上限（R14）
_PAGE_CACHE_MAX_SIZE = 500

# 需要从正文中清除的噪音标签（参考 sisyphus 的清洗逻辑）
_NOISE_TAGS = [
    "figure", "table", "sup", "sub", "script", "style",
    "nav", "header", "footer", "aside", "cite",
]
# 需要清除的噪音文本模式
_NOISE_PATTERNS = [
    r'^\s*\d+\s*$',                          # 纯数字行（页码/角标）
    r'^\s*Fig\.?\s*\d+',                     # 图注
    r'^\s*Table\s*\d+',                      # 表注
    r'^\s*Scheme\s*\d+',                     # 反应式注
    r'https?://\S+',                         # URL
    r'^\s*©.*$',                             # 版权声明
    r'^\s*Received:.*Accepted:.*$',          # 投稿日期
    r'^\s*DOI:.*$',                          # DOI 行
]


def _make_lenient_ssl_context() -> ssl.SSLContext:  # R3
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


def _get_publisher_from_doi(doi: str) -> str:
    """通过 DOI 前缀识别出版商，比 RSS 里的 publisher 字段更可靠"""
    if not doi:
        return "DEFAULT"
    for prefix, publisher in DOI_PUBLISHER_MAP.items():
        if doi.startswith(prefix):
            return publisher
    return "DEFAULT"


def _clean_text(text: str) -> str:
    """清洗提取的正文文本，去除噪音（参考 sisyphus 后处理逻辑）"""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 过滤噪音行
        if any(re.match(p, line, re.IGNORECASE) for p in _NOISE_PATTERNS):
            continue
        cleaned.append(line)
    result = ' '.join(cleaned)
    result = re.sub(r'\s+', ' ', result).strip()
    return result


def _parse_html_by_publisher(html: str, publisher: str) -> str:  # R3
    """
    针对不同出版商的精确 HTML 解析（参考 sisyphus article_constr.py）。
    先移除噪音标签，再提取正文容器，最后清洗文本。
    """
    soup = BeautifulSoup(html, "lxml")

    # 移除所有噪音标签
    for tag in _NOISE_TAGS:
        for el in soup.find_all(tag):
            el.decompose()

    text = ""

    if publisher == "ACS":
        container = soup.find("div", class_=re.compile(r"hlFld-Fulltext|article_content"))
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "RSC":
        # RSC 点击 Article HTML 后的页面结构
        container = soup.find("div", class_=re.compile(r"article__content|capsule__column"))
        if not container:
            container = soup.find("article")
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "Wiley":
        container = soup.find("section", class_=re.compile(r"article-section__content"))
        if not container:
            container = soup.find("div", class_=re.compile(r"article-section__content"))
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "Nature":
        container = soup.find("div", class_=re.compile(r"c-article-body"))
        if not container:
            container = soup.find("div", id="article-body")
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "Elsevier":
        container = soup.find("div", id=re.compile(r"^body$|^Body$"))
        if not container:
            container = soup.find("article")
        if container:
            # Elsevier 特别处理：去掉参考文献 section
            refs = container.find("section", class_=re.compile(r"reference|bibliography", re.I))
            if refs:
                refs.decompose()
            text = container.get_text(" ", strip=True)

    elif publisher == "Springer":
        container = soup.find("div", class_=re.compile(r"c-article-body"))
        if not container:
            container = soup.find("div", id="body")
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "AIP":
        container = soup.find("div", class_=re.compile(r"article-content|body"))
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "APS":
        container = soup.find("div", class_=re.compile(r"article-body|prose"))
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "MDPI":
        container = soup.find("div", class_=re.compile(r"html-body"))
        if container:
            text = container.get_text(" ", strip=True)

    elif publisher == "Science":
        container = soup.find("div", class_=re.compile(r"article__body"))
        if container:
            text = container.get_text(" ", strip=True)

    # 通用回退：找最大的文本容器
    if not text or len(text) < 500:
        candidates = []
        for tag in ["article", "main", "div"]:
            for el in soup.find_all(tag):
                t = el.get_text(" ", strip=True)
                if len(t) > len(text):
                    candidates.append((len(t), t))
        if candidates:
            candidates.sort(reverse=True)
            text = candidates[0][1]

    if not text:
        return ""

    # 入库存储放宽到 MAX_STORED_FULLTEXT_CHARS，LLM 输入预算由 analyzer._smart_chunk 控制
    cleaned = _clean_text(text)
    return cleaned[:MAX_STORED_FULLTEXT_CHARS] if len(cleaned) > MAX_STORED_FULLTEXT_CHARS else cleaned


class BrowserDriver:
    _page: Optional[Any] = None  # R2: 添加类变量类型注解

    @classmethod
    def get(cls) -> Any:  # R3: ChromiumPage，用 Any 避免强依赖 DrissionPage 类型
        if cls._page is None:
            cls._page = cls._create()
        return cls._page

    @classmethod
    def _create(cls) -> Any:  # R3: 返回 ChromiumPage，用 Any
        from DrissionPage import ChromiumPage, ChromiumOptions

        co = ChromiumOptions()
        co.headless(True)
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--ignore-certificate-errors")
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = ChromiumPage(addr_or_opts=co)
        page.timeouts.page_load = 30
        logger.info("DrissionPage Chromium 驱动已启动")
        return page

    @classmethod
    def inject_cookies_if_needed(cls, page: Any, url: str) -> None:  # R3
        cookie_file = Path("cookies.json")
        if cookie_file.exists():
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                page.set.cookies(cookies)
            except Exception:
                pass

    @classmethod
    def restart(cls) -> Any:  # R3: 返回 ChromiumPage，用 Any
        cls.quit()
        return cls.get()

    @classmethod
    def quit(cls) -> None:  # R3
        if cls._page:
            try:
                cls._page.quit()
            except Exception:
                pass
            cls._page = None


class JournalFetcher:
    def __init__(self, config: dict) -> None:  # R3
        self.config = config
        self.fetcher_cfg = config.get("fetcher", {})
        self.timeout = self.fetcher_cfg.get("request_timeout", 20)
        self.retry = self.fetcher_cfg.get("retry_times", 3)
        self.delay = self.fetcher_cfg.get("delay_between_requests", 2)
        self.max_per_journal = self.fetcher_cfg.get("max_articles_per_journal", 30)
        self.date_filter_days = self.fetcher_cfg.get("date_filter_days", 2)
        self.unpaywall_email = config.get("unpaywall_email", "your@email.com")
        self.use_browser = self.fetcher_cfg.get("use_browser", True)
        self._page_cache: dict[str, str] = {}  # R3: 添加类型注解
        self._cache_lock = threading.Lock()  # R1: 独立的缓存锁

        perf_cfg = config.get("performance", {})
        self.concurrency = perf_cfg.get("concurrency", 5)
        self._request_manager = RequestManager(config=config)
        self._browser_lock = threading.Lock()

    def fetch_all(self, health_callback=None) -> list[dict]:
        """并发抓取所有订阅源（RSS / arXiv / OpenAlex 检索式）。

        health_callback(journal_id, ok, error="") 用于更新源健康度。
        """
        journals = self.config.get("journals", [])
        concurrency = max(1, int((self.config.get("performance", {}) or {})
                                 .get("rss_concurrency", 4)))
        results: list[list[dict]] = [[] for _ in journals]

        def _one(idx_journal: tuple[int, dict]) -> None:
            idx, journal = idx_journal
            name = journal.get("name", "Unknown")
            source_type = journal.get("source_type", "rss")
            try:
                if source_type == "arxiv":
                    arts = self._fetch_arxiv_source(journal)
                elif source_type == "openalex":
                    arts = self._fetch_openalex_source(journal)
                else:
                    rss_url = journal.get("rss", "")
                    if not rss_url:
                        return
                    arts = self._fetch_journal(journal)
                results[idx] = arts
                logger.info(f"正在抓取: {name} → {len(arts)} 篇")
                if health_callback and journal.get("id"):
                    health_callback(journal["id"], True)
            except Exception as e:  # noqa: BLE001 - 单源失败不阻断其他源
                logger.error(f"  {name} 抓取失败: {e}")
                if health_callback and journal.get("id"):
                    health_callback(journal["id"], False, str(e))

        if concurrency > 1 and len(journals) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(concurrency, len(journals))) as executor:
                list(executor.map(_one, enumerate(journals)))
        else:
            for idx, journal in enumerate(journals):
                _one((idx, journal))
        return [a for r in results for a in r]

    def _fetch_arxiv_source(self, journal: dict) -> list[dict]:
        """arXiv API 分类订阅（query 如 cat:cond-mat.mtrl-sci）。"""
        query = journal.get("query") or journal.get("rss") or ""
        per_max = int(journal.get("max_articles", self.max_per_journal))
        url = (f"http://export.arxiv.org/api/query?search_query={quote(query)}"
               f"&sortBy=submittedDate&sortOrder=descending&max_results={per_max}")
        feed = feedparser.parse(url)
        if not feed or not feed.entries:
            return []
        articles = []
        for entry in feed.entries[:per_max]:
            art = self._parse_entry(entry, journal.get("name", "arXiv"), "arXiv")
            if art:
                art["url"] = entry.get("link", art.get("url", ""))
                articles.append(art)
        return self._apply_date_filter(articles)

    def _fetch_openalex_source(self, journal: dict) -> list[dict]:
        """OpenAlex 检索式订阅，按 last_run 水位线增量拉取。"""
        from integrations import openalex as oa
        openalex.set_polite_email(self.unpaywall_email)
        query = journal.get("query") or journal.get("rss") or ""
        from_date = journal.get("last_run") or ""
        works = oa.search_works(query, from_date=from_date,
                                limit=int(journal.get("max_articles", self.max_per_journal)))
        articles = []
        for w in works:
            articles.append({
                "title": w["title"],
                "journal": journal.get("name", w.get("journal", "")),
                "publisher": "DEFAULT",
                "url": w["url"],
                "doi": w["doi"],
                "abstract": w["abstract"],
                "authors": w["authors"],
                "pub_date": w["pub_date"] or datetime.now().strftime("%Y-%m-%d"),
                "has_fulltext": False,
                "cited_count": w.get("cited_count"),
                "discovered_via": "openalex_query",
            })
        return articles

    def _apply_date_filter(self, articles: list[dict]) -> list[dict]:
        if self.date_filter_days > 0:
            cutoff = (datetime.now() - timedelta(days=self.date_filter_days)).strftime("%Y-%m-%d")
            return [a for a in articles if a.get("pub_date", "9999") >= cutoff]
        return articles

    def test_feed(self, rss_url: str, publisher: str = "DEFAULT") -> dict:
        """测试某个 RSS 链接是否可抓取（供 Web 端「测试链接」使用）。"""
        if not rss_url:
            return {"ok": False, "error": "RSS 链接为空", "count": 0, "sample": []}
        try:
            feed = self._fetch_rss(rss_url, publisher)
        except Exception as e:
            logger.warning(f"测试 RSS 失败: {rss_url} - {e}")
            return {"ok": False, "error": f"抓取异常: {e}", "count": 0, "sample": []}

        if not feed or not feed.entries:
            return {
                "ok": False,
                "error": "无法获取 RSS 或没有条目（可能是链接无效或需要登录）",
                "count": 0,
                "sample": [],
            }

        feed_title = getattr(feed.feed, "title", "") or ""
        samples = [
            re.sub(r"\s+", " ", BeautifulSoup(entry.get("title", ""), "html.parser").get_text()).strip()[:80]
            for entry in feed.entries[:5]
        ]
        return {
            "ok": True,
            "feed_title": feed_title,
            "count": len(feed.entries),
            "sample": samples,
        }

    def fetch_fulltext(self, article: dict) -> str:
        return self.fetch_fulltext_with_status(article).text

    def fetch_fulltext_with_status(self, article: dict) -> FetchResult:
        """
        分层全文获取，回退顺序：
          0. Unpaywall OA 链接
          1. HTML 轻量请求（DOI 规范 URL）
          2. 浏览器渲染（出版商特定交互）
          3. OpenAlex 摘要补全
        """
        doi = article.get("doi", "")
        url = article.get("url", "")

        # ★ 通过 DOI 前缀识别出版商，比 RSS 字段更可靠
        publisher = _get_publisher_from_doi(doi) or article.get("publisher", "DEFAULT")

        if not url or publisher in RSS_ONLY_PUBLISHERS:
            return FetchResult(fetch_status=FetchStatus.NO_HTML_URL, error_code="NO_HTML_URL")

        # DOI 规范 URL，让出版商服务器自动重定向
        canonical_url = f"https://doi.org/{doi}" if doi else url
        selectors = PUBLISHER_SELECTORS.get(publisher, {})

        # ── 0. Unpaywall OA 链接 ────────────────────────────────
        if doi:
            oa_url = get_oa_url(doi, email=self.unpaywall_email)
            if oa_url:
                logger.info(f"  Unpaywall 找到 OA 链接: {oa_url[:70]}")
                # ★ arXiv PDF → 转换为 HTML abs 页面
                if "arxiv.org/pdf" in oa_url:
                    oa_url = re.sub(r'\.pdf$', '', oa_url).replace("/pdf/", "/abs/")
                    logger.info(f"  arXiv PDF → HTML: {oa_url}")
                oa_result = fetch_html(oa_url, timeout=self.timeout,
                                       request_manager=self._request_manager)
                if oa_result.fetch_status == FetchStatus.SUCCESS:
                    oa_result.source_url = oa_url
                    logger.info(f"    ✓ OA 全文 {len(oa_result.text)} 字")
                    return oa_result
                else:
                    logger.warning(f"  Unpaywall OA 链接获取失败: {oa_url}")  # R7

        # ── 1. HTML 轻量请求 ────────────────────────────────────
        html_result = fetch_html(canonical_url, timeout=self.timeout,
                                 selectors=selectors,
                                 request_manager=self._request_manager)
        if html_result.fetch_status == FetchStatus.SUCCESS:
            html_result.source_url = canonical_url
            return html_result

        logger.warning(f"  HTML 请求失败 ({html_result.error_code})，尝试浏览器渲染...")  # R8

        # ── 2. 浏览器渲染 ───────────────────────────────────────
        if not self.use_browser:
            logger.info("  浏览器渲染已禁用（use_browser=false），跳过")
        else:
            browser_text = self._fetch_page_content(canonical_url, publisher)
            if browser_text:
                return FetchResult(
                    text=browser_text,
                    best_available_format=BestFormat.HTML_FULLTEXT,
                    fetch_status=FetchStatus.SUCCESS,
                    network_mode=html_result.network_mode,
                    access_path=html_result.access_path,
                    source_url=canonical_url,
                )

        # ── 3. OpenAlex 摘要补全 ────────────────────────────────
        if doi and len(article.get("abstract", "")) < 200:
            oa_abstract = get_openalex_abstract(doi)
            if oa_abstract:
                logger.info(f"  OpenAlex 补全摘要 ({len(oa_abstract)} 字)")
                return FetchResult(
                    text=oa_abstract,
                    best_available_format=BestFormat.ABSTRACT_ONLY,
                    fetch_status=FetchStatus.SUCCESS,
                    network_mode=html_result.network_mode,
                    access_path=html_result.access_path,
                    source_url=f"https://doi.org/{doi}",
                )

        logger.warning(f"  全文获取失败 ({html_result.error_code})，将使用摘要。")  # R9
        return FetchResult(
            fetch_status=FetchStatus.HTML_FETCH_FAIL,
            error_code=f"ALL_METHODS_FAILED:{html_result.error_code}",
            network_mode=html_result.network_mode,
            access_path=html_result.access_path,
        )

    def close(self) -> None:  # R3
        BrowserDriver.quit()

    def fetch_fulltext_batch(self, articles: list[dict]) -> list[FetchResult]:  # R4: 参数改为 list[dict]
        if not articles:
            return []

        results: list[Optional[FetchResult]] = [None] * len(articles)

        def fetch_one(idx_article: tuple[int, dict]) -> None:  # R5: 添加参数注解
            idx, article = idx_article
            try:
                results[idx] = self.fetch_fulltext_with_status(article)
            except Exception as e:
                logger.error(f"  并发全文获取异常: {e}")
                results[idx] = FetchResult(
                    fetch_status=FetchStatus.HTML_FETCH_FAIL,
                    error_code=f"CONCURRENT_FETCH_ERROR:{e}",
                )

        max_workers = min(self.concurrency, len(articles))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(fetch_one, enumerate(articles)))

        return results

    def _fetch_journal(self, journal: dict) -> list[dict]:  # R3
        rss_url = journal["rss"]
        journal_name = journal["name"]
        publisher = journal.get("publisher", "DEFAULT")
        per_max = journal.get("max_articles", self.max_per_journal)

        feed = self._fetch_rss(rss_url, publisher)
        if not feed or not feed.entries:
            return []

        articles = []
        for entry in feed.entries[:per_max]:
            art = self._parse_entry(entry, journal_name, publisher)
            if art:
                articles.append(art)

        if self.date_filter_days > 0:
            cutoff = (datetime.now() - timedelta(days=self.date_filter_days)).strftime("%Y-%m-%d")
            articles = [a for a in articles if a.get("pub_date", "9999") >= cutoff]
        return articles

    def _fetch_rss(self, rss_url: str, publisher: str) -> Optional[Any]:  # R3: Optional[feedparser.FeedParserDict]，用 Optional[Any]
        if publisher == "RSC":
            for attempt in range(self.retry):
                try:
                    req = urllib.request.Request(rss_url, headers=RSS_HEADERS)
                    # HTTP 链接不传 SSL context；HTTPS 链接用宽松 SSL context
                    if rss_url.startswith("https://"):
                        ssl_ctx = _make_lenient_ssl_context()
                        resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=self.timeout)
                    else:
                        resp = urllib.request.urlopen(req, timeout=self.timeout)
                    with resp as r:
                        feed = feedparser.parse(r.read())
                    if feed.entries:
                        return feed
                except Exception as e:
                    logger.warning(f"RSS 获取失败 (尝试 {attempt+1}/{self.retry}): {rss_url} - {e}")  # R6
                    time.sleep(2 * (attempt + 1))
            logger.warning(f"RSS 获取全部重试失败，放弃: {rss_url}")  # R6
            return None

        h = (WILEY_RSS_HEADERS if publisher == "Wiley"
             else NATURE_RSS_HEADERS if publisher == "Nature"
             else RSS_HEADERS)
        session = requests.Session()
        session.headers.update(h)
        proxies = get_proxies()
        if proxies:
            session.proxies.update(proxies)
        for attempt in range(self.retry):
            try:
                resp = session.get(rss_url, timeout=self.timeout)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                if feed.entries:
                    return feed
            except Exception as e:
                logger.warning(f"RSS 获取失败 (尝试 {attempt+1}/{self.retry}): {rss_url} - {e}")  # R6
                time.sleep(2 * (attempt + 1))
        logger.warning(f"RSS 获取全部重试失败，放弃: {rss_url}")  # R6
        return None

    def _fetch_page_content(self, url: str, publisher: str) -> str:  # R3
        # R1: 用独立的缓存锁检查缓存，避免缓存命中时阻塞在浏览器锁上
        with self._cache_lock:
            if url in self._page_cache:
                return self._page_cache[url]

        selectors = PUBLISHER_SELECTORS.get(publisher, {})

        # ★ 在 with 块外初始化，避免异常时变量未定义
        found_text = ""

        with self._browser_lock:
            try:
                page = BrowserDriver.get()
                page.get(url)
                BrowserDriver.inject_cookies_if_needed(page, url)

                # Cloudflare 等待，最多 5 秒
                wait_time = 0
                while ("Just a moment" in page.title or "Cloudflare" in page.title) and wait_time < 5:
                    time.sleep(1)
                    wait_time += 1

                if "Just a moment" in page.title or "Cloudflare" in page.title:
                    logger.warning("    ⚠ 触发 Cloudflare 盾拦截，放弃获取全文")
                    # R6: 将 restart 包裹在 try/except 中，避免重启异常覆盖原始警告
                    try:
                        BrowserDriver.restart()
                    except Exception as e:
                        logger.warning(f"浏览器重启失败: {e}")
                    # R4b: 不缓存该 URL（瞬态失败，下次应可重试）
                    return ""

                # ★ RSC：点击 Article HTML 按钮（参考 sisyphus）
                if publisher == "RSC":
                    try:
                        btn = page.ele("text:Article HTML", timeout=5)
                        if btn:
                            btn.click()
                            time.sleep(2)
                            logger.debug("  RSC: 已点击 Article HTML 按钮")
                    except Exception:
                        pass

                # ★ ACS：点击 Full text 标签
                if publisher == "ACS":
                    try:
                        btn = page.ele("css:a.tab-nav__link[href*='full']", timeout=3)
                        if btn:
                            btn.click()
                            time.sleep(1)
                            logger.debug("  ACS: 已点击 Full text 标签")
                    except Exception:
                        pass

                page.wait.load_start()
                time.sleep(1)

                html_content = page.html

                if "There was a problem providing the content you requested" in html_content:
                    logger.warning("    ⚠ 触发 Elsevier DataDome 拦截，放弃获取全文")
                    # R6: 将 restart 包裹在 try/except 中，避免重启异常覆盖原始警告
                    try:
                        BrowserDriver.restart()
                    except Exception as e:
                        logger.warning(f"浏览器重启失败: {e}")
                    # R5: 不缓存该 URL（瞬态失败，下次应可重试）
                    return ""
                else:
                    # ★ 使用精确的出版商解析器（参考 sisyphus article_constr.py）
                    found_text = _parse_html_by_publisher(html_content, publisher)

                    # 回退到通用 CSS 选择器
                    if not found_text and selectors:
                        soup = BeautifulSoup(html_content, "html.parser")
                        for sel in selectors.get("fulltext", []):
                            paras = soup.select(sel)
                            if len(paras) >= 3:
                                text = re.sub(r'\s+', ' ', " ".join(
                                    p.get_text(strip=True) for p in paras)).strip()
                                if len(text) > 500:
                                    found_text = text[:MAX_FULLTEXT_CHARS]
                                    break

                    if found_text:
                        logger.info(f"    ✓ 浏览器全文 {len(found_text)} 字 [{publisher}]")
                    else:
                        logger.warning(f"    ⚠ 浏览器渲染无结果 [{publisher}]")  # R11

            except Exception as e:
                err_str = str(e)
                if "timeout" in err_str.lower() or "断开" in err_str:
                    logger.warning(f"浏览器加载超时，尝试重启驱动: {e}")
                    try:
                        BrowserDriver.restart()
                    except Exception as re_e:
                        logger.warning(f"浏览器重启失败: {re_e}")
                else:
                    logger.warning(f"浏览器渲染失败 [{publisher}]: {e}")

        with self._cache_lock:
            # FIFO 淘汰，避免缓存无上限增长
            if len(self._page_cache) >= _PAGE_CACHE_MAX_SIZE:
                self._page_cache.pop(next(iter(self._page_cache)))
            self._page_cache[url] = found_text
        return found_text

    def _parse_entry(self, entry, journal_name: str, publisher: str) -> Optional[dict]:
        try:
            raw_title = BeautifulSoup(entry.get("title", ""), "html.parser").get_text()
            # 清洗 LaTeX 数学符号（例如 APS 期刊的 ${\mathrm{PbZrO}}_{3}$ 等）
            raw_title = re.sub(r'\$\s*\{\s*\\mathrm\{([^}]+)\}\s*\}\s*(\^|\_)?(\{?[^}$]*\}?)?\s*\$', r'\1\3', raw_title)
            raw_title = re.sub(r'\$\s*\\mathrm\{([^}]+)\}\s*\$', r'\1', raw_title)
            raw_title = re.sub(r'\$([^\$]+)\$', lambda m: m.group(1).replace(r'\mathrm{', '').replace('}', '').replace('{', ''), raw_title)
            raw_title = re.sub(r'\\math[a-z]+\{([^}]+)\}', r'\1', raw_title)
            raw_title = re.sub(r'[_^]\{([^}]+)\}', r'\1', raw_title)
            title = re.sub(r'\s+', ' ', raw_title).strip()
            if not title or title.lower() in ("addition/correction", "correction") \
                    or title.startswith("[ASAP]"):
                return None
            url = entry.get("link", "")
            doi = self._extract_doi(entry, url)
            detected_publisher = _get_publisher_from_doi(doi) or publisher
            # 优先使用 RSS 条目的真实发布日期，缺省时回退为运行当天
            pub_date = datetime.now().strftime("%Y-%m-%d")
            for date_field in ("published_parsed", "updated_parsed"):
                parsed = entry.get(date_field)
                if parsed:
                    pub_date = datetime(*parsed[:6]).strftime("%Y-%m-%d")
                    break
            return {
                "title":        title,
                "journal":      journal_name,
                "publisher":    detected_publisher,
                "url":          url,
                "doi":          doi,
                "abstract":     self._extract_rss_abstract(entry),
                "authors":      self._extract_authors(entry),
                "pub_date":     pub_date,
                "has_fulltext": False,
            }
        except Exception as e:
            logger.warning(f"RSS 条目解析失败，跳过: {e}")
            return None

    def _extract_rss_abstract(self, entry) -> str:
        for field in ("summary", "description"):
            text = entry.get(field, "")
            if text:
                clean = re.sub(r'\s+', ' ', BeautifulSoup(
                    text, "html.parser").get_text()).strip()
                if len(clean) > 100 and not re.match(
                        r'^(Journal of|ACS |Nano |Angewandte|Chemical Science|Nature\s)', clean):
                    return clean[:MAX_ABSTRACT_CHARS]
        return ""

    def _extract_doi(self, entry, url: str) -> str:
        for field in ("prism_doi", "dc_identifier", "id"):
            val = str(getattr(entry, field, "") or entry.get(field, ""))
            if val and "10." in val:
                m = re.search(r'10\.\d{4,}/\S+', val)
                if m:
                    return m.group(0).rstrip(".")
        m = re.search(r'10\.\d{4,}/[^\s&?#]+', url)
        return m.group(0).rstrip(".") if m else ""

    def _extract_authors(self, entry) -> list[str]:
        authors: list[str] = []
        if hasattr(entry, "authors"):
            authors = [a.get("name", "").strip()
                       for a in entry.authors if a.get("name", "").strip()]
        if not authors and hasattr(entry, "author"):
            authors = [a.strip() for a in entry.author.split(",") if a.strip()]
        return authors[:10]