"""
fetchers/manual_upload.py - 手动上传文献文件的最小可用入口

当自动抓取链路全部失败时，用户可通过此入口上传本地文献文件，
上传后进入与自动抓取完全一致的解析流程。

支持格式：.pdf  .html  .htm  .txt

CLI 用法：
    python -m fetchers.manual_upload --file /path/to/paper.pdf --doi 10.1021/jacs.xxx
    python -m fetchers.manual_upload --file /path/to/paper.html
"""
import argparse
import logging
import re
import sys
from pathlib import Path

from .models import FetchResult, FetchStatus, BestFormat, MAX_FULLTEXT_CHARS
from .pdf_fetcher import extract_pdf_bytes_to_text

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".html", ".htm", ".txt"}
MAX_FILE_SIZE_MB = 50
MAX_FULLTEXT_CHARS = 20000


class ManualUploader:
    """手动上传文献处理器，将本地文件解析为与自动抓取相同的 FetchResult"""

    def upload(self, file_path: str, doi: str = "") -> FetchResult:
        """
        处理用户手动上传的文献文件。

        Args:
            file_path: 本地文件路径（.pdf / .html / .htm / .txt）
            doi:       关联的 DOI（可选，用于与数据库记录绑定）

        Returns:
            FetchResult（source="manual", best_available_format="manual_uploaded"）
            失败时 fetch_status 为 WAITING_USER_UPLOAD 并携带 error_code。
        """
        path = Path(file_path)

        # ── 文件校验 ──────────────────────────────────────────
        if not path.exists():
            logger.error(f"文件不存在: {file_path}")
            return FetchResult(
                fetch_status=FetchStatus.WAITING_USER_UPLOAD,
                error_code="FILE_NOT_FOUND",
                source="manual",
            )

        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            logger.error(
                f"不支持的文件格式 {ext}，请上传以下格式之一: "
                f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
            return FetchResult(
                fetch_status=FetchStatus.WAITING_USER_UPLOAD,
                error_code=f"UNSUPPORTED_FORMAT:{ext}",
                source="manual",
            )

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            logger.error(f"文件过大 ({size_mb:.1f} MB)，上限 {MAX_FILE_SIZE_MB} MB")
            return FetchResult(
                fetch_status=FetchStatus.WAITING_USER_UPLOAD,
                error_code="FILE_TOO_LARGE",
                source="manual",
            )

        # ── 解析文本 ──────────────────────────────────────────
        if ext == ".pdf":
            text = self._parse_pdf(path)
        elif ext in (".html", ".htm"):
            text = self._parse_html(path)
        else:  # .txt
            text = path.read_text(encoding="utf-8", errors="replace")

        if not text or len(text.strip()) < 100:
            logger.warning(f"文件内容过短或无法提取文本: {file_path}")
            return FetchResult(
                fetch_status=FetchStatus.WAITING_USER_UPLOAD,
                error_code="EMPTY_CONTENT",
                source="manual",
            )

        text = re.sub(r'\s+', ' ', text.strip())[:MAX_FULLTEXT_CHARS]
        logger.info(
            f"手动上传成功: {path.name} ({len(text)} 字"
            + (f", DOI={doi}" if doi else "") + ")"
        )
        return FetchResult(
            text=text,
            best_available_format=BestFormat.MANUAL_UPLOADED,
            fetch_status=FetchStatus.SUCCESS,
            source="manual",
        )

    def _parse_pdf(self, path: Path) -> str:
        """从 PDF 文件提取文本（使用 pdf_fetcher 中的共享 PyMuPDF 实现）"""
        try:
            return extract_pdf_bytes_to_text(path.read_bytes())
        except Exception as e:
            logger.warning(f"PDF 文件读取失败: {e}")
            return ""

    def _parse_html(self, path: Path) -> str:
        """从 HTML 文件提取正文文本"""
        try:
            from bs4 import BeautifulSoup
            html = path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html, "lxml")
            paras = soup.find_all("p")
            return re.sub(r'\s+', ' ', " ".join(p.get_text(strip=True) for p in paras))
        except Exception as e:
            logger.warning(f"HTML 解析失败: {e}")
            return ""


def main():
    """CLI 入口：手动上传文献文件"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="手动上传文献文件，进入与自动抓取一致的解析流水线",
        epilog=(
            "示例:\n"
            "  python -m fetchers.manual_upload --file paper.pdf --doi 10.1021/jacs.xxx\n"
            "  python -m fetchers.manual_upload --file paper.html"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--file", required=True,
        help="文献文件路径（支持 .pdf / .html / .htm / .txt）",
    )
    parser.add_argument(
        "--doi", default="",
        help="关联的 DOI（可选）",
    )
    args = parser.parse_args()

    uploader = ManualUploader()
    result = uploader.upload(args.file, doi=args.doi)

    if result.fetch_status == FetchStatus.SUCCESS:
        print("\n✅ 上传成功！")
        print(f"   格式:     {result.best_available_format}")
        print(f"   来源:     {result.source}")
        print(f"   文本长度: {len(result.text)} 字")
        print(f"   预览:\n   {result.text[:300]}...")
        print(
            "\n提示：在 main.py 中将此 FetchResult 注入对应文章，"
            "即可进入与自动抓取相同的 LLM 解读流程。"
        )
    else:
        print(f"\n❌ 上传失败: {result.error_code}")
        print(
            "   提示：请检查文件路径与格式"
            f"（支持 {', '.join(sorted(SUPPORTED_EXTENSIONS))}）后重试。"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
