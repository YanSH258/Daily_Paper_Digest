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
from typing import Any, Optional, TypedDict

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
RELEVANCE_MAX_TOKENS: int = 200
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


# ──────────────────────────────────────────────
# TypedDict 返回值定义
# ──────────────────────────────────────────────
class AnalysisResult(TypedDict):
    success: bool
    analysis: str
    evidence_level: str


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
        self.model: str = provider_cfg.get("model", "deepseek-chat")
        logger.info(
            "[LLM_INIT] LLM 已初始化: provider=%s, model=%s",
            self.provider,
            self.model,
        )

    # ──────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────

    def filter_relevance(self, article: dict) -> Optional[float]:
        """对单篇文章打相关性分数（0-10），返回 float；失败时返回 None"""
        title: str = article.get("title", "")
        abstract: str = article.get("abstract", "")
        doi: str = article.get("doi", "")

        if not abstract:
            abstract = "(摘要不可用，请仅凭标题判断)"

        topics_str: str = "\n".join(f"- {t}" for t in self.topics)
        prompt = f"""你是一位化学领域的专业研究人员。
请判断下面这篇论文与以下研究方向的相关性，给出 0-10 的整数评分：
- 10：与研究方向高度相关，必读
- 7-9：比较相关，值得关注
- 4-6：有一定关联，可选读
- 0-3：基本无关

【研究方向】
{topics_str}

【论文标题】
{title}

【论文摘要】
{abstract}

请只返回一个 JSON 对象，格式如下（不要有任何其他文字）：
{{"score": <0-10的整数>, "reason": "<一句话说明理由>"}}"""

        try:
            result = self._call_llm(prompt, max_tokens=RELEVANCE_MAX_TOKENS)
            data = self._parse_json(result)
            score = float(data.get("score", 0))
            reason: str = data.get("reason", "")
            logger.debug(
                "  相关性评分 %s/10 | title=%s | doi=%s | reason=%s",
                score,
                title,
                doi,
                reason,
            )
            return score
        except LLMResponseParseError as e:
            logger.error(
                "[LLM_PARSE_ERROR] 相关性评分解析失败 | title=%s | doi=%s | provider=%s | model=%s: %s",
                title,
                doi,
                self.provider,
                self.model,
                e,
                exc_info=True,
            )
            return None
        except LLMQuotaExhaustedError:
            logger.error(
                "[LLM_QUOTA] 相关性评分失败：配额耗尽 | title=%s | doi=%s | provider=%s | model=%s",
                title,
                doi,
                self.provider,
                self.model,
                exc_info=True,
            )
            return None
        except LLMTimeoutError:
            logger.error(
                "[LLM_TIMEOUT] 相关性评分失败：请求超时 | title=%s | doi=%s | provider=%s | model=%s",
                title,
                doi,
                self.provider,
                self.model,
                exc_info=True,
            )
            return None
        except LLMError as e:
            logger.error(
                "[LLM_ERROR] 相关性评分失败 | title=%s | doi=%s | provider=%s | model=%s: %s",
                title,
                doi,
                self.provider,
                self.model,
                e,
                exc_info=True,
            )
            return None

    def analyze_article(self, article: dict) -> AnalysisResult:
        """对文章进行深度解读，返回结构化结果"""
        title: str = article.get("title", "")
        journal: str = article.get("journal", "")
        authors: str = ", ".join(article.get("authors", []))
        doi: str = article.get("doi", "")
        has_fulltext: bool = article.get("has_fulltext", False)
        topics_str: str = "\n".join(f"- {t}" for t in self.topics)

        # ★ 智能切分全文，提取信息密度最高的部分喂给 LLM
        raw_text: str = article.get("abstract", "（摘要不可用）")
        if has_fulltext:
            content = self._smart_chunk(raw_text)
            logger.debug(
                "  全文切分: 原始 %d 字 → 输入 %d 字 | title=%s",
                len(raw_text),
                len(content),
                title,
            )
        else:
            content = raw_text

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
            prompt = f"""你是一位经验丰富的化学领域研究人员。
当前这篇论文【仅获取到摘要，未获取到全文】。

【期刊】{journal}
【标题】{title}
【作者】{authors}
【论文摘要】
{content}

请严格按照以下格式输出，并遵守【极严苛指令】：
⚠️ 对于摘要中没有提及的内容，必须原封不动输出"因未获取到全文，摘要中无此信息"，绝对禁止依靠领域知识猜测或补全。

### 0. 摘要翻译
将论文摘要原文翻译为中文，保持学术语言风格，不做删减。
---
### 1. 方法动机
仅基于摘要提取动机和背景（若没有则写"因未获取到全文，摘要中无此信息"）。
---
### 2. 方法设计
因未获取到全文，摘要中无此信息。
---
### 3. 与其他方法对比
因未获取到全文，摘要中无此信息。
---
### 4. 实验表现
仅基于摘要提取关键结果（若摘要中无具体数据，写"因未获取到全文，摘要中无此信息"）。
---
### 5. 学习与应用
因未获取到全文，摘要中无此信息。
---
### 6. 总结
**a) 一句话核心思想**（基于摘要概括，≤20字）
**b) 速记版 Pipeline**：因未获取到全文，摘要中无此信息。
"""

        analysis_max_tokens: int = min(
            max(self.max_tokens, ANALYSIS_MIN_TOKENS), ANALYSIS_MAX_TOKENS
        )

        try:
            analysis = self._call_llm(prompt, max_tokens=analysis_max_tokens)
            evidence_level = "FULLTEXT" if has_fulltext else "ABSTRACT_ONLY"
            return AnalysisResult(
                success=True,
                analysis=analysis,
                evidence_level=evidence_level,
            )
        except LLMQuotaExhaustedError as e:
            logger.error(
                "[LLM_QUOTA] 文章解读失败：配额耗尽 | title=%s | doi=%s | provider=%s | model=%s: %s",
                title,
                doi,
                self.provider,
                self.model,
                e,
                exc_info=True,
            )
            return AnalysisResult(
                success=False,
                analysis=f"解读失败（配额耗尽）: {e}",
                evidence_level="ERROR",
            )
        except LLMTimeoutError as e:
            logger.error(
                "[LLM_TIMEOUT] 文章解读失败：请求超时 | title=%s | doi=%s | provider=%s | model=%s: %s",
                title,
                doi,
                self.provider,
                self.model,
                e,
                exc_info=True,
            )
            return AnalysisResult(
                success=False,
                analysis=f"解读失败（请求超时）: {e}",
                evidence_level="ERROR",
            )
        except LLMResponseParseError as e:
            logger.error(
                "[LLM_PARSE_ERROR] 文章解读失败：响应解析错误 | title=%s | doi=%s | provider=%s | model=%s: %s",
                title,
                doi,
                self.provider,
                self.model,
                e,
                exc_info=True,
            )
            return AnalysisResult(
                success=False,
                analysis=f"解读失败（响应解析错误）: {e}",
                evidence_level="ERROR",
            )
        except LLMError as e:
            logger.error(
                "[LLM_ERROR] 文章解读失败 | title=%s | doi=%s | provider=%s | model=%s: %s",
                title,
                doi,
                self.provider,
                self.model,
                e,
                exc_info=True,
            )
            return AnalysisResult(
                success=False,
                analysis=f"解读失败: {e}",
                evidence_level="ERROR",
            )

    # ──────────────────────────────────────────────
    # 智能全文切分
    # ──────────────────────────────────────────────

    def _smart_chunk(self, text: str, max_chars: int = SMART_CHUNK_MAX_CHARS) -> str:
        """
        从全文中提取信息密度最高的段落喂给 LLM。
        策略：摘要 + 引言开头 + 方法核心 + 结论
        比直接截断前 N 字符质量高得多。
        """
        if len(text) <= max_chars:
            return text

        # 各部分分配字符预算
        budget: dict[str, int] = {
            "intro":      CHUNK_BUDGET_INTRO,
            "method":     CHUNK_BUDGET_METHOD,
            "results":    CHUNK_BUDGET_RESULTS,
            "conclusion": CHUNK_BUDGET_CONCLUSION,
        }

        # 常见章节标题的正则（不区分大小写）
        section_patterns: dict[str, str] = {
            "intro":      r'(introduction|background|motivation)',
            "method":     r'(method|approach|model|framework|computational|theory|calculation)',
            "results":    r'(result|experiment|performance|evaluation|benchmark)',
            "conclusion": r'(conclusion|summary|discussion|outlook)',
        }

        sections: dict[str, str] = {}
        for key, pattern in section_patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                start = match.start()
                chunk = text[start : start + budget[key]]
                sections[key] = chunk

        if not sections:
            # 找不到任何章节标题，退化为：前1/3 + 后1/4
            front = text[: max_chars // 2]
            tail_start = max(0, len(text) - max_chars // 4)
            tail = text[tail_start:]
            return front + "\n...[中间内容已省略]...\n" + tail

        # 按顺序拼接找到的段落
        parts: list[str] = []
        for key in ("intro", "method", "results", "conclusion"):
            if key in sections:
                parts.append(sections[key])

        result = "\n...\n".join(parts)

        # 如果拼接后还是超长，截断
        if len(result) > max_chars:
            result = result[:max_chars]

        return result

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
                message: str = choice.message.content.strip()
                finish_reason: str = choice.finish_reason

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