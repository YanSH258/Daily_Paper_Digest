"""统一日报服务：预览、发布、重新生成、读取、渲染与渠道投递的唯一业务入口。

所有入口（CLI、HTTP、MCP、主流水线）通过本服务操作版本化日报：
- 选择规则仍由 digest.builder / selector 提供（纯计算，不发请求）；
- 快照持久化由 core.db 的 digest_versions/items/artifacts/sends 承担；
- 渲染与渠道投递由 core.notifier 承担（render 与 send 分离）。

语义（docs/DEVELOPMENT_PLAN_2026-09-11.md §4）：
- preview 只计算不写库；
- publish 当日已有版本则复用（不重算、不重发）；否则计算 → 快照事务 → 渲染 → 投递；
- regenerate 用新 request_key 创建新版本，旧版保留；同 key 重试返回原版本；
- 渲染/发送失败不撤销版本，按产物/发送记录恢复；
- 历史读取失败显式报错（HISTORY_UNAVAILABLE），不默认当成空历史。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import tempfile
from copy import deepcopy
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from .builder import build_daily_digest
from .config import collect_daily_config

SCHEMA_VERSION = 1

# 渠道枚举：publish/regenerate/retry 只接受这些渠道，不接收任意 webhook 地址
CHANNELS = ("email", "feishu")

# 快照保存渲染所需的文章字段（不只存可变 article_id，文章删除不影响历史）
_SNAPSHOT_FIELDS = (
    "title", "authors", "journal", "pub_date", "url", "doi", "abstract",
    "analysis", "evidence_level", "title_zh", "topic", "relevance",
    "relevance_reason", "created_at", "cas_zone", "impact_factor",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DigestError(Exception):
    """带稳定错误码的日报业务异常。"""

    def __init__(self, message: str, *, code: str = "DIGEST_ERROR",
                 retryable: bool = False,
                 recovery_action: str = "检查配置与输入后重试",
                 details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.recovery_action = recovery_action
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            **({"details": self.details} if self.details else {}),
        }


def _invalid_config(msg: str) -> DigestError:
    return DigestError(msg, code="INVALID_CONFIG", recovery_action="修正 digest 配置后重试")


def parse_digest_date(date_str: Any) -> str:
    """严格解析 YYYY-MM-DD；不合法抛 INVALID_DATE。"""
    if not isinstance(date_str, str) or not _DATE_RE.fullmatch(date_str.strip()):
        raise DigestError(f"日期格式必须为 YYYY-MM-DD，当前为 {date_str!r}",
                          code="INVALID_DATE", recovery_action="使用 2026-09-11 形式的日期")
    try:
        datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError as e:
        raise DigestError(f"日期不合法：{date_str!r}（{e}）",
                          code="INVALID_DATE", recovery_action="使用真实存在的日期") from e
    return date_str.strip()


def normalize_channels(channels: Any) -> list[str]:
    """渠道白名单归一化：只接受明确枚举，去重保序。"""
    if channels is None:
        return []
    if isinstance(channels, str):
        channels = [channels]
    if not isinstance(channels, (list, tuple)):
        raise _invalid_config(f"channels 必须是列表，当前为 {type(channels).__name__}")
    out: list[str] = []
    for ch in channels:
        if ch not in CHANNELS:
            raise _invalid_config(
                f"未知渠道 {ch!r}，可用渠道：{', '.join(CHANNELS)}")
        if ch not in out:
            out.append(ch)
    return out


def deterministic_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DigestService:
    """版本化日报的业务入口。notifier 可为 None（仅测试选择/快照路径）。"""

    def __init__(self, db, config: dict[str, Any], notifier=None) -> None:
        self.db = db
        self.config = config or {}
        self.notifier = notifier

    # ── 预览 ──────────────────────────────────────────────
    def preview_digest(self, date_str: str, config: Optional[dict] = None) -> dict[str, Any]:
        """只读预览：不写任何业务表（CLI 审阅文件由调用方另行处理）。"""
        date = parse_digest_date(date_str)
        cfg = self.config if config is None else config
        result = self._compute_selection(date, cfg)
        sel = result["selection"]
        return {
            "date": date,
            "preview": True,
            "articles_above_threshold": result["articles_above_threshold"],
            "excluded_repeat": sel.stats.get("excluded_repeat", 0),
            "selected_count": len(sel.selected),
            "by_category": sel.stats.get("by_category", {}),
            "items": [
                {
                    "article_id": row.get("id"),
                    "rank": i,
                    "title": row.get("title") or "",
                    "category": (row.get("scores") or {}).get("category"),
                    "final": (row.get("scores") or {}).get("final"),
                    # 只读补充：方向与已有推荐依据（来自文章库评分，不现场生成）
                    "topic": row.get("topic") or "",
                    "reason": (row.get("relevance_reason") or "").strip()[:300],
                }
                for i, row in enumerate(sel.selected, start=1)
            ],
            "errors": [],
        }

    # ── 发布 / 重新生成 ───────────────────────────────────
    def publish_digest(self, date_str: str, request_key: Optional[str] = None,
                       channels: Any = None,
                       config: Optional[dict] = None) -> dict[str, Any]:
        """发布当日日报：已有版本则复用（不重算、不重发）。"""
        date = parse_digest_date(date_str)
        channel_list = normalize_channels(channels)
        cfg = deepcopy(self.config if config is None else config)
        request_key = self._normalize_request_key(request_key)
        request_hash = self._request_hash(date, channel_list, cfg, "publish")
        if request_key:
            by_key = self._get_by_request_key(request_key)
            if by_key is not None:
                self._check_request(by_key, request_hash)
                return self._result_from_version(by_key, created=False,
                                                 note="同请求键幂等复用，未重新计算")
        existing = self._read(self.db.get_latest_published_digest_version, date)
        if existing is not None:
            return self._result_from_version(existing, created=False,
                                             note="当日已有发布版本，直接复用（不重复发送）")
        return self._compute_and_persist(date, request_key, channel_list, cfg=cfg)

    def regenerate_digest(self, date_str: str, request_key: str,
                          channels: Any = None,
                          config: Optional[dict] = None) -> dict[str, Any]:
        """显式重新生成：必须携带新 request_key；同一 key 重试返回原版本。"""
        date = parse_digest_date(date_str)
        channel_list = normalize_channels(channels)
        if not request_key or not str(request_key).strip():
            raise DigestError("regenerate 必须提供新的 request_key",
                              code="INVALID_CONFIG",
                              recovery_action="使用未用过的 request_key 标识本次重新生成")
        request_key = self._normalize_request_key(request_key)
        cfg = deepcopy(self.config if config is None else config)
        request_hash = self._request_hash(date, channel_list, cfg, "regenerate")
        by_key = self._get_by_request_key(request_key)
        if by_key is not None:
            self._check_request(by_key, request_hash)
            return self._result_from_version(by_key, created=False,
                                             note="同请求键幂等复用，未创建新版本")
        return self._compute_and_persist(date, request_key, channel_list,
                                         cfg=cfg, regenerate=True)

    # ── 读取 ──────────────────────────────────────────────
    def get_digest(self, version_id: int) -> dict[str, Any]:
        version = self._get_version_or_raise(version_id)
        items = self._read(self.db.get_digest_items, version_id)
        return self._result_from_version(version, items=items)

    def list_digest_history(self, date_from: Optional[str] = None,
                            date_to: Optional[str] = None,
                            limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if date_from:
            parse_digest_date(date_from)
        if date_to:
            parse_digest_date(date_to)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        versions = self._read(self.db.list_digest_versions, date_from=date_from, date_to=date_to,
                                                limit=limit + 1, offset=offset)
        has_more = len(versions) > limit
        versions = versions[:limit]
        return {
            "items": [
                {
                    "version_id": v["id"], "date": v["digest_date"],
                    "version": v["version"], "status": v["status"],
                    "content_hash": v["content_hash"], "created_at": v["created_at"],
                }
                for v in versions
            ],
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }

    # ── 渲染恢复 ──────────────────────────────────────────
    def render_digest(self, version_id: int,
                      formats: Any = ("markdown", "html")) -> dict[str, Any]:
        """从固定快照重建版本化文件（不重新选文、不发送）。"""
        version = self._get_version_or_raise(version_id)
        items = self._read(self.db.get_digest_items, version_id)
        if self.notifier is None:
            raise DigestError("未配置渲染器，无法恢复产物", code="RENDER_FAILED",
                              recovery_action="配置 Notifier 后调用 render_digest")
        if isinstance(formats, str):
            formats = [formats]
        if not isinstance(formats, (list, tuple)) or not formats or any(
                fmt not in ("markdown", "html") for fmt in formats):
            raise _invalid_config("formats 必须是 markdown/html 的非空列表")
        try:
            _, errors = self._render_and_record(version, items, formats=formats)
        except sqlite3.Error:
            errors = [{"code": "RENDER_FAILED", "message": "产物状态写入失败，版本已保留",
                       "recovery_action": "检查数据库后调用 render_digest"}]
        return self._result_from_version(version, errors=errors)

    # ── 渠道投递（工作包 E） ─────────────────────────────
    def send_digest(self, version_id: int, channels: Any) -> dict[str, Any]:
        """按渠道投递已发布版本：逐渠道领取（claim）→ 事务外投递 → 回写。"""
        version = self._get_version_or_raise(version_id)
        channel_list = normalize_channels(channels)
        if self._read(self.db.get_digest_items, version_id):
            self.db.init_digest_sends(version_id, channel_list)
        deliveries, errors = self._deliver(version, channel_list)
        return self._result_from_version(
            version, deliveries=deliveries, errors=errors)

    def retry_digest_send(self, version_id: int, channels: Any = None) -> dict[str, Any]:
        """补发：只处理 pending/failed 渠道；sent 不重发；unknown 需显式处理。"""
        version = self._get_version_or_raise(version_id)
        requested = normalize_channels(channels)
        existing = {s["channel"]: s for s in self.db.list_digest_sends(version_id)}
        if requested:
            targets = [ch for ch in requested
                       if ch in existing and existing[ch]["status"] in ("pending", "failed")]
            skipped = [ch for ch in requested
                       if ch not in existing or existing[ch]["status"] not in ("pending", "failed")]
        else:
            targets = [ch for ch, s in existing.items()
                       if s["status"] in ("pending", "failed")]
            skipped = [ch for ch, s in existing.items()
                       if s["status"] not in ("pending", "failed")]
        deliveries, errors = self._deliver(version, targets)
        if skipped:
            note = "；".join(
                f"{ch}: {existing[ch]['status']}（普通补发不处理，需显式操作）" if existing.get(ch)
                else f"{ch}: 无发送记录" for ch in skipped)
            errors = errors + [{"code": "SKIPPED", "message": note}]
        return self._result_from_version(version, deliveries=deliveries, errors=errors)

    def recover_digest_send(self, version_id: int, channel: str, claim_token: str) -> dict[str, Any]:
        """Operator-confirmed interrupted claim: sending → unknown, never resend."""
        version = self._get_version_or_raise(version_id)
        normalize_channels([channel])
        if not isinstance(claim_token, str) or not claim_token:
            raise _invalid_config("claim_token 必须是非空字符串")
        if not self.db.finish_digest_send(version_id, channel, claim_token, "unknown",
                                           error="显式恢复中断投递，需核对远端"):
            raise DigestError("发送状态或领取令牌已变化", code="CONFLICT")
        return self._result_from_version(version)

    def resolve_digest_send(self, version_id: int, channel: str, delivered: bool) -> dict[str, Any]:
        """Reconcile unknown after checking remote delivery; False only queues retry."""
        version = self._get_version_or_raise(version_id)
        normalize_channels([channel])
        if not isinstance(delivered, bool):
            raise _invalid_config("delivered 必须是布尔值")
        if not self.db.resolve_digest_send(version_id, channel, delivered):
            raise DigestError("仅 unknown 状态允许显式核对", code="CONFLICT")
        return self._result_from_version(version)

    # ── 内部实现 ──────────────────────────────────────────
    @staticmethod
    def _normalize_request_key(key):
        if key is None:
            return None
        if not isinstance(key, str) or not key.strip():
            raise _invalid_config("request_key 必须是非空字符串")
        return key.strip()

    @staticmethod
    def _config_snapshot(cfg):
        values, errors = collect_daily_config(cfg)
        if errors:
            raise _invalid_config("digest 配置无效: " + "; ".join(errors))
        return {
            "schema_version": SCHEMA_VERSION,
            "relevance_threshold": values["min_score"],
            # Match the classifier's case/whitespace/set semantics. Empty and
            # omitted lists both retain its built-in journal hints.
            "top_chemistry_journals": sorted({
                str(name).strip().lower()
                for name in (((cfg.get("digest") or {}).get("tracks") or {})
                             .get("top_chemistry", {}).get("journals") or [])
                if str(name).strip()
            }),
            "digest_daily": {k: values[k] for k in (
                "limit", "repeat_window_days", "pool_window_days", "category_limits")},
        }

    def _request_hash(self, date, channels, cfg, operation):
        return _sha256(deterministic_json({
            "date": date, "channels": sorted(channels), "operation": operation,
            "config": self._config_snapshot(cfg),
        }))

    @staticmethod
    def _check_request(version, request_hash):
        if version.get("request_hash") != request_hash:
            raise DigestError("request_key 已用于不同或无法核验的请求", code="CONFLICT",
                              recovery_action="更换新的 request_key")

    def _compute_selection(self, date: str, cfg: dict[str, Any]) -> dict[str, Any]:
        values, errors = collect_daily_config(cfg)
        if errors:
            raise _invalid_config("digest 配置无效: " + "; ".join(errors))
        try:
            return build_daily_digest(self.db, cfg, date_str=date, dry_run=True)
        except ValueError as e:
            raise _invalid_config(str(e)) from e
        except sqlite3.Error as e:
            raise DigestError(
                f"防重历史读取失败：{e}", code="HISTORY_UNAVAILABLE",
                retryable=True, recovery_action="检查数据库后重试") from e

    def _compute_and_persist(self, date: str, request_key: Optional[str],
                             channels: list[str], *, cfg: Optional[dict] = None,
                             regenerate: bool = False) -> dict[str, Any]:
        cfg = self.config if cfg is None else cfg
        selection = self._compute_selection(date, cfg)
        sel = selection["selection"]
        values, _ = collect_daily_config(cfg)

        # 条目快照：文章删除不影响历史（快照自带渲染所需全部字段）
        items: list[dict[str, Any]] = []
        for i, row in enumerate(sel.selected, start=1):
            scores = row.get("scores") or {}
            snapshot = {f: row.get(f) for f in _SNAPSHOT_FIELDS}
            items.append({
                "article_id": row.get("id"),
                "rank": i,
                "category": scores.get("category"),
                "scores_json": deterministic_json(scores),
                "reason": (row.get("relevance_reason") or "").strip()[:500],
                "snapshot_json": deterministic_json(snapshot),
            })
        content_hash = _sha256(deterministic_json({"schema_version": SCHEMA_VERSION, "items": items}))
        config_snapshot = self._config_snapshot(cfg)
        config_json = deterministic_json(config_snapshot)
        config_hash = _sha256(config_json)
        stats = {
            "candidates": selection["articles_above_threshold"],
            "excluded_repeat": sel.stats.get("excluded_repeat", 0),
            "selected": len(items),
            "by_category": sel.stats.get("by_category", {}),
        }
        stats_json = deterministic_json(stats)

        try:
            saved = self.db.save_digest_version(
                digest_date=date, items=items, config_json=config_json,
                config_hash=config_hash, content_hash=content_hash,
                stats_json=stats_json,
                request_key=request_key, status="published", channels=channels,
                reuse_published=not regenerate,
                request_hash=self._request_hash(date, channels, cfg,
                                                "regenerate" if regenerate else "publish"),
            )
        except sqlite3.IntegrityError as e:
            raise DigestError(
                f"版本并发/幂等冲突：{e}", code="CONFLICT", retryable=True,
                recovery_action="同日重复发布无需重试；regenerate 请换新的 request_key") from e
        except sqlite3.Error as e:
            raise DigestError(
                f"快照持久化失败：{e}", code="PERSIST_FAILED", retryable=True,
                recovery_action="检查数据库可写性后重试") from e

        version = self._get_version_or_raise(saved["version_id"])
        if not saved["created"]:
            return self._result_from_version(version, note="并发发布已完成，复用固定快照")
        items = self._read(self.db.get_digest_items, version["id"])
        # 快照事务已提交即发布；渲染与发送失败不撤销版本
        render_errors: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        if self.notifier is not None:
            try:
                artifacts, render_errors = self._render_and_record(version, items)
            except sqlite3.Error:
                render_errors = [{"code": "RENDER_FAILED", "message": "产物状态写入失败，版本已保留",
                                  "recovery_action": "检查数据库后调用 render_digest"}]
            if channels and items and not render_errors:
                deliveries, send_errors = self._deliver(version, channels)
                errors = render_errors + send_errors
            else:
                errors = render_errors
        else:
            errors = render_errors
        return self._result_from_version(version, items=items, created=True,
                                         artifacts=artifacts, deliveries=deliveries,
                                         errors=errors,
                                         note="空候选：已保存空版本，默认不发送" if not items else None)

    def _render_and_record(self, version: dict[str, Any],
                           items: list[dict[str, Any]], *, formats=("markdown", "html")) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """渲染版本文件并记录产物状态；失败返回 RENDER_FAILED 错误（版本保留）。"""
        errors: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        try:
            payload = self._render_payload(version, items)
            payload["formats"] = list(formats)
            rendered = self.notifier.render_digest_version(payload)
        except Exception as e:  # noqa: BLE001 - 渲染失败保留版本，允许恢复
            errors.append({"code": "RENDER_FAILED", "message": "渲染失败，请检查渲染器"})
            for fmt in formats:
                self.db.upsert_digest_artifact(version["id"], fmt, status="failed",
                                               error="渲染器失败")
            return artifacts, errors
        by_format = {a["format"]: a for a in rendered}
        for fmt in formats:
            a = by_format.get(fmt)
            if not self._verified_artifact(a):
                self.db.upsert_digest_artifact(version["id"], fmt, status="failed",
                                               error="产物缺失或 SHA256 不匹配")
                errors.append({"code": "RENDER_FAILED", "message": f"{fmt} 产物未通过核验"})
                continue
            self.db.upsert_digest_artifact(
                version["id"], fmt, path=a["path"],
                content_hash=a["content_hash"], status="rendered")
            artifacts.append(a)
        return artifacts, errors

    def _deliver(self, version: dict[str, Any],
                 channels: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """逐渠道领取并投递；外部调用在领取事务之外执行。"""
        deliveries: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if self.notifier is None or not channels or not self._read(self.db.get_digest_items, version["id"]):
            return deliveries, errors
        for ch in channels:
            # Re-read records and bytes for each channel. Never pass an unchecked
            # HTML path: the notifier prefers it over Markdown for email.
            artifacts = {a["format"]: a for a in self.db.list_digest_artifacts(version["id"])}
            required = ("markdown", "html") if ch == "email" else ("markdown",)
            verified_bytes = {fmt: self._artifact_bytes(artifacts.get(fmt)) for fmt in required}
            invalid = [fmt for fmt in required if verified_bytes[fmt] is None]
            if invalid:
                for fmt in invalid:
                    self.db.upsert_digest_artifact(version["id"], fmt, status="failed",
                                                   error="产物缺失或 SHA256 不匹配")
                errors.append({"code": "RENDER_FAILED", "message": "产物未通过 SHA256 核验，拒绝投递",
                               "recovery_action": "调用 render_digest 恢复产物后重试"})
                continue
            token = uuid.uuid4().hex
            row = self.db.claim_digest_send(version["id"], ch, token)
            if row is None or row.get("claim_token") != token or row["status"] != "sending":
                continue
            # Transport and persistence failures must not share an except block:
            # once transport returns successfully, retry may duplicate delivery.
            status, error = "sent", None
            try:
                # Send the verified bytes, not a source path another writer may replace.
                with tempfile.TemporaryDirectory(prefix="dpd-delivery-") as tmp:
                    md_path = Path(tmp) / "report.md"
                    md_path.write_bytes(verified_bytes["markdown"])
                    html_path = None
                    if ch == "email":
                        html_path = Path(tmp) / "report.html"
                        html_path.write_bytes(verified_bytes["html"])
                    self.notifier.send_digest_files(
                        md_path=str(md_path), html_path=str(html_path) if html_path else None,
                        date_str=version["digest_date"], channels=[ch])
            except TimeoutError as exc:
                status, error = "unknown", "投递超时，结果未知"
                delivered = getattr(exc, "delivered_parts", None)
                total = getattr(exc, "total_parts", None)
                if (type(delivered) is int and type(total) is int
                        and 0 <= delivered < total):
                    error = f"分段投递未完成：已确认 {delivered}/{total} 段，请核对远端后处理"
            except Exception:
                status, error = "failed", "渠道投递失败"
            try:
                ok = self.db.finish_digest_send(version["id"], ch, token, status, error=error)
            except Exception:
                ok = False
            if not ok:
                # Best effort CAS to unknown; if the DB remains unavailable the
                # durable sending claim still blocks all automatic retries.
                status, error = "unknown", "投递结果回写失败，请核对远端"
                try:
                    self.db.finish_digest_send(version["id"], ch, token, "unknown", error=error)
                except Exception:
                    pass
            if status != "sent":
                errors.append({"code": "DELIVERY_UNKNOWN" if status == "unknown" else "DELIVERY_FAILED",
                               "message": f"{ch}: {error}",
                               "recovery_action": "核对远端后显式处理" if status == "unknown"
                               else "调用 retry_digest_send 补发该渠道"})
        return deliveries, errors

    @staticmethod
    def _artifact_bytes(artifact):
        if not artifact or artifact.get("status") != "rendered" or not artifact.get("content_hash"):
            return None
        try:
            content = Path(artifact["path"]).read_bytes()
            return content if hashlib.sha256(content).hexdigest() == artifact["content_hash"] else None
        except (OSError, TypeError, KeyError):
            return None

    @classmethod
    def _verified_artifact(cls, artifact):
        return cls._artifact_bytes(artifact) is not None

    @staticmethod
    def _render_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only accept decoded persisted rows; never substitute empty snapshots."""
        out: list[dict[str, Any]] = []
        for it in items:
            scores = it.get("scores")
            if not isinstance(scores, dict):
                raise DigestError("持久化条目 scores 损坏", code="HISTORY_UNAVAILABLE")
            snapshot = it.get("snapshot")
            if not isinstance(snapshot, dict):
                raise DigestError("持久化条目 snapshot 损坏", code="HISTORY_UNAVAILABLE")
            out.append({
                "rank": it.get("rank"),
                "article_id": it.get("article_id"),
                "category": it.get("category"),
                "scores": scores,
                "reason": it.get("reason") or "",
                "snapshot": snapshot,
            })
        return out

    def _render_payload(self, version: dict[str, Any],
                        items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "digest_date": version["digest_date"],
            "version": version["version"],
            "version_id": version["id"],
            "content_hash": version.get("content_hash") or "",
            "stats": version.get("stats") or {},
            "items": self._render_items(items),
        }

    @staticmethod
    def _read(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.Error as e:
            raise DigestError("日报持久化数据读取失败", code="HISTORY_UNAVAILABLE",
                              recovery_action="检查数据库完整性后重试") from e

    def _get_version_or_raise(self, version_id: int) -> dict[str, Any]:
        version = self._read(self.db.get_digest_version_by_id, version_id)
        if version is None:
            raise DigestError(f"日报版本不存在：{version_id}", code="NOT_FOUND",
                              recovery_action="用 list_digest_history 查询可用版本")
        return version

    def _get_by_request_key(self, request_key: str) -> Optional[dict[str, Any]]:
        return self._read(self.db.get_digest_version_by_request_key, request_key)

    def _result_from_version(self, version: dict[str, Any],
                             items: Optional[list[dict[str, Any]]] = None,
                             *, created: bool = False,
                             artifacts: Optional[list[dict[str, Any]]] = None,
                             deliveries: Optional[list[dict[str, Any]]] = None,
                             errors: Optional[list[dict[str, Any]]] = None,
                             note: Optional[str] = None) -> dict[str, Any]:
        # Persisted rows, not this call's local work list, are authoritative.
        items = self._read(self.db.get_digest_items, version["id"])
        artifacts = self.db.list_digest_artifacts(version["id"])
        deliveries = self.db.list_digest_sends(version["id"])
        errors = list(errors or [])
        codes = {e.get("code") for e in errors}
        for artifact in artifacts:
            if artifact["status"] != "rendered":
                code = "RENDER_FAILED" if artifact["status"] == "failed" else "RENDER_PENDING"
                if code not in codes:
                    errors.append({"code": code, "message": "产物尚未完成",
                                   "recovery_action": "调用 render_digest 恢复"})
                    codes.add(code)
        for delivery in deliveries:
            state = delivery["status"]
            if state in ("sent", "skipped"):
                continue
            code = {"failed": "DELIVERY_FAILED", "unknown": "DELIVERY_UNKNOWN",
                    "sending": "DELIVERY_SENDING", "pending": "DELIVERY_PENDING"}.get(state, "DELIVERY_UNKNOWN")
            if code not in codes:
                errors.append({"code": code, "message": f"{delivery['channel']}: {state}",
                               "recovery_action": "显式核对投递结果" if state in ("sending", "unknown")
                               else "调用 retry_digest_send"})
                codes.add(code)
        overall = "success"
        if any(e.get("code") in ("PERSIST_FAILED", "HISTORY_UNAVAILABLE") for e in errors):
            overall = "failed"
        elif any(e.get("code") != "SKIPPED" for e in errors) or any(
                a.get("status") != "rendered" for a in artifacts):
            overall = "partial"
        return {
            "version_id": version["id"],
            "date": version["digest_date"],
            "version": version["version"],
            "status": version["status"],
            "created": created,
            "selected_count": len(items),
            "content_hash": version.get("content_hash") or "",
            "stats": version.get("stats") or {},
            "artifacts": artifacts,
            "deliveries": deliveries,
            "overall_status": overall,
            "errors": errors,
            "items": self._render_items(items),
            **({"note": note} if note else {}),
        }
