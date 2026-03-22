# src/utils/reanalyze.py
"""
手动上传 PDF/HTML 补充 AI 解读

用法：
  # 自动扫描 data/uploads/ 目录，匹配数据库里的文章
  python src/utils/reanalyze.py

  # 指定单个文件和 DOI
  python src/utils/reanalyze.py --file data/uploads/paper.pdf --doi 10.1063/5.0315390

  # 只处理摘要文章（不需要上传文件，用摘要重新解读）
  python src/utils/reanalyze.py --abstract-only --limit 10
"""
import sqlite3
import sys
import argparse
import logging
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from core.analyzer import LLMAnalyzer
from core.db import Database
from fetchers.models import MAX_FULLTEXT_CHARS
from utils.stat_db import build_html_index

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reanalyze")

SUPPORTED = {".pdf", ".html", ".htm", ".txt"}


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def doi_to_filename(doi: str) -> str:
    """DOI 转文件名：所有 / 换成 @"""
    return doi.replace('/', '@')


def filename_to_doi(stem: str) -> str:
    """文件名还原 DOI：所有 @ 换回 /"""
    return stem.replace('@', '/')


def extract_text_from_file(file_path: Path) -> str:
    """从 PDF/HTML/TXT 提取文本，最多 MAX_FULLTEXT_CHARS 字符"""
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc  = fitz.open(str(file_path))
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            if len(text.strip()) < 200:
                logger.warning("  PDF 文本层内容过少，可能是扫描版")
            return text[:MAX_FULLTEXT_CHARS]
        except ImportError:
            logger.error("请安装 PyMuPDF: pip install PyMuPDF")
            return ""
        except Exception as e:
            logger.error("PDF 提取失败: %s", e)
            return ""

    elif ext in (".html", ".htm"):
        try:
            from bs4 import BeautifulSoup
            html = file_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html, "lxml")
            for tag in ["script", "style", "nav", "header", "footer", "figure"]:
                for el in soup.find_all(tag):
                    el.decompose()
            text = re.sub(r'\s+', ' ', soup.get_text(" ", strip=True))
            return text[:MAX_FULLTEXT_CHARS]
        except Exception as e:
            logger.error("HTML 提取失败: %s", e)
            return ""

    elif ext == ".txt":
        return file_path.read_text(encoding="utf-8", errors="replace")[:MAX_FULLTEXT_CHARS]

    return ""


def process_one(
    conn: sqlite3.Connection,
    article: dict,
    text: str,
    analyzer: LLMAnalyzer,
    source: str = "file",
) -> bool:
    """对单篇文章用提供的文本重新解读并更新数据库"""
    article_copy = dict(article)
    article_copy["abstract"]     = text
    article_copy["has_fulltext"] = True

    try:
        result   = analyzer.analyze_article(article_copy)
        analysis = result.get("analysis")
        if analysis:
            conn.execute(
                "UPDATE articles SET analysis = ?, abstract = ? WHERE id = ?",
                (analysis, text[:MAX_FULLTEXT_CHARS], article["id"])
            )
            conn.commit()
            logger.info("  ✓ 解读完成 (%d 字) [%s]", len(analysis), source)
            return True
        else:
            logger.warning("  ✗ 解读返回空")
            return False
    except Exception as e:
        logger.error("  ✗ 解读失败: %s", e)
        return False


def scan_uploads(
    conn: sqlite3.Connection,
    analyzer: LLMAnalyzer,
    upload_dir: Path,
) -> None:
    """扫描 upload_dir 目录，自动匹配数据库文章"""
    upload_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in upload_dir.iterdir() if f.suffix.lower() in SUPPORTED]

    if not files:
        print(f"{upload_dir}/ 目录为空，请把 PDF/HTML 文件放进去")
        print("文件命名规则：把 DOI 的 / 换成 @")
        print("例如：10.1063/5.0315390 → 10.1063@5.0315390.pdf")
        return

    print(f"发现 {len(files)} 个文件")
    success = 0

    for idx, f in enumerate(files, 1):
        doi = filename_to_doi(f.stem)
        logger.info("[%d/%d] 处理: %s → DOI: %s", idx, len(files), f.name, doi)

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM articles WHERE doi = ?", (doi,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM articles WHERE LOWER(doi) = LOWER(?)", (doi,)
            ).fetchone()

        if not row:
            logger.warning("  数据库中找不到 DOI=%s，跳过", doi)
            continue

        article = dict(row)
        text    = extract_text_from_file(f)

        if not text or len(text.strip()) < 100:
            logger.warning("  文件内容过短，跳过")
            continue

        logger.info("  提取文本 %d 字，开始解读...", len(text))
        if process_one(conn, article, text, analyzer, source=f.suffix):
            success += 1

    print(f"\n完成：{success}/{len(files)} 篇解读成功")


def main() -> None:
    parser = argparse.ArgumentParser(description="手动上传文件补充 AI 解读")
    parser.add_argument("--config",        default="config/config.yaml")
    parser.add_argument("--file",          default="", help="指定单个文件路径")
    parser.add_argument("--doi",           default="", help="指定 DOI（配合 --file 使用）")
    parser.add_argument("--abstract-only", action="store_true",
                        help="不上传文件，对摘要文章重新用摘要解读")
    parser.add_argument("--limit",         type=int, default=0)
    args = parser.parse_args()

    config    = load_config(args.config)
    db_path   = config["database"]["path"]
    threshold = config.get("relevance_threshold", 5)
    # upload_dir 优先读 config，fallback "data/uploads"
    upload_dir = Path(config.get("fetcher", {}).get("upload_dir", "data/uploads"))

    db       = Database(db_path)
    conn     = db.get_connection()
    analyzer = LLMAnalyzer(config)

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"文件不存在: {args.file}")
            return
        if not args.doi:
            print("请用 --doi 指定对应的 DOI")
            return

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM articles WHERE doi = ?", (args.doi,)
        ).fetchone()
        if not row:
            print(f"数据库中找不到 DOI={args.doi}")
            return

        text = extract_text_from_file(file_path)
        if not text:
            print("文件内容提取失败")
            return

        logger.info("提取文本 %d 字，开始解读...", len(text))
        process_one(conn, dict(row), text, analyzer, source=file_path.suffix)

    elif args.abstract_only:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM articles WHERE analysis IS NULL AND relevance >= ? "
            "ORDER BY relevance DESC",
            (threshold,)
        ).fetchall()
        if args.limit:
            rows = rows[:args.limit]

        total = len(rows)
        print(f"共 {total} 篇文章用摘要重新解读")
        success = 0
        for i, row in enumerate(rows, 1):
            article = dict(row)
            print(f"  [{i}/{total}] {article.get('title','')[:55]}...", flush=True)
            article["has_fulltext"] = False
            try:
                result = analyzer.analyze_article(article)
                analysis = result.get("analysis")
                if analysis:
                    conn.execute(
                        "UPDATE articles SET analysis = ? WHERE id = ?",
                        (analysis, article["id"])
                    )
                    conn.commit()
                    success += 1
                    logger.info("  ✓ 完成")
            except Exception as e:
                logger.error("  ✗ 失败: %s", e)
        print(f"\n完成：{success}/{total} 篇解读成功")

    else:
        scan_uploads(conn, analyzer, upload_dir)

    print("刷新 HTML 索引页...")
    build_html_index(conn, threshold, "data/output/paper_index.html")
    print("全部完成！")


if __name__ == "__main__":
    main()
