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
from datetime import datetime, timedelta, date
from typing import Any, Optional
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup, Tag

from fetchers import fetch_html, FetchResult, FetchStatus, BestFormat
from fetchers.models import (MAX_FULLTEXT_CHARS, MAX_STORED_FULLTEXT_CHARS,
                             MIN_FULLTEXT_LEN)  # 统一使用 fetchers.models 中的常量
from fetchers.network import get_proxies
from fetchers.oa_fetcher import get_oa_url, get_openalex_abstract
from integrations import arxiv, openalex
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


def _extract_arxiv_id(text: str) -> str:
    """从 URL 或 DOI 中提取 arXiv 编号（如 2609.00580v1）；非 arXiv 返回空。"""
    if not text:
        return ""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\S+?)(?:[?#]|$)", text)
    if m:
        return m.group(1).rstrip(".")
    m = re.search(r"10\.48550/arXiv\.(\S+?)(?:[?\s]|$)", text, re.IGNORECASE)
    if m:
        return m.group(1).rstrip(".")
    return ""


def clean_feed_abstract(text: str) -> str:
    """清洗 RSS description/summary 里的出版商包装噪音（尤其 APS）。

    APS 常见形态：
      Author(s): A. Name, B. Name, and C. NameWe report ...
      ... [Phys. Rev. B 114, 175108] Published Fri Sep 04, 2026
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        return ""
    # 去掉 Author(s): 前缀，并从作者列表切到摘要正文
    if re.match(r"(?i)^author\(s\)\s*:", t):
        rest = re.sub(r"(?i)^author\(s\)\s*:\s*", "", t)
        # 在作者名与正文粘连处切开（如 FinkeldeiWe report）
        m = re.search(
            r"(?<=[a-z])(?=[A-Z][a-z]+\s+"
            r"(?:report|present|show|study|investigat|develop|demonstrat|"
            r"propos|calculat|simulat|find|use|analyz|analys|measure|"
            r"introduc|describ|perform|explore|discuss|review|apply))",
            rest,
        )
        if m:
            rest = rest[m.start():]
        else:
            # 兜底：作者名后粘一个大写词（无空格）
            m2 = re.search(r"(?<=[a-z])(?=[A-Z][a-z]{2,})", rest)
            if m2:
                rest = rest[m2.start():]
        t = rest.strip()
    # 去掉文末期刊卷期与发布日期
    t = re.sub(
        r"\s*\[[^\]]*Phys\.?\s*Rev[^]]*\]\s*Published\s+.+$",
        "", t, flags=re.I,
    )
    t = re.sub(r"\s*Published\s+\w{3}\s+\w{3}\s+\d{1,2},\s+\d{4}\s*$", "", t)
    t = re.sub(r"\s*\[Phys\.?\s*Rev\.[^\]]*\]\s*$", "", t, flags=re.I)
    return t.strip()


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
        from integrations import arxiv, openalex
        openalex.configure(config)
        arxiv.configure(config)
        self._browser_lock = threading.Lock()
        self.source_results: list[dict[str, Any]] = []
        self._status_local = threading.local()
        self._window_date = self._configured_run_date()

    def _configured_run_date(self) -> date:
        value = self.config.get("_run_date") or (self.config.get("processing", {}) or {}).get("run_date") or self.config.get("run_date")
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                pass
        scheduler = self.config.get("scheduler", {}) or {}
        try:
            return datetime.now(ZoneInfo(str(scheduler.get("timezone") or "UTC"))).date()
        except Exception:
            return datetime.now().date()

    def set_window(self, run_date) -> None:
        """Set the fixed report date used by all source windows."""
        self._window_date = run_date if isinstance(run_date, date) else date.fromisoformat(str(run_date)[:10])

    def _source_status(self) -> dict[str, Any]:
        status = getattr(self._status_local, "value", None)
        if status is None:
            status = {"started_at": datetime.now().isoformat(), "raw_count": 0}
            self._status_local.value = status
        return status

    def fetch_all(self, health_callback=None) -> list[dict]:
        """并发抓取所有订阅源（RSS / arXiv / OpenAlex 检索式）。

        health_callback(journal_id, ok, error="") 用于更新源健康度。
        """
        journals = self.config.get("journals", [])
        self.source_results = []
        concurrency = max(1, int((self.config.get("performance", {}) or {})
                                 .get("rss_concurrency", 4)))
        results: list[list[dict]] = [[] for _ in journals]

        def _one(idx_journal: tuple[int, dict]) -> None:
            idx, journal = idx_journal
            name = journal.get("name", "Unknown")
            source_type = journal.get("source_type", "rss")
            # Batch fast-fail: skip remaining throttled sources while a sibling
            # 429 cooldown is active; cursors stay and next run re-collects.
            throttled = {"openalex": openalex, "arxiv": arxiv}.get(source_type)
            if throttled is not None and throttled.is_cooling_down():
                error = f"skipped: {source_type} rate-limit cooldown active"
                logger.warning(f"  {name} {error}")
                self.source_results.append({"source_id": journal.get("id"), "id": journal.get("id"),
                                            "success": False, "complete": False, "truncated": False,
                                            "next_cursor": None, "window_start": None, "window_end": None,
                                            "started_at": None, "raw_count": 0, "returned_count": 0,
                                            "error": error})
                if health_callback and journal.get("id"):
                    health_callback(journal["id"], False, error)
                return
            try:
                self._status_local.value = {"started_at": datetime.now().isoformat(), "raw_count": 0}
                if source_type == "arxiv":
                    arts = self._fetch_arxiv_source(journal)
                elif source_type == "openalex":
                    arts = self._fetch_openalex_source(journal)
                elif source_type == "crossref":
                    arts = self._fetch_crossref_source(journal)
                else:
                    rss_url = journal.get("rss", "")
                    if not rss_url:
                        return
                    arts = self._fetch_journal(journal)
                for article in arts:
                    article["_sources"] = ["journal:" + str(journal.get("id") or name)]
                results[idx] = arts
                status = getattr(self._status_local, "value", None) or {}
                result = {"source_id": journal.get("id"), "id": journal.get("id"), "success": True,
                          "complete": not bool(status.get("truncated", False)),
                          "truncated": bool(status.get("truncated", False)),
                          "next_cursor": status.get("next_cursor"),
                          "window_start": status.get("window_start"), "window_end": status.get("window_end"),
                          "started_at": status.get("started_at"),
                          "raw_count": status.get("raw_count", len(arts)), "returned_count": len(arts),
                          "error": None}
                self.source_results.append(result)
                logger.info(f"正在抓取: {name} → {len(arts)} 篇")
                if health_callback and journal.get("id"):
                    health_callback(journal["id"], True)
            except Exception as e:  # noqa: BLE001 - 单源失败不阻断其他源
                logger.error(f"  {name} 抓取失败: {e}")
                self.source_results.append({"source_id": journal.get("id"), "id": journal.get("id"),
                                            "success": False, "complete": False, "truncated": False,
                                            "next_cursor": None, "window_start": None, "window_end": None,
                                            "started_at": None, "raw_count": 0, "returned_count": 0,
                                            "error": str(e)})
                if health_callback and journal.get("id"):
                    health_callback(journal["id"], False, str(e))

        # Throttled API families stay serial inside their group, while the
        # groups themselves run concurrently so one slow API never blocks RSS.
        openalex_jobs = [(idx, journal) for idx, journal in enumerate(journals)
                         if journal.get("source_type", "rss") == "openalex"]
        arxiv_jobs = [(idx, journal) for idx, journal in enumerate(journals)
                      if journal.get("source_type", "rss") == "arxiv"]
        other_jobs = [(idx, journal) for idx, journal in enumerate(journals)
                      if journal.get("source_type", "rss") not in ("openalex", "arxiv")]

        def _run_group(jobs: list[tuple[int, dict]], workers: int) -> None:
            if not jobs:
                return
            if workers <= 1 or len(jobs) == 1:
                for job in jobs:
                    _one(job)
                return
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(workers, len(jobs))) as executor:
                list(executor.map(_one, jobs))

        group_threads = [
            threading.Thread(target=_run_group, args=(jobs, workers))
            for jobs, workers in ((other_jobs, concurrency), (openalex_jobs, 1)) if jobs
        ]
        if arxiv_jobs:
            # One combined arXiv query per run: few requests is what keeps arXiv happy.
            group_threads.append(threading.Thread(
                target=self._fetch_arxiv_group, args=(arxiv_jobs, results)))
        for thread in group_threads:
            thread.start()
        for thread in group_threads:
            thread.join()
        return [a for r in results for a in r]

    def _fetch_arxiv_group(self, entries: list[tuple[int, dict]], results: list[list[dict]]) -> None:
        """Fetch every arXiv subscription with ONE combined OR query.

        arXiv's ToU allow one request per three seconds, so the cheapest fix is
        to make fewer requests: all category/keyword queries are OR-ed together
        and share the same submittedDate window. Per-journal results still get
        their own source_result record so their cursors advance, but the
        articles are attributed once to the first journal to avoid N-fold
        duplication; provenance carries every merged journal id.
        """
        journals = [journal for _, journal in entries]
        queries = [str(j.get("query") or j.get("rss") or "").strip() for j in journals]
        queries = [q for q in dict.fromkeys(queries) if q]
        names = [j.get("name") or "arXiv" for j in journals]
        ids = [j.get("id") for j in journals]
        started_at = datetime.now().isoformat()
        window_start = (self._window_date - timedelta(days=max(0, self.date_filter_days - 1))).strftime("%Y%m%d")
        window_end = self._window_date.strftime("%Y%m%d")
        per_max = min(2000, max((int(j.get("max_articles", self.max_per_journal)) for j in journals), default=self.max_per_journal))

        if not queries:
            for _idx, journal in entries:
                self._record_arxiv_result(journal, started_at, window_start, window_end,
                                          success=True, complete=True)
            return
        if arxiv.is_cooling_down():
            error = "skipped: arxiv rate-limit cooldown active"
            logger.warning("  %s", error)
            for _idx, journal in entries:
                self._record_arxiv_result(journal, started_at, window_start, window_end,
                                          success=False, error=error)
            return

        combined = " OR ".join(f"({q})" for q in queries)
        query = f"({combined}) AND submittedDate:[{window_start}0000 TO {window_end}2359]"
        url = (f"https://export.arxiv.org/api/query?search_query={quote(query)}"
               f"&sortBy=submittedDate&sortOrder=descending&max_results={per_max}")
        try:
            feed = arxiv.fetch_feed(url, timeout=max(self.timeout, 30))
            raw_count = len(feed.entries)
            complete = raw_count < per_max
            articles = []
            for entry in feed.entries[:per_max]:
                art = self._parse_entry(entry, names[0], "arXiv")
                if not art:
                    continue
                art["url"] = entry.get("link", art.get("url", ""))
                art["date_source"] = "arxiv_first_submitted"
                primary = ((entry.get("arxiv_primary_category") or {}).get("term") or "").strip()
                art["journal"] = primary or art.get("journal") or names[0]
                art["_sources"] = [f"journal:{jid}" for jid in ids if jid]
                articles.append(art)
            articles = self._apply_date_filter(articles)
            if entries:
                # Attribute the merged result once; other journals keep their cursor record.
                results[entries[0][0]] = articles
            for _idx, journal in entries:
                self._record_arxiv_result(journal, started_at, window_start, window_end,
                                          success=True, raw_count=raw_count, complete=complete,
                                          returned_count=len(articles))
            logger.info("正在抓取: %s → %d 篇（合并 %d 个 arXiv 订阅）",
                        names[0], len(articles), len(entries))
        except Exception as exc:  # noqa: BLE001 - 单组失败不阻断其他来源
            logger.error("  arXiv 合并抓取失败: %s", exc)
            for _idx, journal in entries:
                self._record_arxiv_result(journal, started_at, window_start, window_end,
                                          success=False, error=str(exc))

    def _record_arxiv_result(self, journal: dict, started_at: str, window_start: str,
                             window_end: str, *, success: bool, raw_count: int = 0,
                             complete: bool = False, returned_count: int = 0,
                             error: Optional[str] = None) -> None:
        """Append one source_result so each merged journal's cursor can advance."""
        self.source_results.append({
            "source_id": journal.get("id"), "id": journal.get("id"),
            "success": success, "complete": bool(success and complete),
            "truncated": bool(success and not complete),
            "next_cursor": None,
            "window_start": window_start, "window_end": window_end,
            "started_at": started_at, "raw_count": raw_count,
            "returned_count": returned_count, "error": error})

    def _fetch_arxiv_source(self, journal: dict) -> list[dict]:
        """arXiv API 分类订阅（query 如 cat:cond-mat.mtrl-sci）。"""
        query = journal.get("query") or journal.get("rss") or ""
        per_max = int(journal.get("max_articles", self.max_per_journal))
        url = (f"https://export.arxiv.org/api/query?search_query={quote(query + chr(32) + 'AND submittedDate:[' + (self._window_date - timedelta(days=max(0, self.date_filter_days - 1))).strftime('%Y%m%d') + '0000 TO ' + self._window_date.strftime('%Y%m%d') + '2359]')}"
               f"&sortBy=submittedDate&sortOrder=descending&max_results={per_max}")
        feed = arxiv.fetch_feed(url, timeout=max(self.timeout, 30))
        if feed is None:
            raise RuntimeError("arXiv feed unavailable")
        if not feed.entries:
            self._source_status().update({"raw_count": 0, "truncated": False, "complete": True,
                                             "window_start": None, "window_end": self._window_date.isoformat()})
            return []
        raw_count = len(feed.entries)
        total = getattr(feed, "feed", {}).get("opensearch_totalresults")
        complete = int(total) <= raw_count if total is not None else raw_count < per_max
        self._source_status().update({"raw_count": raw_count, "truncated": not complete,
                                         "complete": complete,
                                         "window_end": self._window_date.isoformat()})
        articles = []
        for entry in feed.entries[:per_max]:
            art = self._parse_entry(entry, journal.get("name", "arXiv"), "arXiv")
            if art:
                art["url"] = entry.get("link", art.get("url", ""))
                art["date_source"] = "arxiv_first_submitted"
                articles.append(art)
        return self._apply_date_filter(articles)

    def _fetch_openalex_source(self, journal: dict) -> list[dict]:
        """OpenAlex 检索式订阅，按 last_run 水位线增量拉取。"""
        from integrations import arxiv, openalex as oa
        oa.set_polite_email(self.unpaywall_email)
        query = journal.get("query") or journal.get("rss") or ""
        from_date = journal.get("last_run") or (
            self._window_date - timedelta(days=max(0, self.date_filter_days - 1))
        ).isoformat()
        works, page_meta = oa.search_works_page(query, from_date=from_date,
                                to_date=self._window_date.isoformat(),
                                limit=int(journal.get("max_articles", self.max_per_journal)))
        articles = []
        self._source_status().update({"window_start": from_date, "window_end": self._window_date.isoformat(),
                                         "raw_count": page_meta.get("raw_count", len(works)),
                                         "truncated": bool(page_meta.get("truncated")),
                                         "complete": bool(page_meta.get("complete")),
                                         "next_cursor": page_meta.get("next_cursor")})
        for w in works:
            articles.append({
                "title": w["title"],
                "journal": w.get("journal") or journal.get("name", ""),
                "publisher": "DEFAULT",
                "url": w["url"],
                "doi": w["doi"],
                "abstract": w["abstract"],
                "authors": w["authors"],
                "pub_date": w.get("pub_date") or "",
                "date_source": "openalex_publication_date",
                "quarantine": not bool(w.get("pub_date")),
                "has_fulltext": False,
                "cited_count": w.get("cited_count"),
                "discovered_via": "openalex_query",
            })
        return articles

    def _fetch_crossref_source(self, journal: dict) -> list[dict]:
        from integrations.crossref import journal_works_page
        articles, total = journal_works_page(
            (journal.get("query") or "").strip(),
            limit=int(journal.get("max_articles", self.max_per_journal)),
            days=self.date_filter_days, timeout=self.timeout, today=self._window_date,
        )
        self._source_status().update({"raw_count": len(articles),
                                         "truncated": total is None or total > len(articles),
                                         "window_start": (self._window_date - timedelta(days=max(0, self.date_filter_days - 1))).isoformat(),
                                         "window_end": self._window_date.isoformat()})
        for article in articles:
            article["journal"] = journal.get("name") or article.get("journal", "")
            article["publisher"] = DOI_PUBLISHER_MAP.get(article.get("doi", "").split("/")[0], "DEFAULT")
        return articles

    def _apply_date_filter(self, articles: list[dict]) -> list[dict]:
        # Admission is decided after persistence preparation, never by dropping undated records.
        for article in articles:
            article["quarantine"] = not bool(article.get("pub_date"))
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
          0.5 预印本（arXiv：PDF 直取文本，保证全文质量）
          1. HTML 轻量请求（DOI 规范 URL）
          2. 浏览器渲染（出版商特定交互）
          3. OpenAlex 摘要补全
        """
        doi = article.get("doi", "")
        url = article.get("url", "")

        # ★ 通过 DOI 前缀识别出版商，比 RSS 字段更可靠
        publisher = _get_publisher_from_doi(doi) or article.get("publisher", "DEFAULT")

        if not url or publisher in RSS_ONLY_PUBLISHERS:
            # arXiv 文章即使无 URL 也可由 DOI 构造 PDF 链接，交给预印本层处理
            if not (_extract_arxiv_id(url) or _extract_arxiv_id(doi)):
                return FetchResult(fetch_status=FetchStatus.NO_HTML_URL, error_code="NO_HTML_URL")

        # ── 0.5 预印本全文（arXiv 专用层）────────────────────────
        # arXiv 的 abs 页面只有摘要，会被误标为全文；HTML 版（/html/）
        # 与 ar5iv 覆盖不全。PDF 恒可用，直接 PyMuPDF 提取文本。
        arxiv_id = _extract_arxiv_id(url) or _extract_arxiv_id(doi)
        if arxiv_id:
            from fetchers.pdf_fetcher import fetch_pdf_text
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
            logger.info(f"  arXiv 预印本，直接提取 PDF 文本: {pdf_url}")
            pdf_result = fetch_pdf_text(pdf_url, timeout=max(self.timeout, 40))
            if pdf_result.has_fulltext and pdf_result.text:
                pdf_result.source_url = pdf_url
                logger.info(f"    ✓ arXiv 全文 {len(pdf_result.text)} 字（PDF）")
                return pdf_result
            logger.warning(f"    ⚠ arXiv PDF 提取失败 [{pdf_result.fetch_status.value}]，转常规回退链")

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
            # 浏览器渲染也需达到全文长度下限，否则视为摘要级
            if browser_text and len(browser_text) >= MIN_FULLTEXT_LEN:
                return FetchResult(
                    text=browser_text,
                    best_available_format=BestFormat.HTML_FULLTEXT,
                    fetch_status=FetchStatus.SUCCESS,
                    network_mode=html_result.network_mode,
                    access_path=html_result.access_path,
                    source_url=canonical_url,
                )
            if browser_text:
                logger.info(f"    浏览器渲染仅得 {len(browser_text)} 字（<全文下限），按摘要处理")

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
        if feed is None:
            raise RuntimeError("RSS feed unavailable")
        if not feed.entries:
            self._source_status().update({"raw_count": 0, "truncated": False,
                                             "window_start": None, "window_end": self._window_date.isoformat()})
            return []

        self._source_status().update({"raw_count": len(feed.entries), "truncated": len(feed.entries) > per_max,
                                         "window_start": None, "window_end": self._window_date.isoformat()})
        articles = []
        for entry in feed.entries[:per_max]:
            art = self._parse_entry(entry, journal_name, publisher)
            if art:
                articles.append(art)

        # Missing publication dates remain unknown, rather than being silently
        # discarded. Use the same policy as other feeds; never invent a date.
        return self._apply_date_filter(articles)

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
            # feedparser converts published_parsed to UTC; retain timezone until admission.
            pub_date = ""
            pub_date_source = "missing"
            parsed = entry.get("published_parsed")
            if parsed:
                pub_date = datetime(*parsed[:6]).isoformat() + "Z"
                pub_date_source = "rss_published"
            return {
                "title":        title,
                "journal":      journal_name,
                "publisher":    detected_publisher,
                "url":          url,
                "doi":          doi,
                "abstract":     self._extract_rss_abstract(entry),
                "authors":      self._extract_authors(entry),
                "pub_date":     pub_date,
                "pub_date_source": pub_date_source,
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
                clean = clean_feed_abstract(clean)
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