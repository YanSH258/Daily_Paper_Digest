"""
core/request_manager.py - 统一请求管理器

实现多层级降级策略：
  第1层: 直接 HTML 请求（最快）
  第2层: 带轮换 UA 的重试
  第3层: 使用代理（如果配置）
  第4层: 简化版请求（最小 headers）
  第5层: 仅返回空（告知调用方使用浏览器渲染）

集成：
  - User-Agent 轮换（ua_pool）
  - 速率限制（rate_limiter）
  - 指数退避重试
  - 详细统计和日志
"""
import time
import threading
import logging
import requests
from urllib.parse import urlparse

from core.ua_pool import get_academic_ua, get_random_ua
from core.rate_limiter import get_domain_limiter

logger = logging.getLogger(__name__)

# 需要重试的 HTTP 状态码
_RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}

# 完整的浏览器请求头模板（伪装成真实浏览器）
_BROWSER_HEADERS_TEMPLATE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# 简化版请求头（最小集合，避免触发某些过滤器）
_MINIMAL_HEADERS_TEMPLATE = {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# 各出版商域名的 Referer 映射
_DOMAIN_REFERERS = {
    "onlinelibrary.wiley.com": "https://onlinelibrary.wiley.com/",
    "wiley.com": "https://onlinelibrary.wiley.com/",
    "pubs.acs.org": "https://pubs.acs.org/",
    "www.nature.com": "https://www.nature.com/",
    "pubs.rsc.org": "https://www.rsc.org/",
    "www.sciencedirect.com": "https://www.sciencedirect.com/",
    "pubs.aip.org": "https://pubs.aip.org/",
    "link.springer.com": "https://link.springer.com/",
    "iopscience.iop.org": "https://iopscience.iop.org/",
}


def _get_referer(url: str) -> str:
    """根据 URL 返回适合的 Referer"""
    try:
        domain = urlparse(url).netloc.lower()
        for key, referer in _DOMAIN_REFERERS.items():
            if domain.endswith(key):
                return referer
        # 使用同域名根路径作为 Referer
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/"
    except Exception:
        return ""


def _build_headers(ua: str, url: str, minimal: bool = False) -> dict:
    """构建完整的请求头"""
    template = _MINIMAL_HEADERS_TEMPLATE if minimal else _BROWSER_HEADERS_TEMPLATE
    headers = dict(template)
    headers["User-Agent"] = ua
    referer = _get_referer(url)
    if referer and not minimal:
        headers["Referer"] = referer
    return headers


class RequestResult:
    """HTTP 请求结果"""
    __slots__ = ("response", "tier", "error", "elapsed")

    def __init__(self, response=None, tier: int = 0, error: str = "", elapsed: float = 0.0):
        self.response = response
        self.tier = tier
        self.error = error
        self.elapsed = elapsed

    @property
    def ok(self) -> bool:
        return self.response is not None and self.response.status_code < 400


class RequestManager:
    """
    统一 HTTP 请求管理器

    多层级降级流程：
      Tier 1: 直接请求（随机 UA + 完整 headers）
      Tier 2: 不同 UA 重试（避免被 UA 封锁）
      Tier 3: 带代理重试（如果已配置代理）
      Tier 4: 简化 headers 请求（最小 headers）
      Tier 5: 返回空结果（告知调用方使用浏览器渲染）
    """

    def __init__(self, config: dict = None, timeout: int = 20,
                 max_retries: int = 3, backoff_factor: float = 1.5):
        """
        Args:
            config:         全局配置字典
            timeout:        单次请求超时（秒）
            max_retries:    每层最大重试次数
            backoff_factor: 指数退避系数（等待时间 = backoff_factor ** attempt）
        """
        self.config = config or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        # 从配置读取覆盖值
        perf = self.config.get("performance", {})
        retry_cfg = perf.get("retry", {})
        self.max_retries = retry_cfg.get("max_attempts", max_retries)
        self.backoff_factor = retry_cfg.get("backoff_factor", backoff_factor)
        self.retry_codes = set(retry_cfg.get("retry_status_codes", list(_RETRY_STATUS_CODES)))
        self.timeout = self.config.get("fetcher", {}).get("request_timeout", timeout)

        self._domain_limiter = get_domain_limiter(config)
        self._stats_lock = threading.Lock()
        self._stats = {
            "total": 0, "success": 0, "fallback_tier2": 0,
            "fallback_tier3": 0, "fallback_tier4": 0, "failed": 0,
        }

    def get(self, url: str, proxies: dict = None) -> RequestResult:
        """
        执行带降级策略的 HTTP GET 请求。

        Args:
            url:     目标 URL
            proxies: 代理字典（可选，None 表示不使用代理）

        Returns:
            RequestResult 对象
        """
        with self._stats_lock:
            self._stats["total"] += 1

        # Tier 1: 直接请求 + 随机 UA
        result = self._try_request(url, tier=1, proxies=None, minimal=False)
        if result.ok:
            self._record_success()
            return result

        # Tier 2: 换一个 UA 重试
        if result.response is not None and result.response.status_code in self.retry_codes:
            logger.debug(f"  Tier1 失败 ({result.response.status_code})，尝试 Tier2（换UA）")
            self._domain_limiter.notify_throttled(url, result.response.status_code)
            result = self._try_request(url, tier=2, proxies=None, minimal=False)
            if result.ok:
                self._record_stat("fallback_tier2")
                self._record_success()
                return result

        # Tier 3: 使用代理
        if proxies:
            logger.debug(f"  Tier2 失败，尝试 Tier3（代理）")
            result = self._try_request(url, tier=3, proxies=proxies, minimal=False)
            if result.ok:
                self._record_stat("fallback_tier3")
                self._record_success()
                return result

        # Tier 4: 简化 headers
        logger.debug(f"  Tier3 失败，尝试 Tier4（简化headers）")
        result = self._try_request(url, tier=4, proxies=proxies, minimal=True)
        if result.ok:
            self._record_stat("fallback_tier4")
            self._record_success()
            return result

        # Tier 5: 全部失败
        with self._stats_lock:
            self._stats["failed"] += 1
        return RequestResult(error=result.error or "ALL_TIERS_FAILED", tier=5)

    def _try_request(self, url: str, tier: int, proxies: dict,
                     minimal: bool) -> RequestResult:
        """执行单层请求，带重试和指数退避"""
        last_result = RequestResult(error="NOT_ATTEMPTED", tier=tier)

        for attempt in range(self.max_retries):
            if attempt > 0:
                wait = self.backoff_factor ** attempt
                logger.debug(f"  第 {attempt+1} 次重试 (tier={tier})，等待 {wait:.1f}s")
                time.sleep(wait)

            # 速率限制
            self._domain_limiter.acquire(url)

            ua = get_academic_ua() if tier <= 2 else get_random_ua()
            headers = _build_headers(ua, url, minimal=minimal)
            t0 = time.monotonic()

            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                    proxies=proxies,
                )
                elapsed = time.monotonic() - t0
                result = RequestResult(response=resp, tier=tier, elapsed=elapsed)

                if resp.status_code < 400:
                    return result

                # 需要重试的状态码
                if resp.status_code in self.retry_codes:
                    self._domain_limiter.notify_throttled(url, resp.status_code)
                    last_result = result
                    continue

                # 不需要重试的错误（如 404）
                return result

            except requests.exceptions.Timeout:
                elapsed = time.monotonic() - t0
                last_result = RequestResult(
                    error="TIMEOUT", tier=tier, elapsed=elapsed
                )
            except requests.exceptions.ConnectionError as e:
                elapsed = time.monotonic() - t0
                last_result = RequestResult(
                    error=f"CONNECTION_ERROR:{e}", tier=tier, elapsed=elapsed
                )
            except Exception as e:
                elapsed = time.monotonic() - t0
                last_result = RequestResult(
                    error=f"REQUEST_ERROR:{e}", tier=tier, elapsed=elapsed
                )

        return last_result

    def _record_success(self):
        with self._stats_lock:
            self._stats["success"] += 1

    def _record_stat(self, key: str):
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def get_stats(self) -> dict:
        """返回请求统计快照"""
        with self._stats_lock:
            return dict(self._stats)

    def log_stats(self):
        """记录统计信息到日志"""
        stats = self.get_stats()
        total = stats.get("total", 0)
        if total == 0:
            return
        success_rate = stats.get("success", 0) / total * 100
        logger.info(
            f"请求统计: 总计={total}, 成功={stats['success']} ({success_rate:.1f}%), "
            f"Tier2回退={stats['fallback_tier2']}, Tier3回退={stats['fallback_tier3']}, "
            f"Tier4回退={stats['fallback_tier4']}, 失败={stats['failed']}"
        )
