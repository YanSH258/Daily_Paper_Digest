"""
analyzer.py - LLM 调用模块
1. filter_relevance()  : 用 LLM 判断文章与研究方向的相关性评分
2. analyze_article()   : 对相关文章进行专业化学解读
支持 DeepSeek 和 通义千问（Qwen）两种 API
"""
import re
import json
import time
import logging
from typing import Any, Generator, Optional, TypedDict

import openai
from openai import OpenAI

# ──────────────────────────────────────────────
# 从共享模块导入常量
# ──────────────────────────────────────────────
from fetchers.models import MAX_FULLTEXT_CHARS

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 命名常量（消除 Magic Numbers）
# 以下为本模块专用常量；SMART_CHUNK_MAX_CHARS 来自共享模块 fetchers.models
# ──────────────────────────────────────────────
DEFAULT_TEMPERATURE: float = 0.3
DEFAULT_MAX_TOKENS: int = 8192
# 中转站模型可能先输出大段思考再给 JSON：800 会被思考耗尽导致 JSON 截断，
# 放宽到 2000 保证最终 JSON 完整输出
RELEVANCE_MAX_TOKENS: int = 2000
ANALYSIS_MIN_TOKENS: int = 4096
ANALYSIS_MAX_TOKENS: int = 8192

# 共享常量：全文最大字符数（与 fetchers.models.MAX_FULLTEXT_CHARS 保持一致）
SMART_CHUNK_MAX_CHARS: int = MAX_FULLTEXT_CHARS

# 本模块专用：全文智能切分各部分字符预算
CHUNK_BUDGET_INTRO: int = 2000
CHUNK_BUDGET_METHOD: int = 10000
CHUNK_BUDGET_RESULTS: int = 10000
CHUNK_BUDGET_CONCLUSION: int = 5000

# 本模块专用：LLM 重试策略
LLM_MAX_RETRIES: int = 3
LLM_RETRY_BASE_DELAY: float = 5.0

# 本模块专用：文献对话
CHAT_ABSTRACT_MAX_CHARS: int = 3000
CHAT_ANALYSIS_MAX_CHARS: int = 6000
CHAT_HISTORY_MESSAGES: int = 12
CHAT_MAX_TOKENS: int = 4096

# 分析提示词版本：改动提示词结构时递增，并记录进 articles.analysis_prompt_version
# v3：摘要路径改为"仅中文翻译"；全文路径维持 7 节深度解读
PROMPT_VERSION: str = "v3"
# 摘要仅翻译所需输出上限（全文深度解读仍使用 max_tokens/ANALYSIS_* 常量）
ABSTRACT_TRANSLATION_MAX_TOKENS: int = 2048


# ──────────────────────────────────────────────
# 自定义异常类
# ──────────────────────────────────────────────
class LLMError(Exception):
    """LLM 调用相关错误的基类"""


class LLMQuotaExhaustedError(LLMError):
    """API 配额耗尽或速率限制（HTTP 429）"""


class LLMTimeoutError(LLMError):
    """API 调用超时"""


class LLMResponseParseError(LLMError):
    """LLM 返回内容无法解析"""


class LLMStreamError(LLMError):
    """流式对话失败（含模型未返回内容）"""


# ──────────────────────────────────────────────
# TypedDict 返回值定义
# ──────────────────────────────────────────────
class AnalysisResult(TypedDict):
    success: bool
    analysis: str      # 成功时为解读文本；失败时为空字符串（错误放 error 字段）
    evidence_level: str
    error: str


class ScoreResult(TypedDict):
    score: float
    reason: str
    matched_topics: list
    model: str
    basis: str         # 'abstract' | 'title'


# ──────────────────────────────────────────────
# 主类
# ──────────────────────────────────────────────
class LLMAnalyzer:
    def __init__(self, config: dict) -> None:
        self.config: dict = config
        llm_cfg: dict = config.get("llm", {})
        self.provider: str = llm_cfg.get("provider", "deepseek")
        self.temperature: float = llm_cfg.get("temperature", DEFAULT_TEMPERATURE)
        self.max_tokens: int = llm_cfg.get("max_tokens", DEFAULT_MAX_TOKENS)
        self.topics: list[str] = config.get("research_topics", [])

        provider_cfg: dict = llm_cfg.get(self.provider, {})
        self.client: OpenAI = OpenAI(
            api_key=provider_cfg["api_key"],
            base_url=provider_cfg["base_url"],
        )
        self.model: str = provider_cfg.get("model", "deepseek-flash")
        # 反馈注入（推荐质量闭环）：liked/disliked 样例由流水线设置
        self.feedback_examples: dict[str, list[dict[str, str]]] = {"liked": [], "disliked": []}
        # Token 用量统计（成本观测）
        self.usage: dict[str, int] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        logger.info(
            "[LLM_INIT] LLM 已初始化: provider=%s, model=%s",
            self.provider,
            self.model,
        )

    def set_feedback_examples(self, liked: list[dict], disliked: list[dict]) -> None:
        """注入用户偏好样例（标题+方向），评分时作为 few-shot 参考。"""
        self.feedback_examples = {"liked": liked or [], "disliked": disliked or []}

    # ──────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────

    def filter_relevance(self, article: dict) -> ScoreResult:
        """对单篇文章打相关性分数。

        返回 ScoreResult（score/reason/matched_topics/model/basis）；
        任何失败（调用或校验）抛出 LLMError 子类，由调用方决定失败状态。
        """
        title: str = article.get("title", "")
        abstract: str = article.get("abstract", "")
        doi: str = article.get("doi", "")
        basis = "abstract" if abstract.strip() else "title"

        if not abstract:
            abstract = "(摘要不可用，请仅凭标题判断)"

        topics_str: str = "\n".join(f"- {t}" for t in self.topics)
        feedback_str = self._feedback_prompt_section()
        prior_str = ""
        sim_prior = article.get("sim_prior")
        if sim_prior is not None:
            prior_str = (f"\n【系统相似度参考】该文献与你收藏/精读文献的文本相似度约为 "
                         f"{float(sim_prior):.1f}/10（仅作参考，请独立判断）。\n")
        prompt = f"""你是一位化学领域的专业研究人员。
请判断下面这篇论文与以下研究方向的相关性，给出 0-10 的整数评分：
- 10：与研究方向高度相关，必读
- 7-9：比较相关，值得关注
- 4-6：有一定关联，可选读
- 0-3：基本无关

【研究方向】
{topics_str}
{feedback_str}{prior_str}
【论文标题】
{title}

【论文摘要】
{abstract}

请只返回一个 JSON 对象，格式如下（不要有任何其他文字）：
{{"score": <0-10的整数>, "reason": "<一句话中文说明推荐理由>", "matched_topics": ["命中的研究方向，可为空数组"]}}"""

        result = self._call_llm(prompt, max_tokens=RELEVANCE_MAX_TOKENS)
        data = self._parse_json(result)

        # ── 严格校验：缺失 / 非数值 / 越界 / NaN 一律视为解析失败 ──
        if "score" not in data:
            raise LLMResponseParseError("响应 JSON 缺少 score 字段")
        raw_score = data.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise LLMResponseParseError(f"score 不是数值: {raw_score!r}")
        score = float(raw_score)
        if score != score or score in (float("inf"), float("-inf")):
            raise LLMResponseParseError(f"score 非有限数值: {raw_score!r}")
        if not 0 <= score <= 10:
            raise LLMResponseParseError(f"score 越界 (0-10): {score}")

        reason = str(data.get("reason", "")).strip()[:500]
        matched = [str(t) for t in (data.get("matched_topics") or []) if str(t).strip()][:10]
        return ScoreResult(
            score=round(score, 1),
            reason=reason,
            matched_topics=matched,
            model=self.model,
            basis=basis,
        )

    def _feedback_prompt_section(self) -> str:
        """把用户历史反馈拼成 prompt 段落（推荐质量闭环）。"""
        fb = self.feedback_examples or {}
        parts = []
        if fb.get("disliked"):
            items = "\n".join(f"- [{x.get('topic') or '未知方向'}] {x.get('title', '')[:80]}"
                              for x in fb["disliked"][:3])
            parts.append(f"用户近期明确标记为【不相关】的文章（这类方向应倾向低分）：\n{items}")
        if fb.get("liked"):
            items = "\n".join(f"- [{x.get('topic') or '未知方向'}] {x.get('title', '')[:80]}"
                              for x in fb["liked"][:3])
            parts.append(f"用户近期【收藏/认可】的文章（这类方向应倾向高分）：\n{items}")
        if not parts:
            return ""
        return "\n【用户偏好参考（根据其历史反馈）】\n" + "\n".join(parts) + "\n"

    def analyze_article(self, article: dict) -> AnalysisResult:
        """对文章进行深度解读，返回结构化结果。

        失败时 success=False、analysis 为空字符串、error 带原因——
        不再把失败文本伪装成解读内容入库。
        """
        title: str = article.get("title", "")
        journal: str = article.get("journal", "")
        authors: str = ", ".join(article.get("authors", []))
        doi: str = article.get("doi", "")
        has_fulltext: bool = article.get("evidence_level") == "FULLTEXT"
        topics_str: str = "\n".join(f"- {t}" for t in self.topics)

        # ★ 摘要与全文分离：全文来自独立的 fulltext_text 字段，摘要保持原文
        if has_fulltext:
            raw_text: str = article.get("fulltext_text") or article.get("abstract", "")
            content, chunk_note = self._smart_chunk(raw_text)
            logger.debug(
                "  全文切分: 原始 %d 字 → 输入 %d 字 | title=%s",
                len(raw_text), len(content), title,
            )
        else:
            content = article.get("abstract", "") or "（摘要不可用）"
            chunk_note = ""

        if has_fulltext:
            prompt = f"""你是一位经验丰富的化学领域研究人员，擅长阅读和解读化学论文。
请对以下论文进行专业、深入的解读，帮助同行快速掌握核心内容。

【期刊】{journal}
【标题】{title}
【作者】{authors}
【论文正文（已提取关键段落）】
{content}

【我的研究方向（供参考）】
{topics_str}

严格按照以下 7 个部分输出，不得省略。全程中文，术语保留英文原文并附解释。
严禁编造具体数字，重点在提取 Methodology 的真实细节。

### 0. 摘要翻译
将论文摘要原文翻译为中文，保持学术语言风格，不做删减。
---
### 1. 方法动机
**a) 提出动机**：作者为什么要提出这个方法？驱动力和研究背景。
**b) 现有方法的痛点**：现有主流方法的具体局限性（不泛泛而谈）。
**c) 核心假设与直觉**：用 2-3 句话概括本文的核心研究假设。
---
### 2. 方法设计
> ⚠️ 核心部分，必须细致——用户可能不会再读原文。
**a) 方法流程（Pipeline）**：输入 → 每个处理步骤（含技术细节）→ 输出。每步说明：做了什么、为什么、怎么实现。
**b) 模块结构**：每个模块的功能，以及各模块如何协同。
**c) 公式与算法解释**：通俗解释每个关键公式的含义和作用。
---
### 3. 与其他方法对比
**a) 本质区别**：最根本的不同在哪里？
**b) 创新点**：核心贡献列表（编号）
**c) 适用场景**：什么情况下更有优势？
**d) 对比表格**（包含本文方法与至少2个对比方法，列出核心思路、优缺点）
---
### 4. 实验表现
**a) 实验设计**：数据集、基线、评估指标、实验设置
**b) 关键结果**：最具代表性的数据和结论（数字具体）
**c) 优势场景**：在哪些设置下优势最明显？
**d) 局限性**：泛化能力、计算开销、数据依赖、适用范围限制
---
### 5. 学习与应用
**a) 开源情况与复现建议**
**b) 实现细节**：超参数、数据预处理、训练技巧
**c) 迁移潜力**：能否迁移到其他任务/领域？
---
### 6. 总结
**a) 一句话核心思想**（≤20字）
**b) 速记版 Pipeline**（3-5步，不用论文术语，直白具体）
"""
        else:
            prompt = f"""你是专业的学术翻译。
请把下面这篇化学/材料论文的摘要原文完整翻译为中文。

【期刊】{journal}
【标题】{title}
【作者】{authors}
【论文摘要】
{content}

要求：
1. 忠实原文，逐句翻译，不增不减、不概括、不评论、不补充任何摘要以外的信息。
2. 保持学术语言风格；专业术语保留英文原文并可在括号内附中文解释。
3. 只输出译文，不要输出任何标题、前言、注释或格式标记。
"""

        analysis_max_tokens: int = (
            ABSTRACT_TRANSLATION_MAX_TOKENS if not has_fulltext
            else min(max(self.max_tokens, ANALYSIS_MIN_TOKENS), ANALYSIS_MAX_TOKENS)
        )

        if chunk_note:
            prompt = prompt.replace(
                "【论文正文（已提取关键段落）】",
                f"【论文正文（已提取关键段落）】{chunk_note}",
            )

        try:
            analysis = self._call_llm(prompt, max_tokens=analysis_max_tokens)
            analysis = self._strip_model_deliberation(analysis, is_translation=not has_fulltext)
            # 质量门槛：deepseek-flash 偶发把推理草稿/英文原文回显当输出。
            # 翻译路径要求结果确为中文叙述；不达标按失败处理，下轮自动重试，
            # 绝不让"翻译工作笔记"冒充译文入库。
            if not has_fulltext:
                def _cjk_ratio(s: str) -> float:
                    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
                    return cjk / max(len(s), 1)
                if len(analysis) < 120 or _cjk_ratio(analysis) < 0.6:
                    logger.warning(
                        "[LLM_QUALITY] 摘要翻译输出不达标（长度 %d, 中文占比 %.2f），"
                        "按失败处理待重试 | title=%s | model=%s",
                        len(analysis), _cjk_ratio(analysis), title[:50], self.model,
                    )
                    return AnalysisResult(
                        success=False,
                        analysis="",
                        evidence_level="ABSTRACT_ONLY",
                        error=f"translation_quality_failed:len={len(analysis)}",
                    )
            evidence_level = "FULLTEXT" if has_fulltext else "ABSTRACT_ONLY"
            return AnalysisResult(
                success=True,
                analysis=analysis,
                evidence_level=evidence_level,
                error="",
            )
        except LLMError as e:
            kind = type(e).__name__
            logger.error(
                "[LLM_ERROR] 文章解读失败 (%s) | title=%s | doi=%s | provider=%s | model=%s: %s",
                kind, title, doi, self.provider, self.model, e,
                exc_info=True,
            )
            return AnalysisResult(
                success=False,
                analysis="",
                evidence_level="ERROR",
                error=f"{kind}: {e}",
            )

    # ──────────────────────────────────────────────
    # 文献对话（流式）
    # ──────────────────────────────────────────────

    def chat_context_summary(self, article: dict) -> str:
        """返回对话上下文的依据说明（前端展示用）。"""
        fulltext = (article.get("fulltext_text") or "").strip()
        if fulltext:
            excerpt, _ = self._smart_chunk(fulltext)
            return f"全文节选 {len(excerpt)} 字（原文共 {len(fulltext)} 字）"
        abstract = (article.get("abstract") or "").strip()
        if abstract:
            return f"仅摘要 {min(len(abstract), CHAT_ABSTRACT_MAX_CHARS)} 字"
        return "标题与元数据（无摘要/全文）"

    def chat_with_article(
        self,
        article: dict,
        history: list[dict],
        question: str,
    ) -> Generator[str, None, None]:
        """针对单篇文献的多轮对话，流式 yield 回答文本片段。

        article: 文献记录（title/journal/authors/abstract/fulltext_text/analysis 等）
        history: 历史消息 [{role, content}, ...]
        question: 本次用户提问
        失败时抛出 LLMStreamError，由调用方决定如何反馈（不产出伪回答文本）。
        """
        fulltext = (article.get("fulltext_text") or "").strip()
        abstract = (article.get("abstract") or "").strip()
        authors = article.get("authors") or ""
        if isinstance(authors, list):
            authors = ", ".join(authors)

        doc_parts = [
            f"标题：{article.get('title', '')}",
            f"期刊：{article.get('journal', '') or '未知'}",
            f"作者：{authors or '未知'}",
            f"DOI：{article.get('doi', '') or '无'}",
        ]
        if fulltext:
            excerpt, _ = self._smart_chunk(fulltext)
            doc_parts.append(
                "\n【原文全文节选】\n" + excerpt
                + "\n（以上为原文节选，回答时请注明依据的是节选内容）"
            )
        else:
            doc_parts.append(
                "\n【摘要】\n" + (abstract[:CHAT_ABSTRACT_MAX_CHARS] if abstract else "（摘要不可用）")
            )
        analysis = (article.get("analysis") or "").strip()
        if analysis:
            doc_parts.append(
                "\n【已有的 AI 深度解读（供参考，可能有误，以原文内容为准）】\n"
                + analysis[:CHAT_ANALYSIS_MAX_CHARS]
            )
        document = "\n".join(doc_parts)

        system_prompt = (
            "你是一位专业的化学/材料领域文献阅读助手。用户正在阅读下面这篇文献，"
            "会针对它向你提问。\n\n"
            f"【文献信息】\n{document}\n\n"
            "回答要求：\n"
            "1. 优先基于给定文献信息回答；若信息不足以回答，明确说明，"
            "可适当补充领域通用知识但必须标注\"（文献未提及，为通用知识补充）\"。\n"
            "2. 使用中文回答，专业术语保留英文原文。\n"
            "3. 回答简洁、结构化，可使用 markdown 列表和小标题。\n"
            "4. 严禁编造文献中的具体数据、公式或结论。"
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for m in (history or [])[-CHAT_HISTORY_MESSAGES:]:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})

        yield from self._call_llm_stream(messages)

    def _call_llm_stream(
        self,
        messages: list[dict[str, str]],
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        """流式调用 LLM，逐段 yield 回答内容。

        失败（认证/限流/超时/其他/未返回内容）抛出 LLMStreamError，
        由调用方转换为明确的错误事件——不产出会被当作回答保存的伪文本。
        """
        effective_max_tokens: int = max_tokens if max_tokens is not None else CHAT_MAX_TOKENS
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=effective_max_tokens,
                stream=True,
            )
            got_content = False
            for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    got_content = True
                    yield content
            if not got_content:
                raise LLMStreamError("模型未返回内容，请稍后重试或检查模型配置")
        except openai.AuthenticationError as e:
            logger.error(
                "[LLM_AUTH_ERROR] 文献对话失败：认证错误 | provider=%s | model=%s: %s",
                self.provider, self.model, e,
            )
            raise LLMStreamError("API 认证失败，请检查 API key 或账户余额") from e
        except openai.RateLimitError as e:
            logger.error(
                "[LLM_QUOTA] 文献对话失败：速率限制 | provider=%s | model=%s: %s",
                self.provider, self.model, e,
            )
            raise LLMStreamError("API 速率限制/配额不足，请稍后重试") from e
        except openai.APITimeoutError as e:
            logger.error(
                "[LLM_TIMEOUT] 文献对话失败：超时 | provider=%s | model=%s: %s",
                self.provider, self.model, e,
            )
            raise LLMStreamError("API 请求超时，请重试") from e
        except LLMStreamError:
            raise
        except Exception as e:  # noqa: BLE001 - 流式调用需要兜底以保证前端总能收到错误反馈
            logger.error(
                "[LLM_ERROR] 文献对话失败 | provider=%s | model=%s: %s",
                self.provider, self.model, e,
                exc_info=True,
            )
            raise LLMStreamError(f"{type(e).__name__}: {e}") from e

    # ──────────────────────────────────────────────
    # 模型输出清洗（deepseek-flash 偶尔把推理草稿混进正式输出）
    # ──────────────────────────────────────────────

    _DELIB_PREFIX_RE = re.compile(
        r"^(?:我们需要|我们要|用户要求|用户说|需要翻译|需要判断|需要回答|"
        r"好的[，,]?|首先[，,]?|让我来?)[^\n]{0,80}"
    )
    _DELIB_HINTS = (
        "我们需要回答用户", "我们需要提供", "我们需要仔细翻译", "我们要仔细翻译",
        "需要翻译。", "用户说“", "用户说\"", "应该只输出译文", "不要输出任何标题",
        "术语对照", "术语表", "翻译草稿", "思考过程", "让我们逐步",
    )

    @classmethod
    def _strip_model_deliberation(cls, text: str, *, is_translation: bool) -> str:
        """剥掉模型混入正式输出的推理草稿，返回干净的正文。

        deepseek-flash 对新提示词偶发输出"我们需要回答用户……"式的思考过程。
        翻译场景：推理在译文之前（先分析术语再给译文），取最后一个可辨识的
        译文起始段；全文解读场景：推理混在开头，仅剥除推理前缀行。
        无法可靠清洗时原样返回（不丢内容）。
        """
        if not text:
            return text
        stripped = text.strip()

        def _clean_prefix(s: str) -> str:
            # 逐行剥掉推理特征的行/前缀
            lines = s.splitlines()
            out = []
            for line in lines:
                probe = line.strip()
                if any(h in probe[:30] for h in cls._DELIB_HINTS):
                    continue
                line = cls._DELIB_PREFIX_RE.sub("", line) or line
                out.append(line)
            return "\n".join(out).strip()

        if is_translation:
            # 输出形态差异很大：有的是"整段译文"，有的是"逐句中文+英文原句对照"，
            # 推理草稿交织其间。统一策略：行级过滤——
            #   1) 剥推理前缀行；2) 丢弃元讨论/术语表/纯英文引用行；
            #   3) 收集剩余中文叙述行并按原顺序拼接。
            # 行级比段落级稳健：译文句是长中文行，推理是短元讨论行或英文引用行。
            cleaned = _clean_prefix(stripped)

            def cjk_ratio(s: str) -> float:
                cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
                return cjk / max(len(s), 1)

            deliberation = re.compile(
                r"(我们需要|我们必须|我们要|用户(要求|说|写)|需要翻译|需要逐句|需要提供|"
                r"需要准确|需要谨慎|应该只输出|注意术语|术语[对表]|翻译本身|要确保|"
                r"可译为|可写|可保留|可能意味着|加括号|开始翻译|逐句对应|可以[一-以]?保持|"
                r"不添加标题|Need check|Maybe better|Possible translation|Let's craft|"
                r"摘要原文有|通常中文|题目有|标题中|这满足|这里看上下文|照抄|有歧义|"
                r"不要输出任何标题|需要决定|需要处理|需要只输出|可能只需要|不应该?输出|"
                r"是否保留|最好是?|可以这样|要求[“\"不应]|要求只|要求[不忠]|注意标题|"
                r"保留英文原文|保持学术|术语如|主要专业术语|忠实逐句|只翻译|格式标记)")
            term_table = re.compile(
                r"^\s*[\"“'\-•*\d]*\s*(?:[A-Za-z][^→\n]*→|-\s+[\"“']?[A-Za-z]|[A-Za-z][^（()]{0,40}（[^）]*）\s*[=＝]?)"
            )
            quote_en = re.compile(r'^[“"][A-Za-z]')
            original_en = re.compile(r"^原文[:：]?\s*(?:Abstract|ABSTRACT)?", re.I)
            keep: list[str] = []
            for raw_line in cleaned.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("> ⚠️"):
                    continue
                orig_m = original_en.match(line)
                if orig_m and cjk_ratio(line[orig_m.end():]) < 0.3:
                    continue
                if term_table.match(line) or quote_en.match(line):
                    continue
                if deliberation.search(line):
                    continue
                if len(line) < 12 or cjk_ratio(line) < 0.4:
                    continue
                keep.append(line)
            if keep:
                return "".join(keep) if all(len(k) < 200 for k in keep) else "\n".join(keep)
            return cleaned
        # 非翻译（深度解读）：只做保守前缀清理
        return _clean_prefix(stripped)

    # ──────────────────────────────────────────────
    # 智能全文切分
    # ──────────────────────────────────────────────

    def _smart_chunk(self, text: str, max_chars: Optional[int] = None) -> tuple[str, str]:
        """
        从全文中按章节选取信息密度最高的段落喂给 LLM，总长不超过 max_chars。
        抓取层保存的正文可达 MAX_STORED_FULLTEXT_CHARS，因此必须先选区再输入。

        返回 (节选文本, 覆盖范围说明)；说明会注入提示词，
        让模型明确知道输入是节选以及覆盖了哪些章节。
        """
        if not max_chars:
            max_chars = SMART_CHUNK_MAX_CHARS
        if len(text) <= max_chars:
            return text, ""

        # 各章节按总预算比例分配：方法与结果优先，引言/结论保底
        proportions: dict[str, float] = {
            "intro": 0.10, "method": 0.40, "results": 0.35, "conclusion": 0.15,
        }
        section_patterns: dict[str, str] = {
            "intro":      r'(introduction|background|motivation)',
            "method":     r'(method|approach|model|framework|computational|theory|calculation)',
            "results":    r'(result|experiment|performance|evaluation|benchmark)',
            "conclusion": r'(conclusion|summary|discussion|outlook)',
        }

        # 按顺序定位各章节区间；起始位置向后找，天然去重叠
        ranges: list[tuple[str, int, int]] = []
        pos = 0
        for key in ("intro", "method", "results", "conclusion"):
            if pos >= len(text):
                break
            budget = int(max_chars * proportions[key])
            match = re.search(section_patterns[key], text[pos:], re.IGNORECASE)
            if not match:
                continue
            start = max(pos, pos + match.start())
            end = min(start + budget, len(text))
            if end - start < 200:  # 太短的区间没有信息价值
                continue
            ranges.append((key, start, end))
            pos = end

        if not ranges:
            front = text[: max_chars // 2]
            tail = text[len(text) - max_chars // 4:]
            note = "（未能识别章节标题，节选自全文开头与结尾）"
            return front + "\n...[中间内容已省略]...\n" + tail, note

        parts: list[str] = []
        covered: list[str] = []
        used = 0
        for key, start, end in ranges:
            parts.append(text[start:end])
            covered.append(f"{key}≈{end - start}字")
            used += end - start
        note = (
            f"【系统注：以下为全文节选，共约 {used} 字（全文 {len(text)} 字），"
            f"覆盖：{', '.join(covered)}；未覆盖部分不在输入中，请勿引用】"
        )
        return "\n...\n".join(parts), note

    # ──────────────────────────────────────────────
    # 研究工具：多论文对比 / Related Work 草稿 / 方向建议
    # ──────────────────────────────────────────────

    def _articles_context(self, articles: list[dict], max_chars_each: int = 8000) -> str:
        parts = []
        for i, a in enumerate(articles, 1):
            body = (a.get("fulltext_text") or "").strip() or (a.get("abstract") or "").strip()
            parts.append(
                f"[{i}] {a.get('title', '')}\n"
                f"DOI: {a.get('doi', '') or '无'} | 期刊: {a.get('journal', '')} | "
                f"日期: {a.get('pub_date', '')}\n{body[:max_chars_each]}"
            )
        return "\n\n".join(parts)

    def compare_articles(self, articles: list[dict]) -> str:
        """生成结构化对比（markdown 表），每个维度标注来源编号。"""
        ctx = self._articles_context(articles)
        n = len(articles)
        prompt = f"""你是化学/材料领域的资深研究人员。请对比下面 {n} 篇论文。

{ctx}

严格按以下 markdown 输出（不要额外开场白）：

## 对比概览
一句话说明这批论文的关系（同题竞争/互补/方法演进）。

## 对比表
| 维度 | {" | ".join(f"[{i+1}] {articles[i].get('title','')[:24]}" for i in range(n))} |
|------|{'------|' * n}（研究对象 / 核心方法 / 数据与体系 / 主要结果 / 局限）

每个单元格末尾必须标注来源编号，如 "（[2]）"。

## 关键差异
3-5 条要点，每条注明依据哪几篇。
## 对我的启发
结合研究方向给出 2-3 条可执行的启发。"""
        return self._call_llm(prompt, max_tokens=ANALYSIS_MAX_TOKENS)

    def related_work_draft(self, articles: list[dict], focus: str = "") -> str:
        """生成带编号引用的 Related Work 草稿段落。"""
        ctx = self._articles_context(articles)
        focus_str = f"\n写作侧重：{focus}\n" if focus else ""
        prompt = f"""你是学术论文写作助手。基于以下 {len(articles)} 篇论文，撰写一段可用于论文
Related Work 部分的学术草稿（中文，300-500 字）。{focus_str}
要求：
1. 按逻辑脉络组织（不要逐篇罗列）；
2. 每处引用用 [n] 标注，n 对应下面文献的编号，只准引用给出的文献；
3. 语气客观、术语保留英文；
4. 输出末尾附"参考文献"列表：[n] 标题. 期刊, 年份. https://doi.org/DOI

{ctx}"""
        return self._call_llm(prompt, max_tokens=ANALYSIS_MAX_TOKENS)

    def suggest_topics(self, recent_liked: list[dict], current_topics: list[str]) -> dict[str, list[str]]:
        """根据近期高分/收藏文献建议研究方向增删。返回 {"add": [...], "keep_or_remove": [...]}。"""
        ctx = self._articles_context(recent_liked[:20], max_chars_each=1500)
        topics_str = "、".join(current_topics) or "（无）"
        prompt = f"""你是科研方向规划助手。

【用户当前研究方向】
{topics_str}

【用户近期高评分/收藏的文献（标题+摘要节选）】
{ctx}

请分析这些文献反映出的兴趣演化，返回一个 JSON（不要其他文字）：
{{"add": ["建议新增的研究方向关键词（英文短语，2-4 个）"],
  "remove": ["现有方向中已明显冷清、建议移除的（可空数组）"],
  "reason": "<50字以内的说明>"}}"""
        result = self._call_llm(prompt, max_tokens=800)
        try:
            data = self._parse_json(result)
            return {
                "add": [str(x) for x in (data.get("add") or [])][:5],
                "remove": [str(x) for x in (data.get("remove") or [])][:5],
                "reason": str(data.get("reason", "")),
            }
        except LLMResponseParseError:
            return {"add": [], "remove": [], "reason": result[:200]}

    # ──────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────

    def _call_llm(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        retry: int = LLM_MAX_RETRIES,
    ) -> str:
        """调用 LLM API，自带重试并处理截断。
        按异常类型区分：配额耗尽、超时、认证错误、其他 API 错误。
        """
        effective_max_tokens: int = max_tokens if max_tokens is not None else self.max_tokens

        for attempt in range(retry):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature,
                    max_tokens=effective_max_tokens,
                )

                choice = response.choices[0]
                # Token 用量统计（成本观测）
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.usage["calls"] += 1
                    self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
                    self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
                raw_content = getattr(choice.message, "content", "") or ""
                # 如果 content 为空且存在 reasoning_content，尝试使用它
                if not raw_content and hasattr(choice.message, "reasoning_content"):
                    raw_content = getattr(choice.message, "reasoning_content", "") or ""
                message: str = raw_content.strip()
                finish_reason: str = getattr(choice, "finish_reason", "")

                if finish_reason == "length":
                    logger.warning(
                        "LLM 输出达到 max_tokens (%d) 限制，内容被截断！| provider=%s | model=%s",
                        effective_max_tokens,
                        self.provider,
                        self.model,
                    )
                    message += (
                        f"\n\n> ⚠️ **[系统提示：AI 解读因达到输出长度上限被截断。"
                        f"请在 config.yaml 中调大 max_tokens（当前为 {effective_max_tokens}）。]**"
                    )

                return message

            except openai.AuthenticationError as e:
                # 401/402：API key 无效或余额不足，不重试
                logger.error(
                    "[LLM_AUTH_ERROR] API 认证失败，请检查 API key 配置 | provider=%s | model=%s | status=%s: %s",
                    self.provider,
                    self.model,
                    getattr(e, "status_code", "N/A"),
                    e,
                    exc_info=True,
                )
                raise LLMQuotaExhaustedError(
                    f"API 认证失败（请检查 API key 或账户余额）: {e}"
                ) from e

            except openai.RateLimitError as e:
                # HTTP 429：速率限制 / 配额耗尽
                status_code = getattr(e, "status_code", 429)
                delay = LLM_RETRY_BASE_DELAY * (2**attempt)
                if attempt < retry - 1:
                    logger.warning(
                        "[LLM_QUOTA] 速率限制（HTTP %s），第 %d/%d 次重试，等待 %.1f 秒 | provider=%s | model=%s: %s",
                        status_code,
                        attempt + 1,
                        retry,
                        delay,
                        self.provider,
                        self.model,
                        e,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "[LLM_QUOTA] 速率限制重试耗尽（HTTP %s） | provider=%s | model=%s: %s",
                        status_code,
                        self.provider,
                        self.model,
                        e,
                        exc_info=True,
                    )
                    raise LLMQuotaExhaustedError(
                        f"API 速率限制，重试 {retry} 次后仍失败: {e}"
                    ) from e

            except openai.APITimeoutError as e:
                delay = LLM_RETRY_BASE_DELAY * (attempt + 1)
                if attempt < retry - 1:
                    logger.warning(
                        "[LLM_TIMEOUT] 请求超时，第 %d/%d 次重试，等待 %.1f 秒 | provider=%s | model=%s: %s",
                        attempt + 1,
                        retry,
                        delay,
                        self.provider,
                        self.model,
                        e,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "[LLM_TIMEOUT] 请求超时，重试耗尽 | provider=%s | model=%s: %s",
                        self.provider,
                        self.model,
                        e,
                        exc_info=True,
                    )
                    raise LLMTimeoutError(
                        f"API 请求超时，重试 {retry} 次后仍失败: {e}"
                    ) from e

            except openai.APIStatusError as e:
                status_code = getattr(e, "status_code", "N/A")
                # 4xx 客户端错误（无效模型名、非法请求等）是确定性的，重试必然同样失败
                if isinstance(status_code, int) and 400 <= status_code < 500:
                    logger.error(
                        "[LLM_API_ERROR] 客户端错误（HTTP %s），不重试 | provider=%s | model=%s: %s",
                        status_code,
                        self.provider,
                        self.model,
                        e,
                    )
                    raise LLMError(
                        f"API 客户端错误（HTTP {status_code}），不重试（请检查模型名/请求参数）: {e}"
                    ) from e
                delay = LLM_RETRY_BASE_DELAY * (attempt + 1)
                if attempt < retry - 1:
                    logger.warning(
                        "[LLM_API_ERROR] API 错误（HTTP %s），第 %d/%d 次重试，等待 %.1f 秒 | provider=%s | model=%s: %s",
                        status_code,
                        attempt + 1,
                        retry,
                        delay,
                        self.provider,
                        self.model,
                        e,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "[LLM_API_ERROR] API 错误（HTTP %s），重试耗尽 | provider=%s | model=%s: %s",
                        status_code,
                        self.provider,
                        self.model,
                        e,
                        exc_info=True,
                    )
                    raise LLMError(
                        f"API 错误（HTTP {status_code}），重试 {retry} 次后仍失败: {e}"
                    ) from e

            except UnicodeEncodeError as e:
                # 请求头/URL 含非 ASCII 字符（如占位符 API Key）在构造请求时必然失败，重试无意义
                logger.error(
                    "[LLM_INPUT_ERROR] 请求参数含非 ASCII 字符，不重试 | provider=%s | model=%s: %s",
                    self.provider,
                    self.model,
                    e,
                )
                raise LLMError(
                    "请求参数含非 ASCII 字符（请检查 API Key / Base URL 是否为未替换的中文占位符）"
                ) from e

            except Exception as e:
                delay = LLM_RETRY_BASE_DELAY * (attempt + 1)
                if attempt < retry - 1:
                    logger.warning(
                        "LLM 调用第 %d/%d 次失败，等待 %.1f 秒 | provider=%s | model=%s: %s",
                        attempt + 1,
                        retry,
                        delay,
                        self.provider,
                        self.model,
                        e,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "[LLM_ERROR] LLM 调用失败，重试耗尽 | provider=%s | model=%s: %s",
                        self.provider,
                        self.model,
                        e,
                        exc_info=True,
                    )
                    raise LLMError(f"LLM 调用失败，重试 {retry} 次后仍失败: {e}") from e

        # 不应到达此处，但为了类型完整性
        raise LLMError("LLM 调用失败：超出重试次数")

    def _parse_json(self, text: str) -> dict[str, Any]:
        """从 LLM 返回文本中提取 JSON。
        解析失败时记录 error 并抛出 LLMResponseParseError。
        """
        original_text = text
        text = text.strip()

        # 去除 Markdown 代码块包裹（```json ... ``` 或 ``` ... ```）
        md_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if md_match:
            text = md_match.group(1).strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 正则提取第一个 {...} 块
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

        # 解析失败，记录错误并抛出
        logger.error(
            "[LLM_PARSE_ERROR] 无法从 LLM 返回文本中提取 JSON | 原始文本前500字: %s",
            original_text[:500],
        )
        raise LLMResponseParseError(
            f"JSON 解析失败，原始文本片段: {original_text[:200]}"
        )