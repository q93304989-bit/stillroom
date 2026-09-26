"""SQLite 历史：增删改查、筛选、上限裁剪、持久化。"""

from __future__ import annotations

import pytest

from app.services.history import HistoryStore, Record


@pytest.fixture
def store(tmp_path):
    with HistoryStore(tmp_path / "history.db") as s:
        yield s


def test_add_and_get(store):
    record = store.add(Record(kind="image", prompt="一只猫", params={"size": "512x512"}))
    loaded = store.get(record.id)
    assert loaded is not None
    assert loaded.prompt == "一只猫"
    assert loaded.params["size"] == "512x512"
    assert loaded.status == "success"


def test_list_is_newest_first_and_paginated(store):
    for i in range(5):
        store.add(Record(prompt=f"p{i}", created_at=1000 + i))

    first_page = store.list(limit=2)
    second_page = store.list(limit=2, offset=2)

    assert [r.prompt for r in first_page] == ["p4", "p3"]
    assert [r.prompt for r in second_page] == ["p2", "p1"]
    assert store.count() == 5


def test_filter_by_kind(store):
    store.add(Record(kind="image", prompt="图"))
    store.add(Record(kind="video", prompt="视频"))
    assert [r.kind for r in store.list(kind="video")] == ["video"]
    assert store.count(kind="all") == 2


def test_keyword_search_matches_prompt_and_params(store):
    store.add(Record(prompt="赛博朋克城市"))
    store.add(Record(prompt="别的", params={"model": "agnes-video-2.5-flash"}))

    assert [r.prompt for r in store.list(keyword="赛博")] == ["赛博朋克城市"]
    assert [r.prompt for r in store.list(keyword="2.5-flash")] == ["别的"]


def test_update_writes_media_path_and_status(store):
    record = store.add(Record(prompt="x"))
    updated = store.update(record.id, media_path="/tmp/a.png", duration=12.5, unknown="ignored")
    assert updated.media_path == "/tmp/a.png"
    assert updated.duration == 12.5


def test_update_meta_roundtrip(store):
    record = store.add(Record(prompt="x"))
    store.update(record.id, meta={"video_id": "vid-1", "seconds": "8"})
    assert store.get(record.id).meta["video_id"] == "vid-1"


def test_delete_and_clear(store):
    a = store.add(Record(prompt="a"))
    store.add(Record(prompt="b"))

    removed = store.delete(a.id)
    assert removed is not None and removed.prompt == "a"
    assert store.get(a.id) is None
    assert store.delete("不存在") is None

    assert store.clear() == 1
    assert store.count() == 0


def test_prune_keeps_only_max_records(tmp_path):
    with HistoryStore(tmp_path / "h.db", max_records=3) as store:
        for i in range(5):
            store.add(Record(prompt=f"p{i}", created_at=1000 + i))

        remaining = store.list(limit=10)
        assert store.count() == 3
        assert [r.prompt for r in remaining] == ["p4", "p3", "p2"]   # 最旧的两条被裁掉


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    with HistoryStore(path) as store:
        store.add(Record(prompt="持久化"))
    with HistoryStore(path) as store:
        assert store.count() == 1
        assert store.list()[0].prompt == "持久化"


def test_record_to_dict_shape(store):
    payload = store.add(Record(prompt="x")).to_dict()
    assert set(payload) >= {"id", "kind", "status", "prompt", "params", "refs", "meta", "created_at"}
