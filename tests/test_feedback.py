"""用户评价信号：收藏 / 标签 / 最后一次选择。

这是后面两件事的唯一依据：A-RAG 找参考图、以及提示词与参数的自更新。
所以除了「能存能读」，还要保证**老库能就地升级**（不丢已有历史）。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.services.history import SCHEMA, SCHEMA_VERSION, HistoryStore, Record


def test_favorite_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    with HistoryStore(path) as store:
        record = store.add(Record(prompt="中秋海报"))
        assert record.favorite is False
        store.set_feedback(record.id, favorite=True)
    with HistoryStore(path) as store:
        loaded = store.get(record.id)
        assert loaded.favorite is True


def test_tags_roundtrip(tmp_path):
    path = tmp_path / "h.db"
    with HistoryStore(path) as store:
        record = store.add(Record(prompt="x"))
        store.set_feedback(record.id, tags=["中秋", "竖版"])
    with HistoryStore(path) as store:
        assert store.get(record.id).tags == ["中秋", "竖版"]


def test_last_action_records_user_choice(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        record = store.add(Record(prompt="x"))
        assert store.get(record.id).last_action is None
        store.set_feedback(record.id, action="retry")
        assert store.get(record.id).last_action == "retry"
        store.set_feedback(record.id, action="accept")
        assert store.get(record.id).last_action == "accept"


def test_partial_update_keeps_other_fields(tmp_path):
    """只改收藏，不能把标签或选择清掉。"""
    with HistoryStore(tmp_path / "h.db") as store:
        record = store.add(Record(prompt="x"))
        store.set_feedback(record.id, tags=["a"], action="accept")
        store.set_feedback(record.id, favorite=True)

        loaded = store.get(record.id)
        assert loaded.favorite is True
        assert loaded.tags == ["a"]
        assert loaded.last_action == "accept"


def test_set_feedback_on_missing_record_returns_none(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        assert store.set_feedback("不存在", favorite=True) is None


def test_to_dict_exposes_feedback(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        record = store.add(Record(prompt="x"))
        store.set_feedback(record.id, favorite=True, tags=["t"], action="accept")
        payload = store.get(record.id).to_dict()
        assert payload["favorite"] is True
        assert payload["tags"] == ["t"]
        assert payload["last_action"] == "accept"


def test_legacy_v1_database_upgrades_in_place(tmp_path):
    """v1 库（没有这三列）打开时应自动升级，且原有数据完好。"""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE records (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '', params_json TEXT NOT NULL DEFAULT '{}',
            refs_json TEXT NOT NULL DEFAULT '[]', result_url TEXT, media_path TEXT,
            thumb_path TEXT, error TEXT, meta_json TEXT NOT NULL DEFAULT '{}',
            job_id TEXT, created_at REAL NOT NULL, duration REAL
        );
        """
    )
    connection.execute(
        "INSERT INTO records (id, kind, status, prompt, created_at) VALUES (?,?,?,?,?)",
        ("old-1", "image", "success", "老记录", 1000.0),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with HistoryStore(path) as store:
        loaded = store.get("old-1")
        assert loaded is not None and loaded.prompt == "老记录"
        assert loaded.favorite is False and loaded.tags == []
        store.set_feedback("old-1", favorite=True, tags=["旧"])
        assert store.get("old-1").favorite is True


def test_fresh_database_knows_new_columns(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        columns = {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(records)").fetchall()
        }
        assert {"favorite", "tags_json", "last_action"} <= columns
        version = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION


def test_favorite_is_orthogonal_to_status(tmp_path):
    """失败的记录也能收藏（比如"这个失败案例要留着看"）。"""
    with HistoryStore(tmp_path / "h.db") as store:
        record = store.add(Record(prompt="x", status="failed", error="内容审核"))
        store.set_feedback(record.id, favorite=True)
        loaded = store.get(record.id)
        assert loaded.status == "failed" and loaded.favorite is True
