"""提示词补丁的版本存储：提议 → 人工确认 → 生效，全程留痕、可回滚。

设计约束（任务 7 的硬性要求）：

1. **只提议，不自动改**：LLM 生成的建议只是 `proposed`，用户点「接受」才生效；
   点「拒绝」留痕，再也不会出现。
2. **版本化**：每次接受分配一个递增版本号（1、2、3……），旧版本标 `superseded` 保留，
   随时能回滚到上一个版本。版本 0 表示「出厂行为」（没有任何补丁）。
3. **补丁有边界**：LLM 只能填白名单里的字段（写提示词的附加指令、画幅偏好、
   判断问题的文案覆盖），多给的键一律丢弃——它改不了流程结构，也造不出新问题。

表结构由 `HistoryStore` 的 v3 迁移创建（同一个 history.db）；这里自己连接同一个文件
（WAL 模式允许多连接），并防御性地 `CREATE TABLE IF NOT EXISTS`。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from app.services.history import PROMPT_VERSIONS_SCHEMA

#: 补丁里允许出现的键（此外的一律丢弃）
PATCH_KEYS = ("composer_suffix", "aspect_preference", "question_overrides")

#: 判断问题允许被覆盖的 id（阶段.问题）。
#: 注意这里**没有** `understand.feasible`：能力边界是补丁不该碰的东西——
#: 自更新只能改问法和偏好，不能把「我做不了」改成「我能做」。
OVERRIDABLE_QUESTIONS = (
    "understand.enough",
    "understand.task",
    "understand.aspect",
    "understand.reference_need",
    "understand.web_search",
    "evaluate.fits",
    "evaluate.quality",
    "evaluate.fix",
)

#: 画幅偏好允许的取值（与 prompts.ASPECT_OPTIONS 的闭集一致，空串表示「无偏好」）
ASPECT_CHOICES = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

#: 状态：提议中 / 生效中 / 已被新版取代 / 已被拒绝 / 被更新的提议顶掉（留痕）
STATUSES = ("proposed", "active", "superseded", "rejected", "discarded")


def sanitize_patch(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """把一份（可能来自 LLM 的）补丁裁剪到白名单内。返回干净的补丁，可能是空 dict。"""
    if not isinstance(patch, Mapping):
        return {}
    cleaned: dict[str, Any] = {}

    suffix = str(patch.get("composer_suffix") or "").strip()
    if suffix:
        cleaned["composer_suffix"] = suffix

    aspect = str(patch.get("aspect_preference") or "").strip()
    if aspect in ASPECT_CHOICES:
        cleaned["aspect_preference"] = aspect

    overrides = patch.get("question_overrides")
    if isinstance(overrides, Mapping):
        kept = {
            key: str(text).strip()
            for key, text in overrides.items()
            if key in OVERRIDABLE_QUESTIONS and str(text).strip()
        }
        if kept:
            cleaned["question_overrides"] = kept

    return cleaned


def summarize_patch(patch: Mapping[str, Any]) -> str:
    """把补丁压成一行人能看懂的话（设置页展示用）。"""
    parts: list[str] = []
    suffix = str(patch.get("composer_suffix") or "")
    if suffix:
        parts.append(f"写提示词时附加：「{suffix[:60]}」")
    aspect = str(patch.get("aspect_preference") or "")
    if aspect:
        parts.append(f"画幅偏好 {aspect}")
    overrides = patch.get("question_overrides") or {}
    if overrides:
        parts.append(f"改 {len(overrides)} 个判断问题的问法（{'、'.join(sorted(overrides))}）")
    return "；".join(parts) if parts else "（空补丁）"


class PromptStore:
    """`prompt_versions` 表的唯一入口。同步 API（与 HistoryStore 同一约定）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(PROMPT_VERSIONS_SCHEMA)   # 防御：测试里可能只建本 store
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "PromptStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 写

    def propose(
        self,
        patch: Mapping[str, Any],
        *,
        reason: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """登记一条建议。空补丁不登记（没东西可改的建议没有意义）。

        同一时刻只保留一条「待决定」的建议：新提议进来时，旧的提议标 `discarded` 留痕。
        """
        cleaned = sanitize_patch(patch)
        if not cleaned:
            return None
        row = {
            "id": uuid.uuid4().hex[:12],
            "version": None,
            "status": "proposed",
            "patch": cleaned,
            "reason": str(reason or ""),
            "evidence": dict(evidence or {}),
            "created_at": time.time(),
            "decided_at": None,
        }
        with self._lock:
            self._conn.execute(
                "UPDATE prompt_versions SET status = 'discarded', decided_at = ? "
                "WHERE status = 'proposed'",
                (row["created_at"],),
            )
            self._conn.execute(
                """
                INSERT INTO prompt_versions
                    (id, version, status, patch_json, reason, evidence_json, created_at, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    None,
                    "proposed",
                    json.dumps(cleaned, ensure_ascii=False),
                    row["reason"],
                    json.dumps(row["evidence"], ensure_ascii=False),
                    row["created_at"],
                    None,
                ),
            )
            self._conn.commit()
        return row

    def accept(self, row_id: str) -> dict[str, Any] | None:
        """接受一条提议：当前生效版标 `superseded`，它成为新的 `active` 并分配版本号。"""
        with self._lock:
            row = self._get_locked(row_id)
            if row is None or row["status"] != "proposed":
                return None
            now = time.time()
            next_version = self._next_version_locked()
            self._conn.execute(
                "UPDATE prompt_versions SET status = 'superseded', decided_at = ? "
                "WHERE status = 'active'",
                (now,),
            )
            self._conn.execute(
                "UPDATE prompt_versions SET status = 'active', version = ?, decided_at = ? "
                "WHERE id = ?",
                (next_version, now, row_id),
            )
            self._conn.commit()
        return self.get(row_id)

    def reject(self, row_id: str) -> dict[str, Any] | None:
        """拒绝一条提议（留痕，不参与版本号）。"""
        with self._lock:
            row = self._get_locked(row_id)
            if row is None or row["status"] != "proposed":
                return None
            self._conn.execute(
                "UPDATE prompt_versions SET status = 'rejected', decided_at = ? WHERE id = ?",
                (time.time(), row_id),
            )
            self._conn.commit()
        return self.get(row_id)

    def rollback(self) -> dict[str, Any] | None:
        """回滚到上一个被取代的版本（版本号最高的 superseded）。没有可回滚的就返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_versions WHERE status = 'superseded' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            self._conn.execute(
                "UPDATE prompt_versions SET status = 'superseded', decided_at = ? "
                "WHERE status = 'active'",
                (now,),
            )
            self._conn.execute(
                "UPDATE prompt_versions SET status = 'active', decided_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self._conn.commit()
            return self._row_to_dict(row) | {"status": "active", "decided_at": now}

    # ---------------------------------------------------------------- 读

    def get(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_locked(row_id)
        return self._row_to_dict(row) if row else None

    def pending(self) -> dict[str, Any] | None:
        """当前待决定的那条建议（没有就是 None）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_versions WHERE status = 'proposed' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def current(self) -> dict[str, Any]:
        """当前生效的补丁（没有就是空 dict = 出厂行为）。"""
        row = self.current_row()
        return dict(row["patch"]) if row else {}

    def current_row(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_versions WHERE status = 'active' LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def current_version(self) -> int:
        """当前版本号（0 = 出厂，没有任何补丁）。"""
        row = self.current_row()
        return int(row["version"]) if row and row["version"] is not None else 0

    def can_rollback(self) -> bool:
        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM prompt_versions WHERE status = 'superseded'"
            ).fetchone()[0]
        return count > 0

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """全部版本记录，最新在前（含提议与被拒绝的，接受/拒绝都留痕）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM prompt_versions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ---------------------------------------------------------------- 内部

    def _get_locked(self, row_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM prompt_versions WHERE id = ?", (row_id,)
        ).fetchone()

    def _next_version_locked(self) -> int:
        value = self._conn.execute(
            "SELECT MAX(version) FROM prompt_versions WHERE version IS NOT NULL"
        ).fetchone()[0]
        return int(value or 0) + 1

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "version": row["version"],
            "status": row["status"],
            "patch": _loads(row["patch_json"], {}),
            "reason": row["reason"],
            "evidence": _loads(row["evidence_json"], {}),
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
        }


def _loads(text: str | None, fallback: Any) -> Any:
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback
