"""规则分类：不调用 LLM，基于标题/摘要/期刊/已有 topic。

Phase 0.1：提高 ai_materials / dft / mlip 召回，避免误落入 other。
优先级：mlip > ai_materials > dft > llm_science > top_chemistry > other
（mlip 最强信号，避免被 ai_materials 抢走）
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

DIGEST_CATEGORIES = (
    "mlip",
    "ai_materials",
    "dft",
    "llm_science",
    "top_chemistry",
    "other",
)

_MLIP = re.compile(
    r"(interatomic potential|machine[- ]learn(ed|ing) potential|"
    r"neural (network )?potential|atomistic foundation model|"
    r"universal (machine[- ]learning )?potential|machine learning molecular dynamics|"
    r"\bmlip\b|\bmace\b|\bdeepmd\b|\bdpa[234]?\b|\bnep\b|\bchgnet\b|\bm3gnet\b|"
    r"\bmattersim\b|\bsevennet\b|\bnequip\b|\ballegro\b|"
    r"equivariant (message[- ]passing )?potential|"
    r"message[- ]passing (neural network )?potential|"
    r"active learning.{0,40}potential|"
    r"potential.{0,30}(surrogate|distill)|"
    r"machine[- ]learned interatomic)",
    re.I,
)

# AI / 生成式材料：结构搜索、生成、逆向设计、代理模型（含 universal MLIP 作 surrogate）
_AI_MATS = re.compile(
    r"(ai for materials|materials (discovery|generation|design)|"
    r"crystal structure prediction|structure search|"
    r"crystal (generation|structure)|generative (crystal|materials|model)|"
    r"inverse materials design|inverse design|"
    r"materials foundation model|"
    r"genetic algorithm.{0,60}(structure|crystal|materials)|"
    r"(structure|crystal).{0,40}genetic algorithm|"
    r"physics[- ]informed neural network|\bpinn\b|"
    r"neural network.{0,40}(surrogate|potential|force field)|"
    r"surrogate model.{0,40}(dft|density functional|energy|structure)|"
    r"distill(ed|ation).{0,40}(model|potential|dft)|"
    r"high[- ]throughput.{0,40}(screen|calculat|ml|machine learning)|"
    r"machine learning.{0,50}(structure prediction|materials discovery|"
    r"crystal|screening|design))",
    re.I,
)

_DFT = re.compile(
    r"(density functional theory|\bkohn-sham\b|orbital-free dft|"
    r"electronic structure|exchange[- ]correlation functional|"
    r"neural density functional|machine learning dft|\bofdft\b|\bdft\b|"
    r"pseudopotential|plane[- ]wave|time[- ]dependent dft|\btddft\b|"
    r"nonadiabatic|ehrenfest|band structure|fermi surface|"
    r"meta-gga|\bgga\b|\bhybrid functional\b|"
    r"first[- ]principles|ab initio)",
    re.I,
)

_LLM_SCI = re.compile(
    r"(large language model|\bllm\b|scientific agent|ai scientist|"
    r"agentic science|autonomous chemistry|materials agent|chemistry agent|"
    r"foundation model.{0,40}(molecule|chemistry|materials))",
    re.I,
)

_TOP_JOURNAL_HINTS = (
    "nature chemistry",
    "nature materials",
    "nature catalysis",
    "nature energy",
    "nature",
    "science",
    "nat. chem",
)


def _text_blob(article: dict[str, Any]) -> str:
    parts = [
        str(article.get("title") or ""),
        str(article.get("abstract") or ""),
        str(article.get("topic") or ""),
        str(article.get("relevance_reason") or ""),
        " ".join(str(x) for x in (article.get("matched_topics") or [])),
    ]
    return " ".join(parts)


def _is_top_chemistry_journal(journal: str) -> bool:
    j = (journal or "").strip().lower()
    if not j:
        return False
    if "machine intelligence" in j:
        return False
    if "comput" in j and "nature" not in j:
        return False
    return any(h in j for h in _TOP_JOURNAL_HINTS)


# AI 主导：结构搜索/生成/PINN —— 即使提到 MLIP surrogate 也归 ai_materials
_AI_MATS_DOMINANT = re.compile(
    r"(genetic algorithm|structure search|crystal structure prediction|"
    r"crystal generation|generative (crystal|materials|model)|"
    r"inverse materials design|inverse design|"
    r"physics[- ]informed neural network|\bpinn\b|"
    r"high[- ]throughput.{0,40}(screen|calculat)|"
    r"stable structure prediction|global structure search)",
    re.I,
)


def classify_digest_category(article: dict[str, Any],
                             top_journals: Optional[Iterable[str]] = None) -> str:
    """返回 DIGEST_CATEGORIES 之一；无法判断时 other。"""
    blob = _text_blob(article)
    journal = str(article.get("journal") or "")

    # 0) 结构搜索/生成/PINN 主导 → ai_materials（覆盖 MLIP surrogate 场景）
    if _AI_MATS_DOMINANT.search(blob):
        return "ai_materials"

    # 1) MLIP：方法本身就是势函数
    if _MLIP.search(blob):
        return "mlip"

    # 2) 其他 AI 材料信号
    if _AI_MATS.search(blob):
        return "ai_materials"

    # 3) DFT / 电子结构方法学
    if _DFT.search(blob):
        return "dft"

    # 4) LLM / Agent
    if _LLM_SCI.search(blob):
        return "llm_science"

    # 5) 顶刊化学 track
    allowed = None
    if top_journals:
        allowed = {str(x).strip().lower() for x in top_journals if str(x).strip()}
    if allowed:
        if journal.strip().lower() in allowed or _is_top_chemistry_journal(journal):
            return "top_chemistry"
    elif _is_top_chemistry_journal(journal):
        return "top_chemistry"

    return "other"
