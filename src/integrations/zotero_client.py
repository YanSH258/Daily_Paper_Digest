"""
zotero_client.py - Zotero Web API 封装（基于 pyzotero）

职责：
- 把库内文章推送为 Zotero 条目（journalArticle），幂等（按 DOI 查重）
- 可附带 child note（阅读笔记 / AI 解读速记）与 OA PDF 附件
- PDF 附件策略：本地不存 PDF，附件只出现在 Zotero 端

凭据：
- api_key：环境变量 ZOTERO_API_KEY 或 config.zotero.api_key
- user_id：环境变量 ZOTERO_USER_ID 或 config.zotero.user_id；
  缺省时自动通过 GET https://api.zotero.org/keys/{key} 解析
"""
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"


class ZoteroError(RuntimeError):
    """Zotero 操作失败"""


def resolve_user_id(api_key: str) -> tuple[int, str]:
    """通过 API key 反查 user_id 与用户名（Zotero 官方支持）。"""
    import requests
    resp = requests.get(f"{ZOTERO_API_BASE}/keys/{api_key}", timeout=15)
    resp.raise_for_status()
    info = resp.json()
    user_id = info.get("userID")
    if not user_id:
        raise ZoteroError(f"API key 无效或缺少 userID: {info}")
    return int(user_id), info.get("username", "")


def _parse_creators(article: dict) -> list[dict[str, str]]:
    """把库内 authors 字符串解析为 Zotero creators。

    库内格式不统一（"Last, First" 或 "First Last"，分隔符 , 或 ;），
    采用启发式：优先按 ; 分隔；单名内含逗号视为 "Last, First"。
    """
    raw = article.get("authors") or ""
    if isinstance(raw, list):
        names = [str(x).strip() for x in raw if str(x).strip()]
    else:
        names = [x.strip() for x in re.split(r"[;,]", str(raw)) if x.strip()]
        # "Last, First" 风格按 ", " 被拆开了：两两配对还原
        if len(names) % 2 == 0 and all(" " not in n for n in names):
            names = [f"{names[i]}, {names[i+1]}" for i in range(0, len(names), 2)]
    creators = []
    for name in names[:30]:
        if "," in name:
            last, _, first = name.partition(",")
            creators.append({"creatorType": "author", "lastName": last.strip(),
                             "firstName": first.strip()})
        else:
            parts = name.split()
            if len(parts) > 1:
                creators.append({"creatorType": "author", "lastName": parts[-1],
                                 "firstName": " ".join(parts[:-1])})
            else:
                creators.append({"creatorType": "author", "lastName": name, "firstName": ""})
    return creators


class ZoteroClient:
    def __init__(self, config: dict) -> None:
        cfg = config.get("zotero", {}) or {}
        self.api_key = (os.environ.get("ZOTERO_API_KEY") or str(cfg.get("api_key") or "")).strip()
        self.collection = str(cfg.get("collection") or "").strip()
        self.include_note = bool(cfg.get("include_note", True))
        self.attach_oa_pdf = bool(cfg.get("attach_oa_pdf", True))
        if not self.api_key:
            raise ZoteroError("Zotero 未配置 api_key（环境变量 ZOTERO_API_KEY 或 config.zotero.api_key）")
        self.user_id = (os.environ.get("ZOTERO_USER_ID") or str(cfg.get("user_id") or "")).strip()
        if not self.user_id:
            uid, username = resolve_user_id(self.api_key)
            self.user_id = str(uid)
            logger.info("Zotero user_id 自动解析: %s (%s)", uid, username)
        from pyzotero.zotero import Zotero
        self.zot = Zotero(self.user_id, "user", self.api_key)

    # ── 连接测试 ────────────────────────────────────────────────
    def test_connection(self) -> dict[str, Any]:
        try:
            info = self.zot.key_info()
            collections = []
            try:
                collections = [
                    {"key": c["data"]["key"], "name": c["data"]["name"]}
                    for c in self.zot.collections()
                ]
            except Exception as e:  # noqa: BLE001 - collections 拉取失败不阻塞测试
                logger.warning("拉取 Zotero collections 失败: %s", e)
            return {"ok": True, "username": info.get("username", ""),
                    "user_id": self.user_id, "collections": collections}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ── 查重 ────────────────────────────────────────────────────
    def find_item_by_doi(self, doi: str) -> Optional[str]:
        """按 DOI 查已有条目，返回 item key；Zotero 端幂等的关键。

        注意：Zotero 搜索索引对新写入条目有秒级延迟，因此这里只作为
        兜底（首次推送历史旧库时用）；应用层会先查本地 zotero_key 字段。
        """
        if not doi:
            return None
        doi = doi.strip().lower()
        for attempt in range(2):
            try:
                items = self.zot.items(q=doi, qmode="everything", limit=10)
            except Exception as e:  # noqa: BLE001 - 查重失败不阻断推送
                logger.warning("Zotero DOI 查重失败（继续推送）: %s", e)
                return None
            for item in items:
                data = item.get("data", {})
                if str(data.get("doi", "")).strip().lower() == doi:
                    return data.get("key")
            if attempt == 0:
                import time
                time.sleep(1.5)  # 等搜索索引跟上
        return None

    # ── 推送 ────────────────────────────────────────────────────
    def push_article(self, article: dict, collection: Optional[str] = None) -> str:
        """推送单篇文章；已存在（按 DOI）则直接返回已有 key。"""
        doi = (article.get("doi") or "").strip()
        existing = self.find_item_by_doi(doi)
        if existing:
            return existing

        template = self.zot.item_template("journalArticle")
        template["title"] = article.get("title") or "Untitled"
        source = article.get("journal") or ""
        template["publicationTitle"] = source
        template["DOI"] = doi
        template["url"] = article.get("url") or ""
        template["abstractNote"] = (article.get("abstract") or "")[:3000]
        pub = str(article.get("pub_date") or "")
        if pub:
            template["date"] = pub
        m = re.match(r"^(\d{4})-", pub)
        if m:
            template["date"] = pub
            # Zotero 的 date 字段可含完整日期；年份单独解析
        template["creators"] = _parse_creators(article)
        template["tags"] = [{"tag": t} for t in self._article_tags(article)]
        coll = collection or self.collection
        if coll:
            template["collections"] = [coll]

        resp = self.zot.create_items([template])
        success = (resp.get("success") or {})
        if "0" not in success:
            raise ZoteroError(f"Zotero 创建条目失败: {resp.get('failed') or resp}")
        key = success["0"]

        note_text = self._build_note(article)
        if self.include_note and note_text:
            try:
                note = self.zot.item_template("note")
                note["note"] = note_text
                note["parentItem"] = key
                self.zot.create_items([note])
            except Exception as e:  # noqa: BLE001 - 笔记失败不影响条目
                logger.warning("Zotero 附加笔记失败: %s", e)

        if self.attach_oa_pdf:
            oa_url = self._oa_pdf_url(article)
            if oa_url:
                try:
                    self._attach_pdf(key, oa_url)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Zotero 附 OA PDF 失败（条目已创建）: %s", e)
        return key

    def _article_tags(self, article: dict) -> list[str]:
        tags = []
        if article.get("topic"):
            tags.append(str(article["topic"]))
        for t in str(article.get("tags") or "").split(","):
            t = t.strip()
            if t:
                tags.append(t)
        tags.append("daily-paper-digest")
        return tags[:15]

    @staticmethod
    def _build_note(article: dict) -> str:
        lines = [f"<p><b>推荐理由</b>：{article.get('relevance_reason') or '—'}</p>"]
        score = article.get("relevance")
        if score is not None:
            lines.append(f"<p><b>相关性</b>：{float(score):.1f}/10"
                         f"（{article.get('evidence_level') or 'UNKNOWN'} 依据）</p>")
        note = (article.get("note") or "").strip()
        if note:
            escaped = (note.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                       .replace("\n", "<br>"))
            lines.append(f"<p><b>我的笔记</b>：<br>{escaped}</p>")
        analysis = (article.get("analysis") or "").strip()
        if analysis:
            lines.append("<p><b>AI 解读速记</b>（详见工作台）：</p>")
            plain = re.sub(r"#+\s*", "", analysis)[:1200]
            plain = plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"<pre>{plain}</pre>")
        return "".join(lines)

    @staticmethod
    def _oa_pdf_url(article: dict) -> Optional[str]:
        url = article.get("fulltext_url") or ""
        if url.lower().endswith(".pdf") or "pdf" in url.lower():
            return url
        return None

    def _attach_pdf(self, item_key: str, pdf_url: str) -> None:
        import requests
        resp = requests.get(pdf_url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        if len(resp.content) < 1000 or not resp.content.startswith(b"%PDF"):
            raise ZoteroError("下载内容不是有效 PDF")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name
        try:
            att = self.zot.item_template("attachment")
            att["parentItem"] = item_key
            att["linkMode"] = "imported_file"
            att["title"] = "OA PDF"
            created = self.zot.create_items([att])
            att_key = (created.get("success") or {}).get("0")
            if not att_key:
                raise ZoteroError(f"创建附件条目失败: {created}")
            att["key"] = att_key
            self.zot.upload_attachment(att, tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
