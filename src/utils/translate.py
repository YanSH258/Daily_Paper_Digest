"""
translate.py - 标题翻译

优先用已配置的 LLM（批量、一次调用）；LLM 未配置或失败时
调用方可回退免费接口：默认 MyMemory（免密钥、有日配额），
可切换 LibreTranslate（可自建）。
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


# ──────────────────────────────────────────────
# LLM 批量翻译（复用已配置的 LLM 供应商）
# ──────────────────────────────────────────────
_LLM_TIMEOUT_SECONDS = 60
_LLM_CHUNK_TITLES = 40
_NUM_PREFIX_RE = re.compile(r"^\s*(\d+)\s*[.、)）:：]\s*(.+)$")


def llm_provider_cfg(config: dict) -> dict:
    """取已配置 LLM 供应商的连接信息（与 analyzer 的配置约定一致）。

    缺 api_key 或 base_url 时返回空 dict，表示 LLM 翻译不可用。
    """
    llm = (config or {}).get("llm") or {}
    provider = str(llm.get("provider") or "deepseek").strip().lower()
    pcfg = dict(llm.get(provider) or {})
    if not pcfg.get("api_key") or not pcfg.get("base_url"):
        return {}
    pcfg.setdefault("model", "deepseek-flash")
    return pcfg


def _llm_client(pcfg: dict):
    from openai import OpenAI

    from core.analyzer import LLM_ACCEPT_ENCODING
    return OpenAI(
        api_key=pcfg["api_key"],
        base_url=pcfg["base_url"],
        max_retries=0,
        timeout=_LLM_TIMEOUT_SECONDS,
        default_headers={"Accept-Encoding": LLM_ACCEPT_ENCODING},
    )


def _llm_translate_chunk(client, model: str, titles: list[str]) -> list[str]:
    """一次调用翻译一批标题，按输入顺序返回译文；未返回的条目为空串。"""
    numbered = "\n".join(f"{i}. {t[:500]}" for i, t in enumerate(titles, 1))
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": (
                "你是学术论文标题翻译引擎，把编号标题翻译成简体中文。"
                "要求术语准确、简洁通顺，保留化学式、数字与材料符号。"
                "只输出与输入编号一致的编号译文列表，不要输出任何其他内容。"
            )},
            {"role": "user", "content": numbered},
        ],
        temperature=0.2,
    )
    text = (completion.choices[0].message.content or "") if completion.choices else ""
    out = [""] * len(titles)
    for line in text.splitlines():
        m = _NUM_PREFIX_RE.match(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= len(titles) and not out[idx - 1]:
            out[idx - 1] = _clean_title(m.group(2))
    return out


def translate_titles_llm(titles: list[str], config: dict) -> dict[str, str]:
    """用已配置的 LLM 批量翻译标题，返回 {入参标题: 中文译文}（仅含成功条目）。

    LLM 未配置或调用失败时返回空 dict（或缺失对应条目），调用方应
    回退到免费接口逐条翻译，而不是整体失败。
    """
    pcfg = llm_provider_cfg(config)
    if not pcfg:
        return {}
    items: list[tuple[str, str]] = []  # (入参原标题, 清理后标题)
    for t in titles:
        c = _clean_title(t)
        if c and not looks_chinese(c):
            items.append((t, c))
    if not items:
        return {}
    uniq = list(dict.fromkeys(c for _, c in items))
    by_clean: dict[str, str] = {}
    try:
        client = _llm_client(pcfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 翻译不可用，回退免费接口: %s", e)
        return {}
    for start in range(0, len(uniq), _LLM_CHUNK_TITLES):
        chunk = uniq[start:start + _LLM_CHUNK_TITLES]
        zh_list: list[str] = []
        for attempt in (1, 2):
            try:
                zh_list = _llm_translate_chunk(client, pcfg["model"], chunk)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    logger.warning("LLM 批量翻译失败（%d 条，重试后仍失败）: %s", len(chunk), e)
        for src, zh in zip(chunk, zh_list):
            if zh:
                by_clean[src] = zh
    return {orig: by_clean[c] for orig, c in items if by_clean.get(c)}
