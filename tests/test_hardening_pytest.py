import io
import os
import json
import sqlite3
from types import SimpleNamespace

import pytest

import web_server
from core.db import Database
from main import get_scheduler_timezone, validate_scheduler_time


def test_scheduler_validation_and_timezone():
    assert validate_scheduler_time("8:05") == "08:05"
    with pytest.raises(ValueError):
        validate_scheduler_time("99:99")
    with pytest.raises(ValueError):
        get_scheduler_timezone({"scheduler": {"timezone": "Not/AZone"}})
    assert get_scheduler_timezone({"scheduler": {"timezone": "UTC"}}).key == "UTC"


def test_non_loopback_requires_token():
    web_server.validate_bind_host("127.0.0.1", "")
    web_server.validate_bind_host("::1", "")
    with pytest.raises(ValueError):
        web_server.validate_bind_host("0.0.0.0", "")
    web_server.validate_bind_host("0.0.0.0", "secret")


def test_strict_json_boolean():
    assert web_server.parse_json_bool({"confirm": True}, "confirm") is True
    assert web_server.parse_json_bool({}, "confirm", default=False) is False
    with pytest.raises(web_server.RequestBodyError):
        web_server.parse_json_bool({"confirm": "false"}, "confirm")


class _BodyHandler:
    def __init__(self, body, length=None):
        self.headers = {"Content-Length": str(len(body) if length is None else length)}
        self.rfile = io.BytesIO(body)


def test_json_body_rejects_large_and_non_object():
    with pytest.raises(web_server.RequestBodyError) as exc:
        web_server._read_json_body(_BodyHandler(b"{}", web_server.MAX_JSON_BODY_BYTES + 1))
    assert exc.value.status == 413
    with pytest.raises(web_server.RequestBodyError):
        web_server._read_json_body(_BodyHandler(json.dumps([1]).encode()))


def test_runtime_and_dev_dependency_contract():
    root = __file__.rsplit("/tests/", 1)[0]
    assert not os.path.exists(f"{root}/requirement.txt")
    assert not os.path.exists(f"{root}/requirements-dev.txt")
    text = open(f"{root}/requirements.txt", encoding="utf-8").read().lower()
    runtime, marker, dev = text.partition("# ---- 以下为开发")
    assert marker, "dev section marker missing"
    for pkg in ("requests", "feedparser", "openai"):
        assert pkg in runtime
    assert "pytest" not in runtime and "httpx2" not in runtime
    for pkg in ("pytest", "httpx2"):
        assert pkg in dev


def test_database_rejects_orphans_and_cleans_article_dependents():
    db = Database(":memory:")
    try:
        assert db.set_watch_seed(999, True) is False
        assert db.add_citation_edge(999, 1000) is False
        assert db.integrity_report()["watched_seeds"] == 0
        article_id = db.save_manual_article({"title": "A", "doi": "10.1234/a"})
        topic_id = db.create_topic("T")
        assert article_id and topic_id
        assert db.add_topic_papers(topic_id, [article_id, 999]) == 1
        assert db.add_highlight(article_id, "quote")
        assert db.add_chat_message(article_id, "user", "question")
        assert db.set_watch_seed(article_id, True)
        assert db.delete_articles_by_ids([article_id]) == 1
        report = db.integrity_report()
        assert all(value == 0 for value in report.values())
    finally:
        db.close()
