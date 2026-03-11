"""
fetcher.py - RSS 抓取 + DrissionPage 全文获取模块 (精简生产环境版)
"""
import re
import ssl
import json
import time
import logging
import feedparser
import requests
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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
        "fulltext": ["div.content p", "div.article p", "section p", "div.article-body p"],
        "abstract": ["section.abstract p", "div.abstract p"]
    },
    "Elsevier": {
        "fulltext": ["div#body p", "div.Body p", "article p", "div.article-wrapper p"],
        "abstract": ["div.Abstracts p", "div.abstract p", "div.author-keyword p"]
    },
    "IOP": {
        "fulltext": ["div.article-text p", "div.wd-jnl-art-body p"],
        "abstract": ["div.article-text-abstract p"]
    },
    "RSC": {
        "fulltext": ["div.capsule__column-wrapper p", "div.article__content p"],
        "abstract": ["div.capsule__column-wrapper p", "div.abstract p"]
    }
}

RSS_ONLY_PUBLISHERS = set()

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
WILEY_RSS_HEADERS  = {**RSS_HEADERS, "Referer": "https://onlinelibrary.wiley.com/"}
NATURE_RSS_HEADERS = {**RSS_HEADERS, "Referer": "https://www.nature.com/"}

MAX_FULLTEXT_CHARS = 20000
MAX_ABSTRACT_CHARS = 3000

def _make_lenient_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    ctx.options       |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx

class BrowserDriver:
    _page = None

    @classmethod
    def get(cls):
        if cls._page is None:
            cls._page = cls._create()
        return cls._page

    @classmethod
    def _create(cls):
        from DrissionPage import ChromiumPage, ChromiumOptions
        
        co = ChromiumOptions()
        co.headless(True)
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--ignore-certificate-errors") 
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        
        page = ChromiumPage(addr_or_opts=co)
        page.timeouts.page_load = 30
        logger.info("DrissionPage Chromium 驱动已启动")
        
        return page

    @classmethod
    def inject_cookies_if_needed(cls, page, url: str):
        cookie_file = Path("cookies.json")
        if cookie_file.exists():
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                page.set.cookies(cookies)
            except Exception:
                pass

    @classmethod
    def restart(cls):
        cls.quit()
        return cls.get()

    @classmethod
    def quit(cls):
        if cls._page:
            try:
                cls._page.quit()
            except Exception:
                pass
            cls._page = None

class JournalFetcher:
    def __init__(self, config: dict):
        self.config = config
        self.fetcher_cfg = config.get("fetcher", {})
        self.timeout = self.fetcher_cfg.get("request_timeout", 20)
        self.retry = self.fetcher_cfg.get("retry_times", 3)
        self.delay = self.fetcher_cfg.get("delay_between_requests", 2)
        self.max_per_journal = self.fetcher_cfg.get("max_articles_per_journal", 30)
        self.date_filter_days = self.fetcher_cfg.get("date_filter_days", 2)
        self._page_cache: dict = {}

    def fetch_all(self) -> list:
        journals = self.config.get("journals", [])
        all_articles = []
        for journal in journals:
            name = journal.get("name", "Unknown")
            rss_url = journal.get("rss", "")
            if not rss_url: continue
            logger.info(f"正在抓取: {name}")
            try:
                articles = self._fetch_journal(journal)
                all_articles.extend(articles)
            except Exception as e:
                logger.error(f"  {name} 抓取失败: {e}")
            time.sleep(self.delay)
        return all_articles

    def fetch_fulltext(self, article: dict) -> str:
        publisher = article.get("publisher", "DEFAULT")
        url = article.get("url", "")
        if not url or publisher in RSS_ONLY_PUBLISHERS:
            return ""
        return self._fetch_page_content(url, publisher)

    def close(self):
        BrowserDriver.quit()

    def _fetch_journal(self, journal: dict) -> list:
        rss_url = journal["rss"]
        journal_name = journal["name"]
        publisher = journal.get("publisher", "DEFAULT")
        per_max = journal.get("max_articles", self.max_per_journal)

        feed = self._fetch_rss(rss_url, publisher)
        if not feed or not feed.entries: return []

        articles = []
        for entry in feed.entries[:per_max]:
            art = self._parse_entry(entry, journal_name, publisher)
            if art: articles.append(art)

        if self.date_filter_days > 0:
            cutoff = (datetime.now() - timedelta(days=self.date_filter_days)).strftime("%Y-%m-%d")
            articles = [a for a in articles if a.get("pub_date", "9999") >= cutoff]
        return articles

    def _fetch_rss(self, rss_url: str, publisher: str):
        if publisher == "RSC":
            ssl_ctx = _make_lenient_ssl_context()
            for attempt in range(self.retry):
                try:
                    req = urllib.request.Request(rss_url, headers=RSS_HEADERS)
                    with urllib.request.urlopen(req, context=ssl_ctx, timeout=self.timeout) as r:
                        feed = feedparser.parse(r.read())
                    if feed.entries: return feed
                except Exception:
                    time.sleep(2 * (attempt + 1))
            return None

        h = WILEY_RSS_HEADERS if publisher == "Wiley" else (NATURE_RSS_HEADERS if publisher == "Nature" else RSS_HEADERS)
        session = requests.Session()
        session.headers.update(h)
        for attempt in range(self.retry):
            try:
                resp = session.get(rss_url, timeout=self.timeout)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                if feed.entries: return feed
            except Exception:
                time.sleep(2 * (attempt + 1))
        return None

    def _fetch_page_content(self, url: str, publisher: str) -> str:
        if url in self._page_cache: return self._page_cache[url]
        selectors = PUBLISHER_SELECTORS.get(publisher, {})
        if not selectors: return ""

        for attempt in range(2):
            try:
                page = BrowserDriver.get()
                page.get(url)
                
                BrowserDriver.inject_cookies_if_needed(page, url)
                if attempt == 0: 
                    page.refresh()
                
                wait_time = 0
                while ("Just a moment" in page.title or "Cloudflare" in page.title) and wait_time < 15:
                    time.sleep(1)
                    wait_time += 1
                
                page.wait.load_start()
                time.sleep(1)
                
                html_content = page.html
                found_text = ""

                # 干净的异常拦截日志
                if "There was a problem providing the content you requested" in html_content:
                    logger.warning("    ⚠ 触发 Elsevier DataDome 拦截，放弃获取全文")
                elif "Just a moment" in page.title or "Cloudflare" in page.title:
                    logger.warning("    ⚠ 触发 Cloudflare 盾拦截，放弃获取全文")
                else:
                    soup = BeautifulSoup(html_content, "html.parser")
                    
                    # 尝试全文
                    for sel in selectors.get("fulltext", []):
                        paras = soup.select(sel)
                        if len(paras) >= 3:
                            text = re.sub(r'\s+', ' ', " ".join(p.get_text(strip=True) for p in paras)).strip()
                            if len(text) > 500:
                                found_text = text[:MAX_FULLTEXT_CHARS]
                                break

                    # 尝试摘要
                    if not found_text:
                        for sel in selectors.get("abstract", []):
                            paras = soup.select(sel)
                            if paras:
                                text = re.sub(r'\s+', ' ', " ".join(p.get_text(strip=True) for p in paras)).strip()
                                if len(text) > 100:
                                    found_text = text[:MAX_ABSTRACT_CHARS]
                                    break

                if found_text:
                    self._page_cache[url] = found_text
                    return found_text

                # 没抓到文本直接跳出，不再抛出长串警告和截图
                break

            except Exception as e:
                err_str = str(e)
                if "timeout" in err_str.lower() or "断开" in err_str:
                    logger.debug("浏览器加载超时，尝试重启驱动...")
                    BrowserDriver.restart()
                time.sleep(2)

        self._page_cache[url] = ""
        return ""

    def _parse_entry(self, entry, journal_name: str, publisher: str) -> Optional[dict]:
        try:
            title = re.sub(r'\s+', ' ', BeautifulSoup(entry.get("title", ""), "html.parser").get_text()).strip()
            if not title or title.lower() in ("addition/correction", "correction") or title.startswith("[ASAP]"):
                return None
            url = entry.get("link", "")
            return {
                "title": title, "journal": journal_name, "publisher": publisher,
                "url": url, "doi": self._extract_doi(entry, url), 
                "abstract": self._extract_rss_abstract(entry),
                "authors": self._extract_authors(entry), 
                "pub_date": datetime.now().strftime("%Y-%m-%d"),
                "has_fulltext": False,
            }
        except Exception: return None

    def _extract_rss_abstract(self, entry) -> str:
        for field in ("summary", "description"):
            text = entry.get(field, "")
            if text:
                clean = re.sub(r'\s+', ' ', BeautifulSoup(text, "html.parser").get_text()).strip()
                if len(clean) > 100 and not re.match(r'^(Journal of|ACS |Nano |Angewandte|Chemical Science|Nature\s)', clean):
                    return clean[:MAX_ABSTRACT_CHARS]
        return ""

    def _extract_doi(self, entry, url: str) -> str:
        for field in ("prism_doi", "dc_identifier", "id"):
            val = str(getattr(entry, field, "") or entry.get(field, ""))
            if val and "10." in val:
                m = re.search(r'10\.\d{4,}/\S+', val)
                if m: return m.group(0).rstrip(".")
        m = re.search(r'10\.\d{4,}/[^\s&?#]+', url)
        return m.group(0).rstrip(".") if m else ""

    def _extract_authors(self, entry) -> list:
        authors = []
        if hasattr(entry, "authors"):
            authors = [a.get("name", "").strip() for a in entry.authors if a.get("name", "").strip()]
        if not authors and hasattr(entry, "author"):
            authors = [a.strip() for a in entry.author.split(",") if a.strip()]
        return authors[:10]