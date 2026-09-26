"""上下文草稿的存储与「删过不许复活」这条硬规则。"""

from __future__ import annotations

from app.services.context_store import (
    DEFAULT_SOURCES,
    ContextItem,
    ContextStore,
    merge_items,
)


def item(kind: str, ref: str, title: str = "", origin: str = "") -> ContextItem:
    return ContextItem(kind=kind, ref=ref, title=title, origin=origin, score=0.5)


def test_create_and_get_roundtrip(tmp_path):
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create(
            "中秋海报",
            [item("history", "/media/a.png", origin="历史 09-21"), item("kb", "国潮配色：朱红+描金")],
            notes="不要出现文字",
            sources={"web_images": True},
            decide={"web_reason": "本地只 1 条"},
        )

        loaded = store.get(draft.id)
        assert loaded is not None
        assert loaded.requirement == "中秋海报"
        assert loaded.notes == "不要出现文字"
        assert loaded.state == "draft"
        assert [i.kind for i in loaded.items] == ["history", "kb"]
        assert loaded.items[0].origin == "历史 09-21"
        # 开关是「默认值 + 本次覆盖」，所以只覆盖一项也拿得到完整四件套
        assert loaded.sources == {**DEFAULT_SOURCES, "web_images": True}
        assert loaded.decide["web_reason"] == "本地只 1 条"


def test_removed_item_stays_in_table_but_not_in_kept(tmp_path):
    """删掉的条目要留痕（拿它过滤重找的结果），但交给运行时的不含它。"""
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create("海报", [item("history", "/media/a.png"), item("kb", "片段")])
        key = draft.items[0].key

        updated = store.remove_item(draft.id, key)
        assert updated is not None
        assert [i.user_state for i in updated.items] == ["removed", "kept"]
        assert [i.ref for i in updated.kept] == ["片段"]
        assert updated.removed_keys == {key}
        # 行还在：重找一次要拿它过滤
        assert len(store.get(draft.id).items) == 2


def test_merge_items_never_revives_removed(tmp_path):
    """重找一次时，用户删过的那条不许被检索结果带回界面。"""
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create("海报", [item("history", "/media/a.png"), item("kb", "旧片段")])
        removed = store.remove_item(draft.id, draft.items[0].key)

        found = [item("history", "/media/a.png"), item("kb", "新片段"), item("web", "网上线索")]
        merged = merge_items(found, removed_keys=removed.removed_keys, existing=removed.items)

        refs = [i.ref for i in merged]
        assert "/media/a.png" not in refs, "删过的参考又被检索带回来了"
        assert "新片段" in refs and "网上线索" in refs
        assert "旧片段" in refs                     # 没删的保留，且不重复
        assert refs.count("旧片段") == 1


def test_add_item_is_marked_added(tmp_path):
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create("海报")
        updated = store.add_item(draft.id, item("manual", "必须留白，别放文字"))

        assert updated is not None
        assert [(i.ref, i.user_state) for i in updated.items] == [("必须留白，别放文字", "added")]


def test_bind_run_makes_it_a_snapshot(tmp_path):
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create("海报", [item("kb", "片段")])
        bound = store.bind_run(draft.id, "run-1")

        assert bound is not None and bound.state == "running" and bound.used_at
        snapshot = store.of_run("run-1")
        assert snapshot is not None and snapshot.id == draft.id
        assert store.of_run("run-2") is None


def test_draft_lifecycle_and_stale_cleanup(tmp_path):
    with ContextStore(tmp_path / "h.db") as store:
        old = store.create("旧草稿")
        kept = store.create("新草稿")

        assert store.drop_stale_drafts(keep_id=kept.id) == 1
        assert store.get(old.id).state == "dropped"
        assert store.get(kept.id).state == "draft"

        store.finish(kept.id)
        assert store.get(kept.id).state == "done"
        assert [d.id for d in store.list(state="done")] == [kept.id]


def test_kind_and_aspect_survive_the_round_trip(tmp_path):
    """类型与画幅是草稿自身的一部分（「改参考再跑一次」要靠它接着跑）。"""
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create("中秋海报", [item("kb", "片段")], kind="video", aspect="9:16")
        loaded = store.get(draft.id)

        assert (loaded.kind, loaded.aspect) == ("video", "9:16")
        store.update(draft.id, aspect="1:1")
        assert store.get(draft.id).aspect == "1:1"


# --------------------------------------------------------------------------- 长期档案

def test_profile_round_trip_and_default(tmp_path):
    """存档案 / 取档案 / 设默认：同一时刻只允许一份默认。"""
    with ContextStore(tmp_path / "h.db") as store:
        first = store.save_profile("国潮风", [item("kb", "红金配色")], notes="留白要够")
        second = store.save_profile("写实风", [item("kb", "写实光影")], is_default=True)

        assert [p.name for p in store.list_profiles()] == ["写实风", "国潮风"]
        assert store.default_profile().id == second.id

        store.set_default_profile(first.id)
        assert store.default_profile().id == first.id
        assert [p.is_default for p in store.list_profiles()] == [True, False]

        assert store.set_default_profile("") is None
        assert store.default_profile() is None


def test_profile_drops_removed_items_and_names_itself(tmp_path):
    """档案不带已删条目；名字留空就用条目标题兜底（免得界面上一排无名档案）。"""
    with ContextStore(tmp_path / "h.db") as store:
        removed = item("history", "/media/a.png")
        removed.user_state = "removed"
        kept = item("kb", "红金配色", title="国潮配色要点")

        profile = store.save_profile("", [removed, kept])

        assert [i.ref for i in profile.items] == ["红金配色"]
        assert profile.name == "国潮配色要点"


def test_apply_profile_never_revives_removed_items(tmp_path):
    """套档案时同样遵守「删过的不复活」：档案里那条也不许回来。"""
    with ContextStore(tmp_path / "h.db") as store:
        shared = item("kb", "红金配色")
        draft = store.create("海报", [shared])
        store.remove_item(draft.id, shared.key)
        profile = store.save_profile("国潮风", [shared, item("kb", "留白要够")])

        updated = store.apply_profile(profile, draft.id)

        refs = [i.ref for i in updated.items]
        assert "红金配色" not in refs, "套档案把用户删过的条目带回来了"
        assert "留白要够" in refs


def test_apply_profile_merges_notes_once(tmp_path):
    with ContextStore(tmp_path / "h.db") as store:
        draft = store.create("海报", [item("kb", "片段")], notes="不要文字")
        profile = store.save_profile("国潮风", [item("kb", "另一片")], notes="留白要够")

        store.apply_profile(profile, draft.id)
        again = store.apply_profile(profile, draft.id)

        assert again.notes == "不要文字；留白要够"       # 重复套用不会把备注叠成两遍


def test_snapshot_and_reopen_are_separate_rows(tmp_path):
    """「改参考再跑一次」是复制一份新草稿：原快照留着当那次运行的留痕。"""
    with ContextStore(tmp_path / "h.db") as store:
        snapshot = store.save_snapshot(
            "run-9", "中秋海报", [item("history", "/media/a.png")],
            notes="不要文字", kind="image", aspect="9:16",
        )
        assert store.of_run("run-9").id == snapshot.id

        reopened = store.reopen("run-9")

        assert reopened.id != snapshot.id
        assert reopened.state == "draft"
        assert reopened.requirement == "中秋海报"
        assert reopened.notes == "不要文字"
        assert (reopened.kind, reopened.aspect) == ("image", "9:16")
        assert [i.ref for i in reopened.items] == ["/media/a.png"]
        # 原快照不动：run_id 仍绑在它身上，state 也还是 done
        assert store.get(snapshot.id).run_id == "run-9"
        assert store.get(snapshot.id).state == "done"

        assert store.reopen("run-404") is None
