"""
fetchers/network.py - 代理检测与网络模式判定

从环境变量读取代理配置：
  HTTP_PROXY / http_proxy
  HTTPS_PROXY / https_proxy
  NO_PROXY / no_proxy

network_mode 说明：
  "public"        - 未配置代理，走公网直连
  "campus_proxy"  - 检测到代理，推测为校园网/机构代理
  "vpn"           - 代理 URL 含 vpn 关键字
"""
import os
import logging

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_VPN_KEYWORDS = ("vpn", "tunnel", "openvpn", "wireguard")
_CAMPUS_KEYWORDS = ("ezproxy", "library", "campus", ".edu", "univ", "ac.")


def get_proxies() -> dict:
    """从环境变量读取代理配置，返回 requests 兼容的 proxies 字典"""
    def _env(*keys: str) -> str:
        return next((os.environ[k] for k in keys if os.environ.get(k)), "")

    http_proxy  = _env("HTTP_PROXY",  "http_proxy")
    https_proxy = _env("HTTPS_PROXY", "https_proxy")
    no_proxy    = _env("NO_PROXY",    "no_proxy")

    proxies: dict = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    if no_proxy:
        proxies["no_proxy"] = no_proxy
    return proxies


def get_network_info() -> dict:
    """
    判断当前网络模式并返回 proxies 字典。

    Returns:
        {
            "network_mode": "public" | "campus_proxy" | "vpn",
            "access_path":  "direct" | "proxy",
            "proxies":      dict | None,
        }
    """
    proxies = get_proxies()
    if proxies:
        proxy_url = (proxies.get("https") or proxies.get("http", "")).lower()
        if any(kw in proxy_url for kw in _VPN_KEYWORDS):
            network_mode = "vpn"
        elif any(kw in proxy_url for kw in _CAMPUS_KEYWORDS):
            network_mode = "campus_proxy"
        else:
            # 有代理但无法精确区分，保守标记为 campus_proxy
            network_mode = "campus_proxy"
        access_path = "proxy"
        logger.debug(f"代理已启用: {proxy_url!r} → network_mode={network_mode}")
    else:
        network_mode = "public"
        access_path = "direct"

    return {
        "network_mode": network_mode,
        "access_path": access_path,
        "proxies": proxies or None,
    }
