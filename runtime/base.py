"""协议库上「一个逻辑域」的共用骨架。

`ExecutionRepository`（协议域）与 `WorkflowRegistry`（注册域）共用同一个物理库、
但各开自己的连接、各有自己的类。它们**必须**共享同一套事务纪律，而这段纪律
是全项目最脆的地方（快照边界、回滚不许吞掉根因、写者串行化）。
让它存在两份 = 迟早只修好一份。

所以这里只做一件事：把连接、锁、读写边界一次性定义清楚，两个域继承它。

## 读写边界（继承者必须遵守）

- **写**一律 `BEGIN IMMEDIATE`（拿写锁，跨连接串行化写者）。
- **读**一律走 `_read()`：持锁 + `BEGIN DEFERRED`，`in_transaction` 时**零开销复用**
  外层事务。多语句读必须落在同一个快照上，否则「先读 A 再读 B」会看到两个时点，
  并发写入会被自检误报成数据损坏。
- 单语句读**也**走 `_read()`：这样"公共读者不直连 `self._conn`"是一条统一规则，
  不靠每个方法的作者记得自己是单语句。由源码扫描测试锁死。

## 顺带纠一个常见假设

共用同一个 `db_path` **不等于**共用同一个连接，所以两个域之间**没有跨域事务**。
这是有意的：工作流定义是长期资产，"注册成功但这次执行失败"正是期望结果，
不该被一起回滚掉。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from .schema import connect, ensure_schema, read_versions, rollback_quietly


def utc_now() -> str:
    """ISO-8601 UTC，带 Z 后缀（`execution-event.schema.json` 的 date-time 口味）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DomainDatabase:
    """协议库上某一个逻辑域的基类。子类只关心自己的表与语义。"""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], str] | None = None) -> None:
        self._path = Path(db_path)
        self._clock = clock or utc_now
        self._lock = threading.RLock()
        self._conn = connect(self._path)
        self._ensure_schema()

    # -- 生命周期 ---------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self._path

    def schema_versions(self) -> dict[str, int]:
        """各逻辑域各自的 schema 版本（见 `runtime/schema.py`）。"""
        with self._lock:
            return read_versions(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 事务边界 ---------------------------------------------------------

    def _write(self, op: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = op(self._conn)
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                rollback_quietly(self._conn)
                raise

    def _read(self, op: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            if self._conn.in_transaction:
                # 已被外层事务包住（RLock 重入）：直接复用，天然就是同一个快照。
                return op(self._conn)
            self._conn.execute("BEGIN DEFERRED")
            try:
                result = op(self._conn)
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                rollback_quietly(self._conn)
                raise

    def _ensure_schema(self) -> None:
        with self._lock:
            ensure_schema(self._conn, self._clock())


__all__ = ["DomainDatabase", "utc_now"]
