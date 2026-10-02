"""协议库的建表与**分域迁移**。

## 为什么不是一个 `PRAGMA user_version` 管全库

库里住着三类**独立演化**的东西：

| 域（scope） | 表 | 演化动力 |
|---|---|---|
| `protocol` | `executions` / `execution_events` | 随 Agent↔Stillroom 协议演化 |
| `registry` | `workflows` / `skills` | 随业务与工作流能力演化 |
| `bindings` | `request_bindings` | 幂等锚点，基本不动 |

全都往同一个整数上叠，任何一处迁移都会强迫另外两处"跟上版本号"，
迁移脚本立刻退化成 `if version >= 2 and table_x_has_column_y` 那种靠表结构反推的分支。
于是每个域在 `schema_meta` 里各存一份版本，**迁移决策按域判断**。

`PRAGMA user_version` **保留**为 `protocol` 域的兼容镜像（外部工具看它仍能判断协议版本），
同时它也是老库的**迁移起点**：v1 老库的 `user_version = 1` 会被读出来当 protocol 的当前版本，
于是只跑 1→2 那一步，不会重跑建表。

## 迁移步骤的写法

`_MIGRATIONS[scope][n]` = 「把该域从 n-1 升到 n」要执行的语句。
**只靠版本号推进，不做表结构探测** —— 版本表本身就是这件事的机制；
去查 `PRAGMA table_info` 兜底，等于承认版本表不可信。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from validator.errors import ErrorCode

from .errors import RepositoryError

SCOPE_PROTOCOL = "protocol"
SCOPE_REGISTRY = "registry"
SCOPE_BINDINGS = "bindings"

PROTOCOL_VERSION = 2
REGISTRY_VERSION = 1
BINDINGS_VERSION = 1

SCOPE_TARGETS: dict[str, int] = {
    SCOPE_PROTOCOL: PROTOCOL_VERSION,
    SCOPE_REGISTRY: REGISTRY_VERSION,
    SCOPE_BINDINGS: BINDINGS_VERSION,
}

_SCHEMA_META_DDL = """
    CREATE TABLE IF NOT EXISTS schema_meta (
        scope       TEXT PRIMARY KEY,
        version     INTEGER NOT NULL,
        migrated_at TEXT NOT NULL
    )
"""

# 各域的迁移步骤。键是"升到第几版"，值是达成它的语句序列。
_MIGRATIONS: dict[str, dict[int, tuple[str, ...]]] = {
    SCOPE_BINDINGS: {
        1: (
            """
            CREATE TABLE IF NOT EXISTS request_bindings (
                request_id   TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """,
        ),
    },
    SCOPE_PROTOCOL: {
        1: (
            """
            CREATE TABLE IF NOT EXISTS executions (
                execution_id        TEXT PRIMARY KEY,
                request_id          TEXT NOT NULL,
                workflow_id         TEXT NOT NULL,
                workflow_version    INTEGER NOT NULL,
                trust_level_at_creation TEXT,
                root_execution_id   TEXT NOT NULL,
                parent_execution_id TEXT,
                attempt             INTEGER NOT NULL DEFAULT 0,
                state               TEXT NOT NULL,
                input_snapshot      TEXT NOT NULL,
                input_hash          TEXT NOT NULL,
                output_ref          TEXT,
                error_code          TEXT,
                seq                 INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_executions_request ON executions(request_id)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_attempt
                ON executions(root_execution_id, attempt)
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_events (
                event_id      TEXT NOT NULL UNIQUE,
                execution_id  TEXT NOT NULL,
                seq           INTEGER NOT NULL,
                type          TEXT NOT NULL,
                status_before TEXT NOT NULL,
                status_after  TEXT NOT NULL,
                step          TEXT,
                error_code    TEXT,
                message       TEXT,
                payload_ref   TEXT,
                payload_hash  TEXT,
                at            TEXT NOT NULL,
                PRIMARY KEY (execution_id, seq)
            )
            """,
        ),
        # v2：把"这次执行跑的是哪一版定义"钉在 execution 上。
        # 只有版本号不足以复现 —— 版本号不等于内容。
        2: (
            "ALTER TABLE executions ADD COLUMN workflow_definition_hash TEXT",
            "ALTER TABLE executions ADD COLUMN workflow_definition_ref TEXT",
        ),
    },
    SCOPE_REGISTRY: {
        1: (
            """
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id     TEXT NOT NULL,
                version         INTEGER NOT NULL,
                definition_hash TEXT NOT NULL,
                definition_ref  TEXT NOT NULL,
                status          TEXT NOT NULL,
                trust_level     TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                activated_at    TEXT,
                PRIMARY KEY (workflow_id, version)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflows_active
                ON workflows(workflow_id, status)
            """,
            """
            CREATE TABLE IF NOT EXISTS skills (
                skill_id        TEXT NOT NULL,
                version         INTEGER NOT NULL,
                definition_hash TEXT NOT NULL,
                definition_ref  TEXT NOT NULL,
                status          TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                activated_at    TEXT,
                PRIMARY KEY (skill_id, version)
            )
            """,
        ),
    },
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开协议库连接。**连接配置只此一份**，避免两个域各配一套 PRAGMA。

    `isolation_level=None` = 关闭隐式事务，所有事务由调用方显式 `BEGIN`，
    这样"读写边界"才是代码说了算（见 `repository._read` / `_write`）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _seed_versions(conn: sqlite3.Connection) -> dict[str, int]:
    """老库的版本从**现实**推出来，而不是假设它是全新的。

    - `protocol`：读 `PRAGMA user_version`（老代码就是拿它当全库版本）
    - `bindings` / `registry`：表在就是 v1，不在就是 0（还没建）
    """
    tables = _existing_tables(conn)
    return {
        SCOPE_PROTOCOL: int(conn.execute("PRAGMA user_version").fetchone()[0]),
        SCOPE_BINDINGS: 1 if "request_bindings" in tables else 0,
        SCOPE_REGISTRY: 1 if "workflows" in tables else 0,
    }


def rollback_quietly(conn: sqlite3.Connection) -> None:
    """尽力回滚：回滚本身失败**绝不能顶掉真正的异常**。

    事务可能已经没了 —— 例如 SQL 触发了 `ON CONFLICT ROLLBACK`（它回滚整个事务，
    连 SAVEPOINT 一起），此时再 `ROLLBACK` 会抛
    `cannot rollback - no transaction is active`，把真正的原因盖掉。
    """
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # 连接已失效 / 事务已被外部回滚；原始异常更有价值


def ensure_schema(conn: sqlite3.Connection, now: str) -> dict[str, int]:
    """把库升到各域的目标版本，返回升级后的版本表。

    自己开事务：这是一次性的引导动作，调用方（`ExecutionRepository` /
    `WorkflowRegistry` 的 `__init__`）手上不会有外层事务。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_SCHEMA_META_DDL)
        stored = {
            row["scope"]: int(row["version"])
            for row in conn.execute("SELECT scope, version FROM schema_meta").fetchall()
        }
        seeds = _seed_versions(conn)

        versions: dict[str, int] = {}
        changed: list[str] = []
        for scope, target in SCOPE_TARGETS.items():
            current = stored.get(scope, seeds.get(scope, 0))
            if current > target:
                raise RepositoryError(
                    ErrorCode.SCHEMA_INVALID,
                    f"{scope} schema v{current} is newer than code v{target}",
                    scope=scope,
                    found=current,
                    supported=target,
                )
            for step in range(current + 1, target + 1):
                for statement in _MIGRATIONS[scope][step]:
                    conn.execute(statement)
                current = step
            versions[scope] = current
            # 只有**真的动过**（首次登记，或版本推进）才写回。
            # 每次打开都无条件 UPDATE 的话，`migrated_at` 的含义会退化成
            # "上次谁打开过库"，而它本来要回答的是"这个域上次迁移是什么时候"。
            if scope not in stored or stored[scope] < current:
                changed.append(scope)

        for scope in changed:
            conn.execute(
                "INSERT INTO schema_meta(scope, version, migrated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET version = excluded.version, "
                "migrated_at = excluded.migrated_at",
                (scope, versions[scope], now),
            )
        # protocol 域的兼容镜像
        conn.execute(f"PRAGMA user_version = {versions[SCOPE_PROTOCOL]}")
        conn.execute("COMMIT")
        return versions
    except BaseException:
        rollback_quietly(conn)
        raise


def read_versions(conn: sqlite3.Connection) -> dict[str, int]:
    """读各域当前版本（只读，不开写事务）。"""
    rows = conn.execute("SELECT scope, version FROM schema_meta").fetchall()
    return {row["scope"]: int(row["version"]) for row in rows}


__all__: list[Any] = [
    "SCOPE_PROTOCOL",
    "SCOPE_REGISTRY",
    "SCOPE_BINDINGS",
    "PROTOCOL_VERSION",
    "REGISTRY_VERSION",
    "BINDINGS_VERSION",
    "SCOPE_TARGETS",
    "connect",
    "ensure_schema",
    "read_versions",
    "rollback_quietly",
]
