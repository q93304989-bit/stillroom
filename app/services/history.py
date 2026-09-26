"""历史记录存储（SQLite）。

替代旧版的「history.json 全量读改写」：每次增删改都 load 全量再 `dump(indent=2)`，
记录越多越慢。这里换成 SQLite：

- 增量写，不再全量重写；
- 关键词搜索与类型筛选走索引，不再在 Python 里全表扫；
- 500 条上限照旧保留（超出删最旧，同时删掉它的本地文件）。

历史数据不迁移（已确认），因此没有兼容旧 JSON 的代码。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_RECORDS = 500

#: 表结构版本（`PRAGMA user_version`）：改动表结构时 +1，并在 `_migrate` 里补一条分支
#: v1 加 thumb_path；v2 加 favorite / tags_json / last_action（用户评价信号）；
#: v3 加 prompt_versions（提示词补丁版本表，自更新用）；v4 加 run_contexts（可编辑上下文）
#: v5 加 kb_documents / kb_chunks（用户知识库：上传 → 切片 → 检索）
#: v6 加 context_profiles（长期档案）并给 run_contexts 补 kind / aspect（跑完「改参考再跑一次」要用）
SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    prompt       TEXT NOT NULL DEFAULT '',
    params_json  TEXT NOT NULL DEFAULT '{}',
    refs_json    TEXT NOT NULL DEFAULT '[]',
    result_url   TEXT,
    media_path   TEXT,
    thumb_path   TEXT,
    favorite     INTEGER NOT NULL DEFAULT 0,
    tags_json    TEXT NOT NULL DEFAULT '[]',
    last_action  TEXT,
    error        TEXT,
    meta_json    TEXT NOT NULL DEFAULT '{}',
    job_id       TEXT,
    created_at   REAL NOT NULL,
    duration     REAL
);
CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_kind ON records(kind, created_at DESC);
"""

#: 提示词补丁版本表（v3 起）。`version` 只在「被接受」时分配（全表最大 +1），
#: 提议中的补丁没有版本号；状态流转见 `app/agent/prompt_store.py`。
PROMPT_VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    id            TEXT PRIMARY KEY,
    version       INTEGER,
    status        TEXT NOT NULL,
    patch_json    TEXT NOT NULL DEFAULT '{}',
    reason        TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    decided_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_status
    ON prompt_versions(status, created_at DESC);
"""

#: 可编辑上下文（v4 起）：一次运行要用什么参考，先落成一份「草稿」，用户改完再跑。
#: `items_json` 里每条带 `user_state`——用户删掉的条目本次运行**不许复活**，
#: 所以删除是把状态改成 `removed` 留在表里，而不是删掉那一行（还要拿它过滤重新检索的结果）。
CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_contexts (
    id           TEXT PRIMARY KEY,
    run_id       TEXT,
    requirement  TEXT NOT NULL DEFAULT '',
    items_json   TEXT NOT NULL DEFAULT '[]',
    notes        TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '{}',
    decide_json  TEXT NOT NULL DEFAULT '{}',
    kind         TEXT NOT NULL DEFAULT 'image',
    aspect       TEXT NOT NULL DEFAULT '16:9',
    state        TEXT NOT NULL DEFAULT 'draft',
    created_at   REAL NOT NULL,
    used_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_run_contexts_state ON run_contexts(state, created_at DESC);
"""

#: 长期档案（v6 起）：把「这次的要求 + 参考条目 + 我补一句」存成一份可复用的档案，
#: 下次新草稿一键套用。与 `run_contexts`（一次运行的快照）分开：档案是用户攒下来的偏好，
#: 快照是某一次运行当时实际用了什么——前者可改可删，后者是留痕、不许事后改写。
PROFILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    items_json  TEXT NOT NULL DEFAULT '[]',
    notes       TEXT NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_profiles_default
    ON context_profiles(is_default, created_at DESC);
"""

#: 用户知识库（v5 起）：原文件留在 `<数据目录>/knowledge/`，解析出的文字切成片存进
#: `kb_chunks`——两者分开是为了「改了切片规则还能按原文件重建」。
#: 重复上传靠 `sha256` 认出来；`state` 走 pending → ready / failed，失败理由留在 `error`。
KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    error       TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    built_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_kb_documents_sha ON kb_documents(sha256);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id, ordinal);
"""


def new_record_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@dataclass
class Record:
    """一条生成历史。字段与旧版 history.json 的语义一一对应（便于回填参数）。"""

    id: str = field(default_factory=new_record_id)
    kind: str = "image"                 # image / video
    status: str = "success"             # success / failed
    prompt: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)
    result_url: str | None = None
    media_path: str | None = None
    thumb_path: str | None = None
    # 用户评价信号（A-RAG 找参考、自更新改提示词，都靠这几个字段）
    favorite: bool = False
    tags: list[str] = field(default_factory=list)
    last_action: str | None = None      # accept / retry / discard
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None
    created_at: float = field(default_factory=time.time)
    duration: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "prompt": self.prompt,
            "params": dict(self.params),
            "refs": list(self.refs),
            "result_url": self.result_url,
            "media_path": self.media_path,
            "thumb_path": self.thumb_path,
            "favorite": self.favorite,
            "tags": list(self.tags),
            "last_action": self.last_action,
            "error": self.error,
            "meta": dict(self.meta),
            "job_id": self.job_id,
            "created_at": self.created_at,
            "duration": self.duration,
        }


class HistoryStore:
    """SQLite 历史存储。同步 API（调用方在服务层用 `asyncio.to_thread` 包一层）。"""

    def __init__(self, db_path: str | Path, *, max_records: int = MAX_RECORDS) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_records = max_records
        self._lock = threading.RLock()
        # check_same_thread=False + 锁：允许被 to_thread 调用
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """把已有的库升到当前表结构（历史数据不迁移，但表结构要能就地升级）。"""
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(records)").fetchall()
                }
                if "thumb_path" not in columns:
                    self._conn.execute("ALTER TABLE records ADD COLUMN thumb_path TEXT")
                self._conn.execute("PRAGMA user_version = 1")
                self._conn.commit()
                version = 1
            if version < 2:
                # 用户评价信号：收藏 / 标签 / 最后一次选择
                columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(records)").fetchall()
                }
                if "favorite" not in columns:
                    self._conn.execute(
                        "ALTER TABLE records ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
                    )
                if "tags_json" not in columns:
                    self._conn.execute(
                        "ALTER TABLE records ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"
                    )
                if "last_action" not in columns:
                    self._conn.execute("ALTER TABLE records ADD COLUMN last_action TEXT")
                # 建在这个分支里而不是 SCHEMA 里：老库此时才刚有 favorite 列，
                # 放在 SCHEMA 里会在加列之前建索引，直接报 no such column
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_records_favorite "
                    "ON records(favorite, created_at DESC)"
                )
                self._conn.execute("PRAGMA user_version = 2")
                self._conn.commit()
                version = 2
            if version < 3:
                # 提示词补丁版本表（自更新）：表本身不涉及老库列，直接建
                self._conn.executescript(PROMPT_VERSIONS_SCHEMA)
                self._conn.execute("PRAGMA user_version = 3")
                self._conn.commit()
                version = 3
            if version < 4:
                # 可编辑上下文：同样不涉及老库列，直接建
                self._conn.executescript(CONTEXT_SCHEMA)
                self._conn.execute("PRAGMA user_version = 4")
                self._conn.commit()
                version = 4
            if version < 5:
                # 用户知识库：两张新表，同样不涉及老库列
                self._conn.executescript(KNOWLEDGE_SCHEMA)
                self._conn.execute("PRAGMA user_version = 5")
                self._conn.commit()
                version = 5
            if version < 6:
                # 长期档案（新表）+ 给 run_contexts 补 kind / aspect。
                # 老库（v4/v5）的 run_contexts 建表时没有这两列，必须就地加；
                # 新库由 CONTEXT_SCHEMA 直接带上了，这里判一下列是否存在即可。
                columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(run_contexts)").fetchall()
                }
                if "kind" not in columns:
                    self._conn.execute(
                        "ALTER TABLE run_contexts ADD COLUMN kind TEXT NOT NULL DEFAULT 'image'"
                    )
                if "aspect" not in columns:
                    self._conn.execute(
                        "ALTER TABLE run_contexts ADD COLUMN aspect TEXT NOT NULL DEFAULT '16:9'"
                    )
                self._conn.executescript(PROFILES_SCHEMA)
                self._conn.execute("PRAGMA user_version = 6")
                self._conn.commit()

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 写

    def add(self, record: Record) -> Record:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO records (id, kind, status, prompt, params_json, refs_json,
                                     result_url, media_path, thumb_path, favorite, tags_json,
                                     last_action, error, meta_json, job_id, created_at, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.kind,
                    record.status,
                    record.prompt,
                    json.dumps(record.params, ensure_ascii=False),
                    json.dumps(record.refs, ensure_ascii=False),
                    record.result_url,
                    record.media_path,
                    record.thumb_path,
                    1 if record.favorite else 0,
                    json.dumps(record.tags, ensure_ascii=False),
                    record.last_action,
                    record.error,
                    json.dumps(record.meta, ensure_ascii=False),
                    record.job_id,
                    record.created_at,
                    record.duration,
                ),
            )
            self._conn.commit()
            self._prune_locked()
        return record

    def update(self, record_id: str, **fields: Any) -> Record | None:
        """局部更新（补写本地缓存路径、状态等）。"""
        # 注意：评价信号请走 set_feedback()，那里做了字段名与取值的归一化
        allowed = {
            "status", "result_url", "media_path", "thumb_path", "error", "duration",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        for key in ("params", "meta"):
            if key in fields:
                updates[f"{key}_json"] = json.dumps(fields[key], ensure_ascii=False)
        if not updates:
            return self.get(record_id)
        columns = ", ".join(f"{name} = ?" for name in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE records SET {columns} WHERE id = ?",
                (*updates.values(), record_id),
            )
            self._conn.commit()
        return self.get(record_id)

    def delete(self, record_id: str) -> Record | None:
        record = self.get(record_id)
        if record is None:
            return None
        with self._lock:
            self._conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
            self._conn.commit()
        return record

    def clear(self) -> int:
        with self._lock:
            count = self.count()
            self._conn.execute("DELETE FROM records")
            self._conn.commit()
        return count

    def _prune_locked(self) -> int:
        """超出上限时删掉最旧的若干条，返回删除数量。"""
        removed = 0
        while True:
            total = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if total <= self.max_records:
                return removed
            row = self._conn.execute(
                "SELECT id FROM records ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return removed
            self._conn.execute("DELETE FROM records WHERE id = ?", (row["id"],))
            self._conn.commit()
            removed += 1

    # ---------------------------------------------------------------- 读

    def set_feedback(
        self,
        record_id: str,
        *,
        favorite: bool | None = None,
        tags: list[str] | None = None,
        action: str | None = None,
    ) -> Record | None:
        """记录用户对这条结果的反应：收藏 / 标签 / 最后一次选择（accept、retry、discard）。

        单独给一个入口，而不是让调用方去拼 `update()` 的字段名——这两个信号会被
        「找参考图」和「提示词自更新」直接消费，取值必须干净（标签去空、去掉重复）。
        `None` 表示「这一项不动」，所以可以只改其中一项。
        """
        updates: dict[str, Any] = {}
        if favorite is not None:
            updates["favorite"] = 1 if favorite else 0
        if tags is not None:
            cleaned: list[str] = []
            for tag in tags:
                text = str(tag).strip()
                if text and text not in cleaned:
                    cleaned.append(text)
            updates["tags_json"] = json.dumps(cleaned, ensure_ascii=False)
        if action is not None:
            updates["last_action"] = str(action)

        if not updates:
            return self.get(record_id)

        columns = ", ".join(f"{name} = ?" for name in updates)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE records SET {columns} WHERE id = ?",
                (*updates.values(), record_id),
            )
            self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get(record_id)

    # ---------------------------------------------------------------- 读

    def get(self, record_id: str) -> Record | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE id = ?", (record_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def list(
        self,
        *,
        kind: str | None = None,
        keyword: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Record]:
        """按类型与关键词筛选，最新在前。"""
        where, params = self._where(kind, keyword)
        sql = f"SELECT * FROM records {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit, offset)).fetchall()
        return [_row_to_record(row) for row in rows]

    def count(self, *, kind: str | None = None, keyword: str = "") -> int:
        where, params = self._where(kind, keyword)
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM records {where}", params
            ).fetchone()[0]

    @staticmethod
    def _where(kind: str | None, keyword: str) -> tuple[str, tuple]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind and kind != "all":
            clauses.append("kind = ?")
            params.append(kind)
        text = (keyword or "").strip()
        if text:
            clauses.append("(prompt LIKE ? OR params_json LIKE ?)")
            params.extend([f"%{text}%", f"%{text}%"])
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)

    def iter_all(self) -> Iterable[Record]:
        for record in self.list(limit=10**6):
            yield record


def _row_to_record(row: sqlite3.Row) -> Record:
    return Record(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        prompt=row["prompt"],
        params=_loads(row["params_json"], {}),
        refs=_loads(row["refs_json"], []),
        result_url=row["result_url"],
        media_path=row["media_path"],
        thumb_path=row["thumb_path"],
        favorite=bool(row["favorite"]),
        tags=_loads(row["tags_json"], []),
        last_action=row["last_action"],
        error=row["error"],
        meta=_loads(row["meta_json"], {}),
        job_id=row["job_id"],
        created_at=row["created_at"],
        duration=row["duration"],
    )


def _loads(text: str | None, fallback: Any) -> Any:
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback
