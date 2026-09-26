"""提示词版本存储：提议/接受/拒绝/回滚、补丁白名单、v3 迁移。不联网。"""

from __future__ import annotations

import sqlite3

from app.agent.prompt_store import PromptStore, sanitize_patch, summarize_patch
from app.services.context_store import ContextStore
from app.services.history import SCHEMA_VERSION, HistoryStore

PATCH_V1 = {"composer_suffix": "画面里不要出现文字", "aspect_preference": "9:16"}
PATCH_V2 = {"composer_suffix": "强调暖色光线", "question_overrides": {"evaluate.fits": "这张图达没达到需求的要求？"}}


def make_store(tmp_path) -> PromptStore:
    return PromptStore(tmp_path / "h.db")


def test_fresh_store_is_factory_behavior(tmp_path):
    with make_store(tmp_path) as store:
        assert store.current() == {}
        assert store.current_version() == 0
        assert store.pending() is None
        assert store.can_rollback() is False


def test_propose_registers_a_pending_row(tmp_path):
    with make_store(tmp_path) as store:
        row = store.propose(PATCH_V1, reason="最近老出文字", evidence={"samples": 12})
        assert row is not None
        pending = store.pending()
        assert pending["patch"]["composer_suffix"] == "画面里不要出现文字"
        assert pending["reason"] == "最近老出文字"
        assert pending["evidence"]["samples"] == 12


def test_empty_patch_is_not_registered(tmp_path):
    with make_store(tmp_path) as store:
        assert store.propose({}) is None
        assert store.propose({"unknown_key": "x"}) is None
        assert store.pending() is None


def test_new_proposal_discards_the_old_one_but_keeps_trace(tmp_path):
    with make_store(tmp_path) as store:
        first = store.propose(PATCH_V1)
        second = store.propose(PATCH_V2)
        assert store.pending()["id"] == second["id"]
        statuses = {row["id"]: row["status"] for row in store.list()}
        assert statuses[first["id"]] == "discarded"        # 留痕：不是被拒绝，是被顶掉
        assert statuses[second["id"]] == "proposed"


def test_accept_activates_and_assigns_version(tmp_path):
    with make_store(tmp_path) as store:
        row = store.propose(PATCH_V1)
        accepted = store.accept(row["id"])
        assert accepted["status"] == "active"
        assert accepted["version"] == 1
        assert store.current()["aspect_preference"] == "9:16"
        assert store.current_version() == 1
        assert store.pending() is None


def test_second_accept_supersedes_the_first(tmp_path):
    with make_store(tmp_path) as store:
        first = store.accept(store.propose(PATCH_V1)["id"])
        second = store.accept(store.propose(PATCH_V2)["id"])
        assert second["version"] == 2
        statuses = {row["id"]: row["status"] for row in store.list()}
        assert statuses[first["id"]] == "superseded"
        assert store.can_rollback() is True


def test_rollback_restores_the_previous_version(tmp_path):
    with make_store(tmp_path) as store:
        store.accept(store.propose(PATCH_V1)["id"])
        store.accept(store.propose(PATCH_V2)["id"])
        restored = store.rollback()
        assert restored["version"] == 1
        assert store.current()["aspect_preference"] == "9:16"
        assert store.current_version() == 1


def test_rollback_without_history_returns_none(tmp_path):
    with make_store(tmp_path) as store:
        store.accept(store.propose(PATCH_V1)["id"])
        assert store.rollback() is None            # 只有一个版本，没有更旧的可回滚


def test_reject_keeps_trace_and_clears_pending(tmp_path):
    with make_store(tmp_path) as store:
        row = store.propose(PATCH_V1)
        rejected = store.reject(row["id"])
        assert rejected["status"] == "rejected"
        assert store.pending() is None
        assert store.current_version() == 0        # 拒绝不影响版本号
        assert store.list()[0]["status"] == "rejected"


def test_accept_or_reject_a_decided_row_is_a_noop(tmp_path):
    with make_store(tmp_path) as store:
        row = store.propose(PATCH_V1)
        store.reject(row["id"])
        assert store.accept(row["id"]) is None     # 已决定的不能再接受
        assert store.reject(row["id"]) is None


# --------------------------------------------------------------------------- 白名单

def test_sanitize_drops_unknown_keys_and_bad_values():
    patch = sanitize_patch({
        "composer_suffix": "  加上浅景深  ",
        "aspect_preference": "99:1",                       # 非法画幅
        "question_overrides": {
            "evaluate.fix": "哪里最该改？",
            "flows.structure": "把六步改成三步",            # 流程结构不许碰
            "understand.enough": "  ",
        },
        "drop_me": True,
    })
    assert patch == {
        "composer_suffix": "加上浅景深",
        "question_overrides": {"evaluate.fix": "哪里最该改？"},
    }


def test_sanitize_tolerates_garbage():
    assert sanitize_patch(None) == {}
    assert sanitize_patch("not a dict") == {}
    assert sanitize_patch({"question_overrides": "broken"}) == {}


def test_summarize_patch_in_plain_words():
    text = summarize_patch(PATCH_V2)
    assert "强调暖色光线" in text
    assert "evaluate.fits" in text
    assert summarize_patch({}) == "（空补丁）"


# --------------------------------------------------------------------------- 迁移

def test_history_store_v3_creates_the_table(tmp_path):
    db = tmp_path / "h.db"
    with HistoryStore(db):
        pass
    conn = sqlite3.connect(db)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert version == SCHEMA_VERSION
    assert "prompt_versions" in tables
    assert "run_contexts" in tables          # v4：可编辑上下文
    assert "context_profiles" in tables      # v6：长期档案


def test_v5_context_table_gains_kind_and_aspect(tmp_path):
    """老库（v5）的 run_contexts 没有 kind / aspect：迁移要就地补上，老行照旧读得出。"""
    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE run_contexts (id TEXT PRIMARY KEY, run_id TEXT,"
        " requirement TEXT NOT NULL DEFAULT '', items_json TEXT NOT NULL DEFAULT '[]',"
        " notes TEXT NOT NULL DEFAULT '', sources_json TEXT NOT NULL DEFAULT '{}',"
        " decide_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'draft',"
        " created_at REAL NOT NULL, used_at REAL)"
    )
    conn.execute(
        "INSERT INTO run_contexts (id, requirement, items_json, state, created_at)"
        " VALUES ('ctx-old', '老草稿', '[]', 'done', 1.0)"
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    # 走真实装配顺序：先开 HistoryStore（它负责迁移），再开 ContextStore（它只读写）
    with HistoryStore(db):
        with ContextStore(db) as store:
            loaded = store.get("ctx-old")
            # 老行没有这两列的值：按默认兜住，不能因此读不出来
            assert loaded.requirement == "老草稿"
            assert (loaded.kind, loaded.aspect) == ("image", "16:9")
            assert store.save_profile("新档案", []) is not None

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_legacy_v2_database_upgrades_in_place(tmp_path):
    """老库（v2，有 records 没 prompt_versions）打开后自动补表，历史数据不动。"""
    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,"
        " prompt TEXT NOT NULL DEFAULT '', params_json TEXT NOT NULL DEFAULT '{}',"
        " refs_json TEXT NOT NULL DEFAULT '[]', result_url TEXT, media_path TEXT,"
        " thumb_path TEXT, favorite INTEGER NOT NULL DEFAULT 0,"
        " tags_json TEXT NOT NULL DEFAULT '[]', last_action TEXT, error TEXT,"
        " meta_json TEXT NOT NULL DEFAULT '{}', job_id TEXT, created_at REAL NOT NULL,"
        " duration REAL)"
    )
    conn.execute(
        "INSERT INTO records (id, kind, status, prompt, created_at) "
        "VALUES ('r1', 'image', 'success', '老记录', 1.0)"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    with HistoryStore(db) as store:
        assert store.get("r1").prompt == "老记录"
    with PromptStore(db) as prompts:
        assert prompts.current_version() == 0      # 表已就位，只是还没有任何补丁

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
