"""Persistent, explicitly retried workbench operations using the shared task log."""
import copy
import logging
import threading
import uuid

logger = logging.getLogger(__name__)
KINDS = {"zotero", "translate"}
MAX_BATCH_ARTICLES = 1000


class BatchTaskError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def article_ids(values):
    if (not isinstance(values, list) or not values or
            any(isinstance(i, bool) or not str(i).isdigit() or int(i) <= 0 for i in values)):
        raise BatchTaskError("请选择文献（ids 必须为正整数列表）")
    ids = list(dict.fromkeys(int(i) for i in values))
    if len(ids) > MAX_BATCH_ARTICLES:
        raise BatchTaskError(f"每个后台任务最多 {MAX_BATCH_ARTICLES} 篇，请分批提交")
    return ids


def task_view(row):
    """Reconstruct pending/interrupted work without treating it as successful."""
    task = copy.deepcopy(row)
    stats = task.get("stats") or {}
    results = stats.get("results") or []
    done = {r["id"] for r in results}
    task.update(kind=task["mode"].removeprefix("batch_"), total=len(stats.get("ids", [])),
                completed=len(done), results=results, retry_task_id=stats.get("retry_task_id"),
                pending_ids=[i for i in stats.get("ids", []) if i not in done])
    task.pop("stats", None)  # internal request options are not part of the display contract
    return task


class BatchTaskRunner:
    """One background worker per server; overlapping submissions fail explicitly."""
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._active = None
        self._thread = None

    def submit(self, kind, ids, options, config, processor, request_id=None, retry_of=None):
        if not isinstance(kind, str) or kind not in KINDS:
            raise BatchTaskError("kind 必须是 zotero 或 translate")
        ids = article_ids(ids)
        try:
            task_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            raise BatchTaskError("request_id 必须是 UUID") from None
        with self._lock:
            existing = self.db.get_batch_task(task_id)
            if existing:
                stats = existing["stats"]
                if existing["mode"] != "batch_" + kind or stats["ids"] != ids or stats["options"] != options:
                    raise BatchTaskError("该提交编号已用于其他任务", 409)
                return task_view(existing)
            parent = self.db.get_batch_task(retry_of) if retry_of else None
            if parent and parent["stats"].get("retry_task_id"):
                return task_view(self.db.get_batch_task(parent["stats"]["retry_task_id"]))
            if self._active:
                raise BatchTaskError("后台任务正在运行，请在任务页查看进度后重试", 409)
            snapshot = copy.deepcopy(config)
            stats = {"ids": ids, "options": copy.deepcopy(options), "results": [], "current_ids": [], "retry_of": retry_of}
            self.db.create_batch_task(task_id, "batch_" + kind, stats)
            if parent:
                parent["stats"]["retry_task_id"] = task_id
                try:
                    self.db.save_batch_task(retry_of, parent["status"], parent["stage"], parent["stats"], parent["error"])
                except Exception:
                    self.db.save_batch_task(task_id, "failed", "failed", stats, "重试关联保存失败，任务未执行")
                    raise
            self._active = task_id
            thread = threading.Thread(target=self._run, args=(task_id, kind, stats, snapshot, processor), daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._active = None
                self.db.save_batch_task(task_id, "failed", "failed", stats, "后台线程未能启动")
                raise
            return task_view(self.db.get_batch_task(task_id))

    def retry(self, task_id, config, processor):
        row = self.db.get_batch_task(task_id)
        if not row:
            raise BatchTaskError("任务不存在", 404)
        if row["status"] == "running":
            raise BatchTaskError("任务仍在运行", 409)
        stats = row["stats"]
        kind = row["mode"].removeprefix("batch_")
        results = {r["id"]: r for r in stats["results"]}
        current = set(stats.get("current_ids", []))
        ids = []
        for aid in stats["ids"]:
            result = results.get(aid)
            if result and result.get("ok") and not result.get("warnings"):
                continue
            if result and result.get("retryable") is False:
                articles = self.db.get_articles_by_ids([aid]) if kind == "zotero" else []
                if not articles or not (articles[0].get("zotero_key") or articles[0].get("doi")):
                    continue
            # A crash during a Zotero write may have created an unrecorded parent.
            if kind == "zotero" and aid in current and result is None:
                articles = self.db.get_articles_by_ids([aid])
                if not articles or not (articles[0].get("zotero_key") or articles[0].get("doi")):
                    continue
            ids.append(aid)
        if not ids:
            raise BatchTaskError("没有可安全重试的条目；请先核对中断时的 Zotero 条目及本地关联")
        return self.submit(kind, ids, stats["options"], config, processor, retry_of=task_id)

    def _run(self, task_id, kind, stats, config, processor):
        try:
            size = 20 if kind == "translate" else 1
            for start in range(0, len(stats["ids"]), size):
                chunk = stats["ids"][start:start + size]
                stats["current_ids"] = chunk
                # Persist intent before any network side effect.
                self.db.save_batch_task(task_id, "running", "processing", stats)
                try:
                    results = processor(kind, chunk, stats["options"], config)
                    if {r["id"] for r in results} != set(chunk) or len(results) != len(chunk):
                        raise ValueError("Incomplete processor result")
                except Exception:
                    logger.exception("后台任务处理失败: %s", task_id)
                    results = [{"id": aid, "ok": False, "retryable": kind != "zotero",
                                "error": "处理失败，请检查连接并核对结果后重试"} for aid in chunk]
                stats["results"].extend(results)
                stats["current_ids"] = []
                self.db.save_batch_task(task_id, "running", "processing", stats)
            incomplete = [r for r in stats["results"] if not r.get("ok") or r.get("warnings")]
            status = "partial" if incomplete else "success"
            if incomplete and all(not r.get("ok") for r in stats["results"]):
                status = "failed"
            self.db.save_batch_task(task_id, status, "done", stats)
        except Exception:
            logger.exception("后台任务记录保存失败，已停止后续处理: %s", task_id)
            try:
                self.db.save_batch_task(task_id, "interrupted", "interrupted", stats, "任务中断，请核对结果后重试未完成条目")
            except Exception:
                logger.exception("无法保存任务中断状态: %s", task_id)
        finally:
            with self._lock:
                self._active = None
            # File-backed connections are thread-local; never close a shared in-memory DB here.
            if self.db._memory_conn is None:
                self.db.close()
