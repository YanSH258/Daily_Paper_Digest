"""
notifier.py - 输出与推送模块
生成两部分内容：
  1. 📊 文献总览统计表（所有抓取文章，按期刊/方向分类）
  2. 🔬 相关文章深度解读（仅相关文章）
支持：Markdown 文件 / HTML 文件 / 邮件推送 / 飞书 Webhook
"""
import re
import smtplib
import logging
import requests
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email import encoders
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# ── 关键词 → 研究子方向映射（用于自动分类）─────────────────────
TOPIC_CLASSIFIER: List[tuple] = [
    ("机器学习势函数 / MLIP",   ["machine learning potential", "machine learning force field",
                                 "neural network potential", "mlip", "deepmd", "dp-md",
                                 "gpumd", "nequip", "mace", "allegro", "schnet",
                                 "active learning potential", "mlff"]),
    ("分子动力学 / MD",         ["molecular dynamics", "md simulation", "force field",
                                 "classical md", "langevin", "nose-hoover"]),
    ("金属有机框架 / MOF",       ["metal-organic framework", "mof", "covalent organic",
                                  "porous material", "reticular"]),
    ("DFT / 第一性原理",         ["density functional", "dft", "ab initio", "first-principles",
                                  "plane wave", "pseudopotential", "quantum espresso", "vasp"]),
    ("催化 / 反应机理",          ["catalysis", "catalyst", "reaction mechanism", "activation energy",
                                  "transition state", "electrocatalysis", "photocatalysis"]),
    ("材料性质预测",             ["property prediction", "bandgap", "formation energy",
                                  "phonon", "elastic", "thermal conductivity", "materials informatics"]),
    ("大模型 / AI for Science", ["large language model", "llm", "foundation model",
                                  "generative model", "diffusion model", "gnn", "graph neural",
                                  "transformer", "ai for science", "alphafold"]),
    ("量子化学 / 电子结构",      ["coupled cluster", "ccsd", "mp2", "hartree-fock", "hf",
                                  "configuration interaction", "quantum chemistry", "excited state",
                                  "td-dft", "tddft"]),
    ("纳米材料 / 表面",          ["nanoparticle", "surface", "adsorption", "thin film",
                                  "two-dimensional", "2d material", "graphene", "interface"]),
    ("其他",                     []),  # 兜底分类
]


def _build_kw_pattern(kw: str) -> re.Pattern:
    """
    为单个关键词构建正则模式。
    含连字符（dp-md、first-principles）或空格（2d material）的关键词，
    在连字符/空格两侧不能用 \\b（因为 \\b 只识别 \\w/\\W 边界），
    需要在整体前后加边界断言。
    策略：
      - 整个关键词首字符前加 (?<![\\w])  （或直接 \\b 当首字符是字母数字时）
      - 整个关键词尾字符后加 (?![\\w])
    这样可以正确处理含连字符和空格的情况。
    """
    escaped = re.escape(kw)  # 连字符、点等均被转义
    # 用 lookahead/lookbehind 替代 \b，更稳健地处理非纯字母边界
    pattern = r'(?<![^\W_])' + escaped + r'(?![^\W_])'
    # 更简洁的方案：在词边界用 (?<!\w) / (?!\w)
    pattern = r'(?<!\w)' + escaped + r'(?!\w)'
    return re.compile(pattern, re.IGNORECASE)


# 模块加载时预编译所有关键词的正则模式
# 结构：List[ (topic_name, List[re.Pattern]) ]
_TOPIC_PATTERNS: List[tuple] = [
    (topic_name, [_build_kw_pattern(kw) for kw in keywords])
    for topic_name, keywords in TOPIC_CLASSIFIER
]


def classify_article(article: Dict[str, Any]) -> str:
    """根据标题和摘要判断文章属于哪个子方向"""
    text = (article.get("title", "") + " " + article.get("abstract", "")).lower()
    for topic_name, patterns in _TOPIC_PATTERNS[:-1]:  # 不含"其他"
        if any(pat.search(text) for pat in patterns):
            return topic_name
    return "其他"


# 邮件必要配置字段
REQUIRED_EMAIL_KEYS: List[str] = ['smtp_server', 'smtp_port', 'username', 'password', 'recipients']


class Notifier:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.output_cfg = config.get("output", {})
        # ★ 默认输出目录改为 data/output；相对路径锚定到项目根，避免工作目录影响
        out_dir = self.output_cfg.get("output_dir", "data/output")
        out_path = Path(out_dir)
        if not out_path.is_absolute():
            out_path = Path(__file__).resolve().parent.parent / out_path
        self.output_dir = out_path
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.formats: List[str] = self.output_cfg.get("formats", ["markdown"])

    # ──────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────

    def notify(self, relevant_articles: List[Dict[str, Any]],
               all_articles: Optional[List[Dict[str, Any]]] = None,
               date_str: Optional[str] = None) -> tuple[str, Dict[str, Optional[bool]]]:
        """
        生成今日报告并推送。

        返回 (md_path, push_results)：push_results 记录各渠道成功与否，
        由调用方持久化到 daily_reports.push_results，支持后续只补发失败渠道。
        """
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        all_articles = all_articles or relevant_articles

        # R8: 预计算所有文章的分类，避免重复调用 classify_article
        article_topics: Dict[int, str] = {
            id(a): classify_article(a) for a in all_articles
        }
        # relevant 中可能有不在 all_articles 里的文章，一并覆盖
        for a in relevant_articles:
            if id(a) not in article_topics:
                article_topics[id(a)] = classify_article(a)

        # 1. 构建 Markdown 内容
        md_content = self._build_markdown(relevant_articles, all_articles, date_str, article_topics)
        md_path = self.output_dir / f"{date_str}.md"

        # 2. 保存 Markdown
        if "markdown" in self.formats:
            md_path.write_text(md_content, encoding="utf-8")
            logger.info(f"Markdown 报告已保存: {md_path}")

        # 3. 生成 HTML（无论 formats 是否包含 html，邮件发送时都需要）
        html_content = self._build_html(md_content, date_str)
        html_ok = not html_content.startswith("<h1>错误</h1>")

        if "html" in self.formats:
            html_path = self.output_dir / f"{date_str}.html"
            html_path.write_text(html_content, encoding="utf-8")
            logger.info(f"HTML 报告已保存: {html_path}")

        # 推送状态汇总
        push_results: Dict[str, Optional[bool]] = {}

        # 4. 邮件推送
        email_cfg = self.output_cfg.get("email", {})
        fail_on_error: bool = self.output_cfg.get("fail_on_push_error", False)
        if email_cfg.get("enabled", False):
            if not html_ok:
                logger.warning("HTML 生成失败，邮件将降级为纯文本发送")
                send_content = md_content
                send_as_html = False
            else:
                send_content = html_content
                send_as_html = True
            # 附加数据库 HTML 文件（如存在）
            db_html = self.output_dir / "paper_index.html"
            attachments = [db_html] if db_html.exists() else []
            try:
                self._send_email(send_content, date_str, email_cfg, is_html=send_as_html, attachments=attachments)
                push_results["email"] = True
            except Exception:
                push_results["email"] = False
                if fail_on_error:
                    raise

        # 5. 飞书推送
        feishu_cfg = self.output_cfg.get("feishu", {})
        if feishu_cfg.get("enabled", False):
            try:
                self._send_feishu(relevant_articles, all_articles, date_str, feishu_cfg, article_topics)
                push_results["feishu"] = True
            except Exception:
                push_results["feishu"] = False
                if fail_on_error:
                    raise

        # R6: 总结日志
        if push_results:
            summary_parts = []
            for channel, success in push_results.items():
                status = "✓ 成功" if success else "✗ 失败"
                summary_parts.append(f"{channel}: {status}")
            logger.info(f"推送渠道状态 — {' | '.join(summary_parts)}")

        return str(md_path), push_results

    def resend(self, date_str: str) -> tuple[str, Dict[str, Optional[bool]]]:
        """重发已有日报（不重新抓取/评分/分析），供补发失败推送使用。

        返回 (md_path, push_results)；当日无日报文件时抛 FileNotFoundError。
        """
        md_path = self.output_dir / f"{date_str}.md"
        html_path = self.output_dir / f"{date_str}.html"
        if not md_path.exists() and not html_path.exists():
            raise FileNotFoundError(f"未找到 {date_str} 的日报文件: {md_path}")

        push_results: Dict[str, Optional[bool]] = {}

        email_cfg = self.output_cfg.get("email", {})
        if email_cfg.get("enabled", False):
            if html_path.exists():
                content, is_html = html_path.read_text(encoding="utf-8"), True
            else:
                content, is_html = md_path.read_text(encoding="utf-8"), False
            db_html = self.output_dir / "paper_index.html"
            attachments = [db_html] if db_html.exists() else []
            try:
                self._send_email(content, date_str, email_cfg, is_html=is_html, attachments=attachments)
                push_results["email"] = True
            except Exception:
                logger.exception(f"重发邮件失败: {date_str}")
                push_results["email"] = False

        feishu_cfg = self.output_cfg.get("feishu", {})
        if feishu_cfg.get("enabled", False):
            try:
                text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
                header = f"📚 化学文献日报 {date_str}（补发）\n\n"
                self._send_feishu_text(header + text[:3000], feishu_cfg)
                push_results["feishu"] = True
            except Exception:
                logger.exception(f"重发飞书失败: {date_str}")
                push_results["feishu"] = False

        return str(md_path), push_results

    # ──────────────────────────────────────────────
    # HTML 构建
    # ──────────────────────────────────────────────

    def _build_html(self, md_content: str, date_str: str) -> str:
        """将 Markdown 转换为带排版的 HTML"""
        try:
            import markdown
        except ImportError:
            # R7: 功能降级用 warning，非 error
            logger.warning("未安装 markdown 库，HTML 生成已降级。请运行: pip install markdown")
            return (
                "<h1>错误</h1>"
                "<p>markdown 库未安装，无法生成 HTML 格式报告。"
                "请运行 <code>pip install markdown</code> 后重试。</p>"
            )

        html_body = markdown.markdown(
            md_content,
            extensions=['tables', 'fenced_code', 'nl2br']
        )

        html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📚 化学文献日报 {date_str}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            line-height: 1.6;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px 30px;
            color: #333;
            background-color: #fcfcfc;
        }}
        h1, h2, h3, h4 {{
            color: #2c3e50;
            margin-top: 1.5em;
            margin-bottom: 0.5em;
        }}
        h1 {{ border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
        h2 {{ border-bottom: 1px solid #eee; padding-bottom: 8px; }}
        a {{ color: #0366d6; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
            font-size: 0.95em;
            background-color: #fff;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        th, td {{ border: 1px solid #e1e4e8; padding: 12px 15px; text-align: left; }}
        th {{ background-color: #f6f8fa; font-weight: 600; }}
        tr:nth-child(even) {{ background-color: #fafafa; }}
        blockquote {{
            margin: 0 0 20px 0;
            padding: 10px 20px;
            color: #6a737d;
            border-left: 4px solid #dfe2e5;
            background-color: #f8f9fa;
        }}
        details {{
            margin: 20px 0;
            padding: 15px;
            background: #fff;
            border: 1px solid #ddd;
            border-radius: 5px;
        }}
        summary {{ font-weight: bold; cursor: pointer; outline: none; }}
        hr {{ border: 0; border-top: 1px solid #eaecef; margin: 30px 0; }}
    </style>
</head>
<body>
    {html_body}
</body>
</html>
"""
        return html_template

    # ──────────────────────────────────────────────
    # Markdown 构建
    # ──────────────────────────────────────────────

    def _build_markdown(self, relevant: List[Dict[str, Any]],
                        all_articles: List[Dict[str, Any]],
                        date_str: str,
                        article_topics: Optional[Dict[int, str]] = None) -> str:
        topics = self.config.get("research_topics", [])
        topics_str = "、".join(topics)

        lines = [
            f"# 📚 化学文献日报 {date_str}",
            "",
            f"> **研究方向**: {topics_str}  ",
            f"> **今日抓取**: {len(all_articles)} 篇  |  "
            f"**相关推送**: {len(relevant)} 篇  ",
            f"> **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "---",
            "",
        ]

        lines += self._build_summary_table(all_articles, date_str, article_topics)
        lines += self._build_deep_analysis(relevant, article_topics)

        return "\n".join(lines)

    # ── Part 1: 统计总览表 ──────────────────────────

    def _build_summary_table(self, all_articles: List[Dict[str, Any]],
                              date_str: str,
                              article_topics: Optional[Dict[int, str]] = None) -> List[str]:
        if not all_articles:
            return ["## 📊 今日文献总览\n\n今日无新文章。\n\n---\n"]

        # R8: 使用预计算的分类，fallback 到实时计算
        def get_topic(a: Dict[str, Any]) -> str:
            if article_topics is not None:
                return article_topics.get(id(a), classify_article(a))
            return classify_article(a)

        lines = [
            "## 📊 今日文献总览",
            "",
            "> 以下统计覆盖本次抓取的**所有**文章，无论是否与研究方向相关，方便追踪各领域研究前沿。",
            "",
        ]

        by_journal: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for a in all_articles:
            by_journal[a.get("journal", "Unknown")].append(a)

        lines += [
            "### 按期刊统计",
            "",
            "| 期刊 | 文章数 | 相关篇数 | 主要方向分布 |",
            "|------|--------|----------|------------|",
        ]
        for journal, arts in sorted(by_journal.items(), key=lambda x: -len(x[1])):
            relevant_count = sum(
                1 for a in arts
                if a.get("relevance", 0) >= self.config.get("relevance_threshold", 5)
            )
            topic_counter: Dict[str, int] = defaultdict(int)
            for a in arts:
                topic_counter[get_topic(a)] += 1
            top_topics = sorted(topic_counter.items(), key=lambda x: -x[1])[:3]
            topics_str = "、".join(f"{t}({n})" for t, n in top_topics)
            rel_str = f"**{relevant_count}**" if relevant_count > 0 else "0"
            lines.append(f"| {journal} | {len(arts)} | {rel_str} | {topics_str} |")

        lines.append("")

        by_topic: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for a in all_articles:
            by_topic[get_topic(a)].append(a)

        lines += [
            "### 按研究方向分类",
            "",
            "| 研究方向 | 文章数 | 相关 | 代表性文章（标题节选）|",
            "|----------|--------|------|----------------------|",
        ]
        sorted_topics = sorted(
            [(t, arts) for t, arts in by_topic.items() if t != "其他"],
            key=lambda x: -len(x[1])
        )
        if "其他" in by_topic:
            sorted_topics.append(("其他", by_topic["其他"]))

        for topic_name, arts in sorted_topics:
            rel_count = sum(
                1 for a in arts
                if a.get("relevance", 0) >= self.config.get("relevance_threshold", 5)
            )
            best = max(arts, key=lambda a: a.get("relevance", 0))
            title_short = best.get("title", "")[:45] + ("…" if len(best.get("title", "")) > 45 else "")
            rel_str = f"**{rel_count}**" if rel_count > 0 else "0"
            lines.append(f"| {topic_name} | {len(arts)} | {rel_str} | {title_short} |")

        lines.append("")

        lines += [
            "<details>",
            f"<summary>📋 展开所有 {len(all_articles)} 篇文章列表</summary>",
            "",
            "| # | 期刊 | 标题 | 方向 | 相关性 | 链接 |",
            "|---|------|------|------|--------|------|",
        ]
        sorted_all = sorted(all_articles, key=lambda a: (-a.get("relevance", 0), a.get("journal", "")))
        for i, a in enumerate(sorted_all, 1):
            title = a.get("title", "")[:50] + ("…" if len(a.get("title", "")) > 50 else "")
            journal = a.get("journal", "")
            topic = get_topic(a)
            score = a.get("relevance", 0)
            # R4: 统一使用 _score_to_stars，无需调用方判断 score > 0
            stars = self._score_to_stars(score)
            url = a.get("url", "")
            link = f"[🔗]({url})" if url else "—"
            lines.append(f"| {i} | {journal} | {title} | {topic} | {stars} | {link} |")

        lines += ["", "</details>", "", "---", ""]
        return lines

    # ── Part 2: 相关文章深度解读 ────────────────────

    def _build_deep_analysis(self, relevant: List[Dict[str, Any]],
                              article_topics: Optional[Dict[int, str]] = None) -> List[str]:
        if not relevant:
            return [
                "## 🔬 相关文章深度解读",
                "",
                "> 今日没有符合相关性阈值的文章。可尝试降低 `config.yaml` 中的 `relevance_threshold`。",
                "",
            ]

        sorted_relevant = sorted(relevant, key=lambda a: -a.get("relevance", 0))

        analyzed = [a for a in sorted_relevant if a.get("analysis") is not None]
        abstract_only = [a for a in sorted_relevant if a.get("analysis") is None]

        lines = [
            "## 🔬 相关文章深度解读",
            "",
            f"> 共 {len(sorted_relevant)} 篇相关文章：**{len(analyzed)} 篇**已完成 AI 深度解读，"
            f"**{len(abstract_only)} 篇**仅获取到摘要（已收录，不作解读）。",
            "",
            "### 目录",
            ""
        ]

        for i, a in enumerate(sorted_relevant, 1):
            score = a.get("relevance", 0)
            stars = self._score_to_stars(score)
            title = a.get("title", "无标题")[:60]
            journal = a.get("journal", "")
            tag = "" if a.get("analysis") is not None else " *(仅摘要)*"
            lines.append(f"{i}. {stars} **[{journal}]** {title}{tag}")

        lines += ["", "---", ""]

        for i, a in enumerate(sorted_relevant, 1):
            lines += self._article_block(i, a, article_topics)

        return lines

    def _article_block(self, idx: int, article: Dict[str, Any],
                       article_topics: Optional[Dict[int, str]] = None) -> List[str]:
        title    = article.get("title", "无标题")
        journal  = article.get("journal", "")
        authors  = article.get("authors", [])
        url      = article.get("url", "")
        doi      = article.get("doi", "")
        pub_date = article.get("pub_date", "")
        score    = article.get("relevance", 0)
        # R8: 使用预计算分类
        if article_topics is not None:
            topic = article_topics.get(id(article), classify_article(article))
        else:
            topic = classify_article(article)
        analysis = article.get("analysis")  # 可能为 None

        authors_str = ", ".join(authors[:5])
        if len(authors) > 5:
            authors_str += f" 等{len(authors)}人"

        stars = self._score_to_stars(score)

        lines = [
            f"### {idx}. {title}",
            "",
            "| 字段 | 内容 |",
            "|------|------|",
            f"| 期刊 | **{journal}** |",
            f"| 方向分类 | {topic} |",
            f"| 作者 | {authors_str or '—'} |",
            f"| 日期 | {pub_date} |",
            f"| 相关性 | {stars} ({score:.1f}/10) |",
        ]
        if doi:
            lines.append(f"| DOI | [{doi}](https://doi.org/{doi}) |")
        if url:
            lines.append(f"| 链接 | [阅读原文]({url}) |")

        lines += ["", "#### 🤖 AI 解读", ""]

        if analysis is not None:
            lines.append(analysis)
        else:
            abstract = article.get("abstract", "").strip()
            lines.append("> 📄 **仅获取到摘要，跳过 AI 深度解读。**")
            if abstract:
                lines += ["", "> **摘要**：", "", abstract]

        lines += ["", "---", ""]
        return lines

    @staticmethod
    def _score_to_stars(score: float) -> str:
        """将相关性评分转为星级字符串；score <= 0 时返回 '—'"""
        score = float(score)
        if score <= 0:
            return "—"
        if score >= 8:
            return "⭐⭐⭐"
        elif score >= 6:
            return "⭐⭐"
        elif score >= 4:
            return "⭐"
        return "○"

    # ──────────────────────────────────────────────
    # 邮件推送
    # ──────────────────────────────────────────────

    def _send_email(self, content: str, date_str: str,
                    cfg: Dict[str, Any], is_html: bool = False,
                    attachments: Optional[List[Path]] = None) -> None:
        # R1: 邮件配置校验
        missing = [key for key in REQUIRED_EMAIL_KEYS if not cfg.get(key)]
        if missing:
            msg = f"邮件配置缺少必要字段: {', '.join(missing)}"
            logger.error(msg)
            raise ValueError(msg)

        try:
            msg_obj = MIMEMultipart("mixed")
            msg_obj["Subject"] = f"📚 化学文献日报 {date_str}"
            msg_obj["From"]    = cfg["username"]
            msg_obj["To"]      = ", ".join(cfg["recipients"])

            mime_type = "html" if is_html else "plain"
            msg_obj.attach(MIMEText(content, mime_type, "utf-8"))

            # 附加文件
            for attach_path in (attachments or []):
                if not attach_path.exists():
                    logger.warning(f"附件不存在，已跳过: {attach_path}")
                    continue
                part = MIMEBase("application", "octet-stream")
                part.set_payload(attach_path.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=attach_path.name,
                )
                msg_obj.attach(part)
                logger.info(f"已附加文件: {attach_path.name}")

            with smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"]) as server:
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["username"], cfg["recipients"], msg_obj.as_string())
            logger.info(f"邮件推送成功 (格式: {mime_type})")
        except ValueError:
            # 配置校验异常直接向上传递，不重复记录
            raise
        except Exception as e:
            # R6: 使用 logger.exception 输出完整 traceback
            logger.exception(f"邮件推送失败: {e}")
            raise

    # ──────────────────────────────────────────────
    # 飞书 Webhook
    # ──────────────────────────────────────────────

    def _send_feishu_text(self, text: str, cfg: Dict[str, Any]) -> None:
        """发送纯文本到飞书 Webhook（供重发/摘要场景复用）。"""
        payload = {"msg_type": "text", "content": {"text": text}}
        resp = requests.post(cfg["webhook_url"], json=payload, timeout=10)
        resp.raise_for_status()

    def _send_feishu(self, relevant: List[Dict[str, Any]],
                     all_articles: List[Dict[str, Any]],
                     date_str: str,
                     cfg: Dict[str, Any],
                     article_topics: Optional[Dict[int, str]] = None) -> None:
        # R2: 飞书配置校验
        webhook_url = cfg.get("webhook_url", "").strip()
        if not webhook_url:
            msg = "飞书配置缺少必要字段: webhook_url"
            logger.error(msg)
            raise ValueError(msg)

        try:
            # R8: 使用预计算分类
            def get_topic(a: Dict[str, Any]) -> str:
                if article_topics is not None:
                    return article_topics.get(id(a), classify_article(a))
                return classify_article(a)

            by_topic: Dict[str, int] = defaultdict(int)
            for a in all_articles:
                by_topic[get_topic(a)] += 1

            topic_lines = "\n".join(
                f"  • {t}: {n}篇"
                for t, n in sorted(by_topic.items(), key=lambda x: -x[1])
                if t != "其他" and n > 0
            )

            header = (
                f"📚 化学文献日报 {date_str}\n"
                f"共抓取 {len(all_articles)} 篇 | 相关推送 {len(relevant)} 篇\n\n"
                f"📊 方向分布：\n{topic_lines}\n\n"
                f"🔬 重点文章：\n"
            )

            rel_lines = []
            for i, a in enumerate(sorted(relevant, key=lambda x: -x.get("relevance", 0))[:8], 1):
                score = a.get("relevance", 0)
                stars = Notifier._score_to_stars(score)
                rel_lines.append(
                    f"{i}. {stars} [{a.get('journal','')}]\n"
                    f"   {a.get('title','')[:60]}\n"
                    f"   🔗 {a.get('url','')}"
                )

            text = header + "\n".join(rel_lines)
            if not rel_lines:
                text += "（今日无高相关文章）"

            payload = {"msg_type": "text", "content": {"text": text}}
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("飞书推送成功")
        except ValueError:
            raise
        except Exception as e:
            # R6: 使用 logger.exception 输出完整 traceback
            logger.exception(f"飞书推送失败: {e}")
            raise