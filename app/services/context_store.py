"""可编辑上下文的存储：先出草稿（理解 + 找参考），用户改完再跑。

三件事必须在这里定死，不然界面会变成「我改了它又给我加回来」：

1. **草稿与运行分开**：草稿只跑「理解 + 找参考」两步，不生成；确认后才绑到那次运行。
2. **用户裁决是硬约束**：删掉的条目状态改成 `removed` **留在表里**（不删行），
   重新检索时拿它过滤——所以用户删过的参考不会在「重找一次」之后复活。
3. **快照随运行落库**：确认后这份上下文绑到 `run_id`，出问题能回放「当时给它看了什么」。

同步 API + 自己的锁，与 `HistoryStore` / `PromptStore` 同一约定（它们共用一个 history.db）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.history import CONTEXT_SCHEMA, PROFILES_SCHEMA

#: 条目的种类（决定它怎么被用）：
#: history / web_image 是参考图；history_text / kb / web 是进提示词的文字线索；manual 是用户加的
ITEM_KINDS = ("history", "history_text", "kb", "web", "web_image", "manual")

#: 用户对条目的裁决
USER_STATES = ("kept", "removed", "added")

#: 四个来源开关的默认值（配图默认关：版权风险，见方案里的免责声明）
DEFAULT_SOURCES: dict[str, bool] = {
    "history": True,
    "knowledge": True,
    "web": True,
    "web_images": False,
}

#: 草稿状态
STATES = ("draft", "running", "done", "dropped")

#: 本地参考少于几条就联网（方案 4.1 的「代码默认」层；用户设置与模型判断取更高者）
DEFAULT_MIN_LOCAL_REFS = 2


@dataclass
class ContextItem:
    """一条上下文。`ref` 是图片路径/URL 或片段文本，`origin` 是给人看的来源说明。"""

    kind: str
    ref: str = ""
    title: str = ""
    origin: str = ""
    score: float | None = None
    user_state: str = "kept"
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """去重与「删过就不许复活」都按这个键比。"""
        return f"{self.kind}:{self.ref or self.title}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextItem":
        return cls(
            kind=str(data.get("kind") or "manual"),
            ref=str(data.get("ref") or ""),
            title=str(data.get("title") or ""),
            origin=str(data.get("origin") or ""),
            score=data.get("score"),
            user_state=str(data.get("user_state") or "kept"),
            meta=dict(data.get("meta") or {}),
        )


@dataclass
class ContextDraft:
    """一份上下文草稿（确认后就是那次运行的快照）。"""

    id: str
    requirement: str = ""
    items: list[ContextItem] = field(default_factory=list)
    notes: str = ""
    sources: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_SOURCES))
    decide: dict = field(default_factory=dict)
    kind: str = "image"
    aspect: str = "16:9"
    state: str = "draft"
    run_id: str | None = None
    created_at: float = 0.0
    used_at: float | None = None

    # ---------------------------------------------------------------- 取值辅助

    @property
    def kept(self) -> list[ContextItem]:
        """真正会交给运行时的条目（用户删掉的与已删的草稿条目都不算）。"""
        return [item for item in self.items if item.user_state != "removed"]

    @property
    def removed_keys(self) -> set[str]:
        return {item.key for item in self.items if item.user_state == "removed"}

    def references(self, *, kind: str) -> list[str]:
        """能当参考图的条目：历史作品与联网配图。视频只吃公网 URL。"""
        out: list[str] = []
        for item in self.kept:
            if item.kind not in ("history", "web_image") or not item.ref:
                continue
            if kind == "video" and not item.ref.lower().startswith(("http://", "https://")):
                continue
            out.append(item.ref)
        return out

    def hints(self) -> list[str]:
        """能进提示词的文字线索：知识库片段与联网摘要。"""
        return [
            item.ref
            for item in self.kept
            if item.kind in ("history_text", "kb", "web") and item.ref
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement": self.requirement,
            "items": [item.to_dict() for item in self.items],
            "notes": self.notes,
            "sources": dict(self.sources),
            "decide": dict(self.decide),
            "kind": self.kind,
            "aspect": self.aspect,
            "state": self.state,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "used_at": self.used_at,
        }


def merge_items(
    found: Iterable[ContextItem], *, removed_keys: set[str], existing: Iterable[ContextItem] = ()
) -> list[ContextItem]:
    """把新一轮检索的结果并进来：**删过的键直接丢掉**，已有的按 key 去重。

    这是「用户删了不许复活」的落点：不管检索多准，只要键在 `removed_keys` 里就不进来。
    """
    merged: list[ContextItem] = []
    seen: set[str] = set()
    for item in list(existing) + list(found):
        key = item.key
        if key in seen or key in removed_keys:
            continue
        seen.add(key)
        merged.append(item)
    return merged


@dataclass
class ContextProfile:
    """一份长期档案：把常用的「要求 + 参考条目 + 我补一句」存下来，下次一键套用。

    与草稿（一次运行的快照）刻意分开：档案是**用户攒的偏好**，可改可删、可设默认；
    快照是**那次运行当时实际用了什么**，只读、不许事后改写。
    """

    id: str
    name: str = ""
    items: list[ContextItem] = field(default_factory=list)
    notes: str = ""
    is_default: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "items": [item.to_dict() for item in self.items],
            "notes": self.notes,
            "is_default": self.is_default,
            "created_at": self.created_at,
        }


class ContextStore:
    """`run_contexts`（草稿 / 运行快照）与 `context_profiles`（长期档案）两张表的唯一入口。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(CONTEXT_SCHEMA)      # 防御：测试里可能只建本 store
        self._conn.executescript(PROFILES_SCHEMA)     # 同上：长期档案表
        self._ensure_columns()                        # 同上：老库的 run_contexts 缺 kind / aspect
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` 对已有老表是空操作，所以要显式补列。

        正式流程里 `HistoryStore` 的 v6 迁移会先补一遍；这里是为「只建本 store」的
        场景兜底（同时也是幂等的，跑第二遍不会出错）。
        """
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(run_contexts)")
        }
        if "kind" not in columns:
            self._conn.execute(
                "ALTER TABLE run_contexts ADD COLUMN kind TEXT NOT NULL DEFAULT 'image'"
            )
        if "aspect" not in columns:
            self._conn.execute(
                "ALTER TABLE run_contexts ADD COLUMN aspect TEXT NOT NULL DEFAULT '16:9'"
            )

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ContextStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 写

    def create(
        self,
        requirement: str,
        items: Iterable[ContextItem] = (),
        *,
        notes: str = "",
        sources: Mapping[str, bool] | None = None,
        decide: Mapping[str, Any] | None = None,
        kind: str = "image",
        aspect: str = "16:9",
    ) -> ContextDraft:
        draft = ContextDraft(
            id="ctx-" + uuid.uuid4().hex[:10],
            requirement=str(requirement or ""),
            items=list(items),
            notes=str(notes or ""),
            sources={**DEFAULT_SOURCES, **dict(sources or {})},
            decide=dict(decide or {}),
            kind=str(kind or "image"),
            aspect=str(aspect or "16:9"),
            state="draft",
            created_at=time.time(),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO run_contexts
                    (id, run_id, requirement, items_json, notes, sources_json,
                     decide_json, kind, aspect, state, created_at, used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    None,
                    draft.requirement,
                    json.dumps([i.to_dict() for i in draft.items], ensure_ascii=False),
                    draft.notes,
                    json.dumps(draft.sources, ensure_ascii=False),
                    json.dumps(draft.decide, ensure_ascii=False),
                    draft.kind,
                    draft.aspect,
                    draft.state,
                    draft.created_at,
                    None,
                ),
            )
            self._conn.commit()
        return draft

    def update(
        self,
        draft_id: str,
        *,
        requirement: str | None = None,
        items: Iterable[ContextItem] | None = None,
        notes: str | None = None,
        sources: Mapping[str, bool] | None = None,
        decide: Mapping[str, Any] | None = None,
        kind: str | None = None,
        aspect: str | None = None,
    ) -> ContextDraft | None:
        """改草稿（界面上的增删、改需求、改开关都走这里）。"""
        with self._lock:
            draft = self.get(draft_id)
            if draft is None:
                return None
            if requirement is not None:
                draft.requirement = str(requirement)
            if items is not None:
                draft.items = list(items)
            if notes is not None:
                draft.notes = str(notes)
            if sources is not None:
                draft.sources = {**DEFAULT_SOURCES, **dict(sources)}
            if decide is not None:
                draft.decide = {**draft.decide, **dict(decide)}
            if kind is not None:
                draft.kind = str(kind or "image")
            if aspect is not None:
                draft.aspect = str(aspect or "16:9")
            self._write(draft)
        return draft

    def remove_item(self, draft_id: str, key: str) -> ContextDraft | None:
        """删掉一条：**留痕**（状态 `removed`），下次重找也不许复活。"""
        with self._lock:
            draft = self.get(draft_id)
            if draft is None:
                return None
            changed = False
            for item in draft.items:
                if item.key == key:
                    item.user_state = "removed"
                    changed = True
            if changed:
                self._write(draft)
        return draft

    def add_item(self, draft_id: str, item: ContextItem) -> ContextDraft | None:
        """用户自己加一条（要求 / 图片路径 / URL）。"""
        item.user_state = "added"
        with self._lock:
            draft = self.get(draft_id)
            if draft is None:
                return None
            draft.items = merge_items([item], removed_keys=set(), existing=draft.items)
            self._write(draft)
        return draft

    def bind_run(self, draft_id: str, run_id: str) -> ContextDraft | None:
        """确认后绑到那次运行：从此这份上下文就是那次运行的快照。"""
        with self._lock:
            draft = self.get(draft_id)
            if draft is None:
                return None
            draft.run_id = str(run_id)
            draft.state = "running"
            draft.used_at = time.time()
            self._write(draft)
        return draft

    def finish(self, draft_id: str, *, state: str = "done") -> ContextDraft | None:
        with self._lock:
            draft = self.get(draft_id)
            if draft is None:
                return None
            draft.state = state
            self._write(draft)
        return draft

    # ---------------------------------------------------------------- 读

    def get(self, draft_id: str) -> ContextDraft | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_contexts WHERE id = ?", (draft_id,)
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def of_run(self, run_id: str) -> ContextDraft | None:
        """按运行 id 取快照（回放那次运行给它看了什么）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_contexts WHERE run_id = ? ORDER BY used_at DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def list(self, *, state: str | None = None, limit: int = 20) -> list[ContextDraft]:
        sql = "SELECT * FROM run_contexts"
        args: list[Any] = []
        if state:
            sql += " WHERE state = ?"
            args.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def drop_stale_drafts(self, *, keep_id: str | None = None) -> int:
        """把没确认的旧草稿标成 dropped（草稿不该在库里堆着）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE run_contexts SET state = 'dropped' "
                "WHERE state = 'draft' AND (? IS NULL OR id != ?)",
                (keep_id, keep_id),
            )
            self._conn.commit()
            return cur.rowcount

    def save_snapshot(
        self,
        run_id: str,
        requirement: str,
        items: Iterable[ContextItem] = (),
        *,
        notes: str = "",
        sources: Mapping[str, bool] | None = None,
        decide: Mapping[str, Any] | None = None,
        kind: str = "image",
        aspect: str = "16:9",
    ) -> ContextDraft:
        """把一次自动模式运行「实际用了什么」落成快照（绑 run_id）。

        自动模式没有草稿，但跑完仍要能「改参考再跑一次」——所以这里补一份留痕。
        """
        draft = ContextDraft(
            id="ctx-" + uuid.uuid4().hex[:10],
            requirement=str(requirement or ""),
            items=list(items),
            notes=str(notes or ""),
            sources={**DEFAULT_SOURCES, **dict(sources or {})},
            decide=dict(decide or {}),
            kind=str(kind or "image"),
            aspect=str(aspect or "16:9"),
            state="done",
            run_id=str(run_id),
            created_at=time.time(),
            used_at=time.time(),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO run_contexts
                    (id, run_id, requirement, items_json, notes, sources_json,
                     decide_json, kind, aspect, state, created_at, used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    draft.run_id,
                    draft.requirement,
                    json.dumps([i.to_dict() for i in draft.items], ensure_ascii=False),
                    draft.notes,
                    json.dumps(draft.sources, ensure_ascii=False),
                    json.dumps(draft.decide, ensure_ascii=False),
                    draft.kind,
                    draft.aspect,
                    draft.state,
                    draft.created_at,
                    draft.used_at,
                ),
            )
            self._conn.commit()
        return draft

    def reopen(self, run_id: str) -> ContextDraft | None:
        """按某次运行的快照开一份**新的可编辑草稿**（「改参考再跑一次」的入口）。

        是复制、不是复用原行：原快照是那次运行的留痕，不许事后改写。
        """
        snapshot = self.of_run(run_id)
        if snapshot is None:
            return None
        draft = self.create(
            snapshot.requirement,
            [ContextItem.from_dict(item.to_dict()) for item in snapshot.items],
            notes=snapshot.notes,
            sources=snapshot.sources,
            decide=snapshot.decide,
            kind=snapshot.kind,
            aspect=snapshot.aspect,
        )
        self.drop_stale_drafts(keep_id=draft.id)
        return draft

    # ---------------------------------------------------------------- 长期档案

    def save_profile(
        self,
        name: str,
        items: Iterable[ContextItem] = (),
        *,
        notes: str = "",
        is_default: bool = False,
    ) -> ContextProfile:
        """存一份档案（名字为空就用需求原文前 24 字，免得界面上一排无名档案）。"""
        kept = [item for item in items if item.user_state != "removed"]
        profile = ContextProfile(
            id="prof-" + uuid.uuid4().hex[:10],
            name=str(name or "").strip()[:60] or (kept[0].title[:24] if kept else "未命名档案"),
            items=kept,
            notes=str(notes or ""),
            is_default=bool(is_default),
            created_at=time.time(),
        )
        with self._lock:
            if profile.is_default:
                self._conn.execute("UPDATE context_profiles SET is_default = 0")
            self._conn.execute(
                """
                INSERT INTO context_profiles
                    (id, name, items_json, notes, is_default, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.id,
                    profile.name,
                    json.dumps([i.to_dict() for i in profile.items], ensure_ascii=False),
                    profile.notes,
                    1 if profile.is_default else 0,
                    profile.created_at,
                ),
            )
            self._conn.commit()
        return profile

    def list_profiles(self, *, limit: int = 50) -> list[ContextProfile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM context_profiles ORDER BY is_default DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def get_profile(self, profile_id: str) -> ContextProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM context_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def default_profile(self) -> ContextProfile | None:
        """当前默认档案（没有「设为默认」的，就取最近存的一份）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM context_profiles ORDER BY is_default DESC, created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        profile = self._row_to_profile(row)
        return profile if profile.is_default else None

    def set_default_profile(self, profile_id: str) -> ContextProfile | None:
        """把某份设成默认（同时取消别的）；传空串表示取消所有默认。"""
        with self._lock:
            self._conn.execute("UPDATE context_profiles SET is_default = 0")
            if profile_id:
                self._conn.execute(
                    "UPDATE context_profiles SET is_default = 1 WHERE id = ?", (profile_id,)
                )
            self._conn.commit()
        return self.get_profile(profile_id) if profile_id else None

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM context_profiles WHERE id = ?", (profile_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def apply_profile(self, profile: ContextProfile, draft_id: str) -> ContextDraft | None:
        """把档案套到一份草稿上：条目并进去（用户删过的照旧不复活）、备注合起来。"""
        with self._lock:
            draft = self.get(draft_id)
            if draft is None:
                return None
            draft.items = merge_items(
                profile.items, removed_keys=draft.removed_keys, existing=draft.items
            )
            if profile.notes:
                # 按「；」拆开去重：重复套同一份档案不会把备注叠成两遍
                parts: list[str] = []
                for chunk in (draft.notes, profile.notes):
                    parts.extend(part.strip() for part in str(chunk).split("；") if part.strip())
                draft.notes = "；".join(dict.fromkeys(parts))
            self._write(draft)
        return draft

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> ContextProfile:
        return ContextProfile(
            id=row["id"],
            name=row["name"] or "",
            items=[ContextItem.from_dict(i) for i in _loads(row["items_json"], [])],
            notes=row["notes"] or "",
            is_default=bool(row["is_default"]),
            created_at=row["created_at"],
        )

    # ---------------------------------------------------------------- 内部

    def _write(self, draft: ContextDraft) -> None:
        self._conn.execute(
            """
            UPDATE run_contexts
               SET run_id = ?, requirement = ?, items_json = ?, notes = ?,
                   sources_json = ?, decide_json = ?, kind = ?, aspect = ?,
                   state = ?, used_at = ?
             WHERE id = ?
            """,
            (
                draft.run_id,
                draft.requirement,
                json.dumps([i.to_dict() for i in draft.items], ensure_ascii=False),
                draft.notes,
                json.dumps(draft.sources, ensure_ascii=False),
                json.dumps(draft.decide, ensure_ascii=False),
                draft.kind,
                draft.aspect,
                draft.state,
                draft.used_at,
                draft.id,
            ),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_draft(row: sqlite3.Row) -> ContextDraft:
        keys = set(row.keys())
        return ContextDraft(
            id=row["id"],
            requirement=row["requirement"] or "",
            items=[ContextItem.from_dict(i) for i in _loads(row["items_json"], [])],
            notes=row["notes"] or "",
            sources={**DEFAULT_SOURCES, **_loads(row["sources_json"], {})},
            decide=_loads(row["decide_json"], {}),
            # 老库（v6 之前的行）可能没有这两列：缺了就当图片 / 16:9，不要因此读不出来
            kind=(row["kind"] if "kind" in keys else None) or "image",
            aspect=(row["aspect"] if "aspect" in keys else None) or "16:9",
            state=row["state"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            used_at=row["used_at"],
        )


def _loads(text: str | None, fallback: Any) -> Any:
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback
