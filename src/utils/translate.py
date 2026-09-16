"""
translate.py - 免费标题翻译（无需 LLM 额度）

默认 MyMemory（免密钥、有日配额）；可切换 LibreTranslate（可自建）。
仅用于短文本（论文标题），失败时调用方应跳过而不是阻断主流程。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"User-Agent": "DailyPaperDigest/1.0"})

_CJK_RE = re.compile(r"[一-鿿]")


def looks_chinese(text: str) -> bool:
    """标题本身已是中文则无需翻译。"""
    if not text:
        return False
    return bool(_CJK_RE.search(text))


def _clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def translate_mymemory(text: str, src: str = "en", dest: str = "zh-CN",
                       email: str = "") -> str:
    """MyMemory free API（无需 key；带 email 可提高配额）。"""
    params = {"q": text[:500], "langpair": f"{src}|{dest}"}
    if email and "@" in email:
        params["de"] = email
    resp = _session.get("https://api.mymemory.translated.net/get", params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not translated or translated.upper().startswith("MYMEMORY WARNING"):
        raise RuntimeError(f"MyMemory 无有效结果: {str(data)[:200]}")
    return translated


def translate_libre(text: str, base_url: str = "https://libretranslate.com",
                    api_key: str = "", src: str = "en", dest: str = "zh") -> str:
    """LibreTranslate（可自建实例；公共实例可能限流或需 key）。"""
    payload: dict[str, Any] = {"q": text[:500], "source": src, "target": dest, "format": "text"}
    if api_key:
        payload["api_key"] = api_key
    resp = _session.post(base_url.rstrip("/") + "/translate", json=payload, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    translated = (data.get("translatedText") or "").strip()
    if not translated:
        raise RuntimeError(f"LibreTranslate 空结果: {str(data)[:200]}")
    return translated


def translate_title(title: str, provider: str = "mymemory", *,
                    libre_url: str = "", libre_key: str = "",
                    email: str = "", pause: float = 0.3) -> Optional[str]:
    """翻译单条标题；失败返回 None。provider: mymemory | libretranslate。"""
    title = _clean_title(title)
    if not title or looks_chinese(title):
        return title or None
    try:
        if provider == "libretranslate":
            out = translate_libre(title, base_url=libre_url or "https://libretranslate.com",
                                  api_key=libre_key)
        else:
            out = translate_mymemory(title, email=email)
        time.sleep(pause)  # 轻微限速，避免触发免费配额保护
        out = _clean_title(out)
        return out or None
    except Exception as e:  # noqa: BLE001
        logger.warning("标题翻译失败 (%s): %s", title[:60], e)
        return None
