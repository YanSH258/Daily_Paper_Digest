
"""
analyzer.py - LLM 调用模块
1. filter_relevance()  : 用 LLM 判断文章与研究方向的相关性评分
2. analyze_article()   : 对相关文章进行专业化学解读
支持 DeepSeek 和 通义千问（Qwen）两种 API
"""
import json
import time
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)


class LLMAnalyzer:
    def __init__(self, config: dict):
        self.config = config
        llm_cfg = config.get("llm", {})
        self.provider = llm_cfg.get("provider", "deepseek")
        self.temperature = llm_cfg.get("temperature", 0.3)
        self.max_tokens = llm_cfg.get("max_tokens", 8192)
        self.topics = config.get("research_topics", [])

        # 初始化 OpenAI 兼容客户端
        provider_cfg = llm_cfg.get(self.provider, {})
        self.client = OpenAI(
            api_key=provider_cfg["api_key"],
            base_url=provider_cfg["base_url"],
        )
        self.model = provider_cfg.get("model", "deepseek-chat")
        logger.info(f"LLM 已初始化: provider={self.provider}, model={self.model}")

    # ──────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────

    def filter_relevance(self, article: dict) -> float:
        """
        对单篇文章打相关性分数（0-10）
        返回 float，-1 表示调用失败
        """
        title    = article.get("title", "")
        abstract = article.get("abstract", "")

        if not abstract:
            abstract = "(摘要不可用，请仅凭标题判断)"

        topics_str = "\n".join(f"- {t}" for t in self.topics)
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
            result = self._call_llm(prompt, max_tokens=200)
            data = self._parse_json(result)
            score = float(data.get("score", 0))
            reason = data.get("reason", "")
            logger.debug(f"  相关性评分 {score}/10: {reason}")
            return score
        except Exception as e:
            logger.error(f"相关性评分失败: {e}")
            return -1

    def analyze_article(self, article: dict) -> dict:
        """
        对文章进行深度解读，返回结构化结果
        """
        title    = article.get("title", "")
        abstract = article.get("abstract", "（摘要不可用）")
        journal  = article.get("journal", "")
        authors  = ", ".join(article.get("authors", []))
        has_fulltext = article.get("has_fulltext", False)
        topics_str = "\n".join(f"- {t}" for t in self.topics)

        # 🚀 彻底防瞎编设定：根据是否抓到全文，采用完全两套隔离的提示词模板！
        if has_fulltext:
            prompt = f"""你是一位经验丰富的化学领域研究人员，擅长阅读和解读化学论文。
请对以下论文进行专业、深入的解读，帮助同行快速掌握核心内容。

【期刊】{journal}
【标题】{title}
【作者】{authors}
【论文正文】
{abstract}

【我的研究方向（供参考）】
{topics_str}

严格按照以下 7 个部分输出，不得省略。全程中文，术语保留英文原文并附解释。
- 严禁编造具体数字，重点在提取 Methodology 的真实细节。

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
{abstract}

请严格按照以下 7 个部分输出，并遵守【极严苛指令】：
⚠️ 核心警告：对于摘要中没有提及的方法细节、对比表格、实验配置等，你【必须】原封不动地输出“因未获取到全文，摘要中无此信息”这句话。**绝对禁止依靠你的领域知识去猜测或补全框架内容**，一经发现瞎编即视为严重错误！

### 0. 摘要翻译
将论文摘要原文翻译为中文，保持学术语言风格，不做删减。
---
### 1. 方法动机
仅基于摘要提取动机和背景（若没有则写“因未获取到全文，摘要中无此信息”）。
---
### 2. 方法设计
因未获取到全文，摘要中无此信息。
---
### 3. 与其他方法对比
因未获取到全文，摘要中无此信息。
---
### 4. 实验表现
仅基于摘要提取关键结果（若摘要中无具体数据，写“因未获取到全文，摘要中无此信息”）。
---
### 5. 学习与应用
因未获取到全文，摘要中无此信息。
---
### 6. 总结
**a) 一句话核心思想**（基于摘要概括，≤20字）
**b) 速记版 Pipeline**
因未获取到全文，摘要中无此信息。
"""

        # 强制最高 Token 保护（防止长文本被截断），并且确保遵守 DeepSeek 的 8192 上限
        analysis_max_tokens = min(max(self.max_tokens, 4096), 8192)

        try:
            analysis = self._call_llm(prompt, max_tokens=analysis_max_tokens)
            return {
                "success":  True,
                "analysis": analysis,
            }
        except Exception as e:
            logger.error(f"文章解读失败: {e}")
            return {
                "success":  False,
                "analysis": f"解读失败: {e}",
            }

    # ──────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────

    def _call_llm(self, prompt: str, max_tokens: int = None, retry: int = 3) -> str:
        """调用 LLM API，自带重试并处理截断"""
        max_tokens = max_tokens or self.max_tokens
        for attempt in range(retry):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                )
                
                choice = response.choices[0]
                message = choice.message.content.strip()
                finish_reason = choice.finish_reason
                
                # 如果依然被截断，在报告里插入显眼的红色警告
                if finish_reason == "length":
                    logger.warning(f"LLM 输出达到 max_tokens ({max_tokens}) 限制，内容被截断！")
                    message += f"\n\n> ⚠️ **[系统提示：AI 解读因达到输出长度上限被截断。请在 config.yaml 中进一步调大 max_tokens 的值（当前为 {max_tokens}）。]**"
                
                return message
            except Exception as e:
                logger.warning(f"LLM 调用第 {attempt+1} 次失败: {e}")
                if attempt < retry - 1:
                    time.sleep(5 * (attempt + 1))
                else:
                    raise

    def _parse_json(self, text: str) -> dict:
        """从 LLM 返回文本中提取 JSON"""
        # 去除 markdown 代码块
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试找到 { } 范围
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return {"score": 0, "reason": "解析失败"}
        