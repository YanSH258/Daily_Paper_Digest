"""
core/rate_limiter.py - 请求速率限制器

实现基于令牌桶算法（Token Bucket）的速率限制：
  - RateLimiter：全局速率限制
  - DomainRateLimiter：按域名差异化速率限制
  - 线程安全，适合多线程并发场景
"""
import time
import threading
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    令牌桶速率限制器（线程安全）

    原理：以固定速率补充令牌，请求消耗令牌，令牌不足时等待。
    """

    def __init__(self, rate: float, burst: int = None):
        """
        Args:
            rate:  每秒允许的请求数（令牌补充速率）
            burst: 最大突发容量（默认 = max(1, int(rate * 2))）
        """
        self.rate = rate
        self.burst = burst if burst is not None else max(1, int(rate * 2))
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 60.0) -> bool:
        """
        获取一个令牌（阻塞直到可用或超时）。

        Args:
            timeout: 最长等待秒数

        Returns:
            True 表示成功获取令牌，False 表示超时
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                # 计算需要等待的时间
                wait_time = (1.0 - self._tokens) / self.rate

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(wait_time, remaining, 0.1))

    def _refill(self):
        """补充令牌（必须在锁内调用）"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def notify_throttled(self, status_code: int = None):
        """通知限流器遇到了限速响应，临时降低速率"""
        with self._lock:
            if status_code in (429, 503):
                # 被服务器限速，消耗额外令牌（相当于惩罚）
                self._tokens = max(0.0, self._tokens - self.burst * 0.5)
                logger.debug(f"速率限制器：收到 {status_code}，临时降速")


class DomainRateLimiter:
    """
    按域名差异化速率限制器

    对不同域名使用不同的速率限制，特别是对限制严格的出版商（如 Wiley）。
    """

    # 默认域名速率配置（每秒请求数）
    _DEFAULT_DOMAIN_RATES = {
        "onlinelibrary.wiley.com": 1.0,   # Wiley 限制严格
        "wiley.com": 1.0,
        "pubs.acs.org": 2.0,
        "acs.org": 2.0,
        "www.nature.com": 2.0,
        "nature.com": 2.0,
        "pubs.rsc.org": 2.0,
        "rsc.org": 2.0,
        "www.sciencedirect.com": 2.0,
        "sciencedirect.com": 2.0,
        "pubs.aip.org": 2.0,
        "link.springer.com": 2.0,
        "springer.com": 2.0,
        "iopscience.iop.org": 2.0,
        "feeds.aps.org": 3.0,
    }

    def __init__(self, domain_config: dict = None, default_rate: float = 5.0):
        """
        Args:
            domain_config: 自定义域名速率配置，格式 {"domain": rps}
            default_rate:  未配置域名的默认速率（每秒请求数）
        """
        self.default_rate = default_rate
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

        # 合并默认配置和自定义配置
        rates = dict(self._DEFAULT_DOMAIN_RATES)
        if domain_config:
            rates.update(domain_config)

        # 预创建已知域名的限流器
        for domain, rate in rates.items():
            self._limiters[domain] = RateLimiter(rate)

    def acquire(self, url: str, timeout: float = 60.0) -> bool:
        """
        为指定 URL 获取速率令牌。

        Args:
            url:     目标 URL
            timeout: 最长等待秒数

        Returns:
            True 表示成功，False 表示超时
        """
        domain = self._extract_domain(url)
        limiter = self._get_or_create_limiter(domain)
        return limiter.acquire(timeout=timeout)

    def notify_throttled(self, url: str, status_code: int = None):
        """通知某域名遇到了限速响应"""
        domain = self._extract_domain(url)
        limiter = self._get_or_create_limiter(domain)
        limiter.notify_throttled(status_code)

    def _get_or_create_limiter(self, domain: str) -> RateLimiter:
        """获取或创建域名的限流器"""
        if domain in self._limiters:
            return self._limiters[domain]
        with self._lock:
            if domain not in self._limiters:
                self._limiters[domain] = RateLimiter(self.default_rate)
            return self._limiters[domain]

    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取域名"""
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return url


# 全局默认限流器实例（在模块导入时初始化）
_global_domain_limiter: DomainRateLimiter = None
_global_limiter_lock = threading.Lock()


def get_domain_limiter(config: dict = None) -> DomainRateLimiter:
    """
    获取全局域名速率限制器实例（单例）。

    Args:
        config: 性能配置字典（首次调用时传入）

    Returns:
        DomainRateLimiter 实例
    """
    global _global_domain_limiter
    if _global_domain_limiter is None:
        with _global_limiter_lock:
            if _global_domain_limiter is None:
                domain_limits = {}
                default_rate = 5.0
                if config:
                    rate_cfg = config.get("performance", {}).get("rate_limit", {})
                    domain_limits = rate_cfg.get("domain_limits", {})
                    default_rate = rate_cfg.get("global_rps", 5.0)
                _global_domain_limiter = DomainRateLimiter(
                    domain_config=domain_limits,
                    default_rate=default_rate,
                )
    return _global_domain_limiter
