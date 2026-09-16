"""
similarity.py - 轻量文本相似度先验（纯 Python，无外部依赖）

用星标/已读/认可文献的摘要构建 TF-IDF 质心，对候选文章计算相似度，
作为 LLM 评分的参考先验（写入 articles.sim_prior 与评分 prompt）。
"""
import math
import re
from collections import Counter
from typing import Any, Optional

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "can", "our", "their", "which", "into",
    "such", "also", "than", "then", "these", "those", "using", "based", "between",
    "over", "under", "been", "being", "its", "it's", "his", "her", "they", "them",
    "we", "you", "your", "will", "would", "could", "should", "may", "might", "more",
    "most", "other", "others", "however", "where", "when", "what", "who", "how",
    "all", "any", "each", "both", "few", "some", "very", "via", "toward", "towards",
    "upon", "about", "across", "after", "before", "during", "through", "while",
    "here", "there", "thus", "hence", "therefore", "results", "show", "shown",
    "propose", "proposed", "method", "methods", "paper", "study", "work", "new",
    "novel", "high", "low", "large", "small", "different", "various", "several",
}

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


def _tokenize(text: str) -> list[str]:
    return [t.lower().lstrip("-") for t in _TOKEN_RE.findall(text or "")
            if t.lower() not in _STOPWORDS]


def build_profile(texts: list[str], min_docs: int = 5) -> Optional[dict[str, float]]:
    """由正样本文本集合构建 TF-IDF 质心；样本太少返回 None（不足以刻画偏好）。"""
    texts = [t for t in texts if t and len(t.strip()) > 60]
    if len(texts) < min_docs:
        return None
    doc_freq: Counter = Counter()
    tfs: list[Counter] = []
    for t in texts:
        tokens = _tokenize(t)
        if not tokens:
            continue
        counts = Counter(tokens)
        tfs.append(counts)
        doc_freq.update(counts.keys())
    n_docs = len(tfs)
    if n_docs == 0:
        return None
    centroid: dict[str, float] = {}
    for counts in tfs:
        total = sum(counts.values())
        for term, count in counts.items():
            tf = count / total
            idf = math.log((n_docs + 1) / (doc_freq[term] + 1)) + 1
            centroid[term] = centroid.get(term, 0.0) + tf * idf
    # 归一化
    norm = math.sqrt(sum(v * v for v in centroid.values()))
    if norm == 0:
        return None
    return {k: v / norm for k, v in centroid.items()}


def compute_prior(profile: Optional[dict[str, float]], text: str) -> Optional[float]:
    """计算候选文本与质心的余弦相似度，映射为 0-10 分。"""
    if not profile:
        return None
    tokens = _tokenize(text)
    if not tokens:
        return None
    counts = Counter(tokens)
    total = sum(counts.values())
    vec = {t: (c / total) for t, c in counts.items()}
    dot = sum(w * profile.get(t, 0.0) for t, w in vec.items())
    norm = math.sqrt(sum(w * w for w in vec.values()))
    if norm == 0:
        return None
    sim = dot / norm  # 0..1（质心与向量均已归一化）
    return round(min(10.0, sim * 10.0 * 2.2), 1)  # 经验缩放：让常见命中落在 3-7 区间
