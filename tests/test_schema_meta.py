"""协议库的**分域** schema 版本与迁移。

这个文件锁住的是"一个物理库、多个逻辑域"这个结构本身。它要能回答三个问题：

1. 各域的版本**互不牵连** —— 升协议域不该动注册域的版本号；
2. 老库（只有 `PRAGMA user_version`、没有 `schema_meta`）能**就地升级**，不重建；
3. 迁移**只跑一次** —— 版本表是这件事的唯一机制，不靠 `PRAGMA table_info` 反推。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from runtime import (
    BINDINGS_VERSION,
    PROTOCOL_VERSION,
    REGISTRY_VERSION,
    SCOPE_BINDINGS,
    SCOPE_PROTOCOL,
    SCOPE_REGISTRY,
    ExecutionRepository,
    RepositoryError,
    WorkflowRegistry,
)
from validator.errors import ErrorCode

# `runtime/schema.py` 里 v1 的建表语句（不加那两列定义指纹）。
# 手抄一份是有意的：这段是**历史**，不该跟着实现漂 —— 它模拟的是磁盘上已经存在的旧库。
_V1_PROTOCOL_DDL = (
    """
    CREATE TABLE executions (
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
    "CREATE INDEX idx_executions_request ON executions(request_id)",
    """
    CREATE UNIQUE INDEX idx_executions_attempt
        ON executions(root_execution_id, attempt)
    """,
    """
    CREATE TABLE execution_events (
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
)

_V1_BINDINGS_DDL = """
    CREATE TABLE request_bindings (
        request_id   TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )
"""


def _make_v1_database(path: Path) -> None:
    """造一个"老版本"的库：三张表都在，`user_version = 1`，**没有** `schema_meta`。"""
    conn = sqlite3.connect(str(path))
    try:
        for ddl in _V1_PROTOCOL_DDL:
            conn.execute(ddl)
        conn.execute(_V1_BINDINGS_DDL)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _read_meta(path: Path) -> dict[str, tuple[int, str]]:
    """`scope → (version, migrated_at)`。"""
    conn = sqlite3.connect(str(path))
    try:
        return {
            row[0]: (int(row[1]), row[2])
            for row in conn.execute("SELECT scope, version, migrated_at FROM schema_meta")
        }
    finally:
        conn.close()


def _fixed_clock(stamp: str):
    """时钟注入：让 `migrated_at` 的断言与真实时间无关，可复现。"""
    return lambda: stamp


def _rewind_protocol_to_v1(path: Path) -> None:
    """把一个 v2 库的**协议域**倒回 v1 的形状，其余域照旧。

    用 `ALTER TABLE ... DROP COLUMN`（SQLite ≥ 3.35）摘掉 v2 加的两列，
    再把版本表里 protocol 那行删掉、`user_version` 设回 1 ——
    于是下次打开时 `_seed_versions()` 会从 `user_version` 读出 1，只跑 1→2 那一步。

    这是造"只有一个域需要迁移"的干净办法：不必迁就无关域一起升。
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("ALTER TABLE executions DROP COLUMN workflow_definition_hash")
        conn.execute("ALTER TABLE executions DROP COLUMN workflow_definition_ref")
        conn.execute("DELETE FROM schema_meta WHERE scope = ?", (SCOPE_PROTOCOL,))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 一、各域版本独立
# ---------------------------------------------------------------------------

def test_a_fresh_database_carries_one_version_per_scope(tmp_path: Path) -> None:
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db) as repo:
        versions = repo.schema_versions()

    assert versions == {
        SCOPE_PROTOCOL: PROTOCOL_VERSION,
        SCOPE_BINDINGS: BINDINGS_VERSION,
        # 协议域自己会把 registry 的表一起建出来（同一个物理库），
        # 但那是"建表"，不是"注册域被协议域升了版本"。
        SCOPE_REGISTRY: REGISTRY_VERSION,
    }


def test_user_version_mirrors_the_protocol_scope(tmp_path: Path) -> None:
    """`PRAGMA user_version` 是协议域的兼容镜像 —— 外部工具仍能靠它判断协议版本。

    **两个来源要独立读**：`user_version` 是 SQLite 自己的头字段，`schema_meta` 是我们的表。
    若只断言 `repo.schema_version == repo.schema_versions()`，两边可能来自同一条写路径
    （都读 `user_version`，或都读 `schema_meta`），镜像不变式其实没被验证。
    所以这里从**另一条裸连接**读 `schema_meta`。
    """
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db) as repo:
        mirror = repo._conn.execute("PRAGMA user_version").fetchone()[0]
        assert mirror == repo.schema_version

    assert mirror == _read_meta(db)[SCOPE_PROTOCOL][0]


def test_the_mirror_still_holds_after_a_migration(tmp_path: Path) -> None:
    """迁移路径上也要同步 —— 只在新库上相等，等于没锁。

    v1 → v2 迁移会重写 `schema_meta` 里的 protocol 行与 `user_version`，
    这两处必须在**同一个事务**里一起动（它们本来就是同一个 `BEGIN IMMEDIATE` 写的）。
    """
    db = tmp_path / "protocol.db"
    _make_v1_database(db)

    with ExecutionRepository(db) as repo:
        assert repo.schema_version == PROTOCOL_VERSION
        mirror = repo._conn.execute("PRAGMA user_version").fetchone()[0]

    assert mirror == PROTOCOL_VERSION
    assert _read_meta(db)[SCOPE_PROTOCOL][0] == PROTOCOL_VERSION


def test_a_scope_can_migrate_without_touching_the_others(tmp_path: Path) -> None:
    """**只**需要升注册域的库，打开后只有注册域动版本；协议域与幂等域原地不动。

    这条是"分域"这个设计的价值锚点：版本号若能独立推进，就说明迁移决策真是按域判断的，
    而不是"一个整数管全库、谁动都得跟着算"。造法是模拟一个"注册域还没建表"的库 ——
    把 registry 的两张表删掉、把它在版本表里的行也删掉，其余照旧。

    `migrated_at` 也要断言不变：光看版本号相同，不足以排除"其实重跑了一遍建表"。
    """
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db):
        pass

    before = _read_meta(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE skills")
        conn.execute("DROP TABLE workflows")
        conn.execute("DELETE FROM schema_meta WHERE scope = ?", (SCOPE_REGISTRY,))
        conn.commit()
    finally:
        conn.close()

    with ExecutionRepository(db) as repo:
        versions = repo.schema_versions()

    assert versions[SCOPE_REGISTRY] == REGISTRY_VERSION      # 只有它被升
    assert versions[SCOPE_PROTOCOL] == PROTOCOL_VERSION
    assert versions[SCOPE_BINDINGS] == BINDINGS_VERSION

    after = _read_meta(db)
    assert after[SCOPE_PROTOCOL] == before[SCOPE_PROTOCOL]   # 含 migrated_at
    assert after[SCOPE_BINDINGS] == before[SCOPE_BINDINGS]
    # 注册域的表回来了
    assert {"workflows", "skills"} <= _table_names(db)


def test_migrating_the_protocol_scope_leaves_other_timestamps_alone(tmp_path: Path) -> None:
    """反方向同样要成立：升**协议域**，注册域与幂等域的时间戳不许动。

    造法：正常开一次库拿到 registry/bindings 的登记，然后把协议域"倒回 v1" ——
    用 `ALTER TABLE ... DROP COLUMN`（SQLite ≥ 3.35）把定义指纹两列摘掉、
    删掉它在版本表里的行、把 `user_version` 设回 1。这样再打开时**只有协议域需要迁移**。

    只在一个方向断言（升注册域不动协议域）是不够的：那可能是"恰好注册域不动"，
    换个方向就露馅。
    """
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db, clock=_fixed_clock("2026-01-01T00:00:00Z")):
        pass
    before = _read_meta(db)

    _rewind_protocol_to_v1(db)

    with ExecutionRepository(db, clock=_fixed_clock("2026-01-02T00:00:00Z")) as repo:
        assert repo.schema_version == PROTOCOL_VERSION

    after = _read_meta(db)
    assert after[SCOPE_PROTOCOL] == (PROTOCOL_VERSION, "2026-01-02T00:00:00Z")  # 真的迁移过
    assert after[SCOPE_REGISTRY] == before[SCOPE_REGISTRY]     # 这两个纹丝不动
    assert after[SCOPE_BINDINGS] == before[SCOPE_BINDINGS]
    # 摘掉的两列回来了
    assert {"workflow_definition_hash", "workflow_definition_ref"} <= _columns(db, "executions")


def test_migrated_at_advances_when_a_scope_actually_migrates(tmp_path: Path) -> None:
    """正方向的**第一种**路径：**首次登记**（版本表里还没有这个域的行）→ INSERT。

    时钟显式注入，避免两个断言落在同一个微秒上而偶发失败。
    """
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db, clock=_fixed_clock("2026-01-01T00:00:00Z")):
        pass
    assert _read_meta(db)[SCOPE_REGISTRY] == (REGISTRY_VERSION, "2026-01-01T00:00:00Z")

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE skills")
        conn.execute("DROP TABLE workflows")
        conn.execute("DELETE FROM schema_meta WHERE scope = ?", (SCOPE_REGISTRY,))
        conn.commit()
    finally:
        conn.close()

    with ExecutionRepository(db, clock=_fixed_clock("2026-01-02T00:00:00Z")):
        pass

    assert _read_meta(db)[SCOPE_REGISTRY] == (REGISTRY_VERSION, "2026-01-02T00:00:00Z")


def test_migrated_at_advances_when_a_recorded_scope_bumps_version(tmp_path: Path) -> None:
    """正方向的**第二种**路径，也是现实中唯一会走 UPDATE 的那种：**行还在、版本往前走**。

    场景就是"代码升级 bump 了某个域的版本"：老库里已有 `registry v1` 那一行，
    新代码目标 v2 → 跑 1→2 的迁移 → 那一行**已存在**，必须 UPDATE 而不是 INSERT。

    这条用例是补出来的：原先只有上面的"首次登记"那条，而 `INSERT OR IGNORE`
    这种"永不写回"的退化写法在它上面**照样全绿**（反证时发现的）。
    只锁 INSERT 路径，等于没锁住真正要紧的那条。
    """
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db, clock=_fixed_clock("2026-01-01T00:00:00Z")):
        pass

    # 把注册域"倒回未升级"的状态：**保留版本表的行**，只把版本号退回 0。
    # 迁移语句是 `CREATE TABLE IF NOT EXISTS`，表已在也不会炸 —— 正好模拟真实的版本推进。
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE schema_meta SET version = 0, migrated_at = ? WHERE scope = ?",
            ("2025-01-01T00:00:00Z", SCOPE_REGISTRY),
        )
        conn.commit()
    finally:
        conn.close()

    with ExecutionRepository(db, clock=_fixed_clock("2026-01-02T00:00:00Z")):
        pass

    assert _read_meta(db)[SCOPE_REGISTRY] == (REGISTRY_VERSION, "2026-01-02T00:00:00Z")
    # 别的域没有被顺带刷新
    assert _read_meta(db)[SCOPE_PROTOCOL] == (PROTOCOL_VERSION, "2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# 二、老库就地升级
# ---------------------------------------------------------------------------

def test_v1_database_migrates_in_place_without_losing_rows(tmp_path: Path) -> None:
    """v1 老库打开即升到 v2：补上定义指纹两列，**已有数据不动**。

    同时确认起点是 `user_version` 而不是"假设全新" —— 否则会重跑 v1 的建表，
    这里就会撞 `table executions already exists`。
    """
    db = tmp_path / "protocol.db"
    _make_v1_database(db)

    # 老库里放一行历史数据（执行行 + 幂等绑定行，都是 v1 的形状：没有定义指纹两列）
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO executions("
            "execution_id, request_id, workflow_id, workflow_version, "
            "trust_level_at_creation, root_execution_id, parent_execution_id, attempt, "
            "state, input_snapshot, input_hash, output_ref, error_code, seq, "
            "created_at, updated_at) "
            "VALUES('exec_old', 'req_old', 'minimal_job', 1, 'T2', 'exec_old', NULL, 0, "
            "'PENDING', '{}', 'hash_old', NULL, NULL, 0, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO request_bindings(request_id, execution_id, created_at) "
            "VALUES('req_old', 'exec_old', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    with ExecutionRepository(db) as repo:
        assert repo.schema_version == PROTOCOL_VERSION
        binding = repo.get_by_request("req_old")
        assert binding is not None and binding.execution_id == "exec_old"
        # 老行没有定义指纹，读出来是 None 而不是报错 —— 迁移只加列、不改数据
        assert binding.workflow_definition_hash is None
        assert binding.input_hash == "hash_old"

    columns = _columns(db, "executions")
    assert {"workflow_definition_hash", "workflow_definition_ref"} <= columns


def test_opening_twice_does_not_rerun_migrations(tmp_path: Path) -> None:
    """迁移只跑一次 —— 由版本表决定，不靠表结构探测。

    若版本门失效、v2 的 `ALTER TABLE ADD COLUMN` 被重跑，
    SQLite 会抛 `duplicate column name` —— 所以"能开第二次"本身就是证据。
    """
    db = tmp_path / "protocol.db"
    for _ in range(3):
        with ExecutionRepository(db) as repo:
            assert repo.schema_version == PROTOCOL_VERSION
            assert repo.schema_versions()[SCOPE_BINDINGS] == BINDINGS_VERSION


def test_registry_shares_the_same_database_and_migration_state(tmp_path: Path) -> None:
    """两个域类打开同一个文件时看到**同一份**版本表。"""
    db = tmp_path / "protocol.db"
    ExecutionRepository(db).close()

    with WorkflowRegistry(db) as registry:
        versions = registry.schema_versions()
        assert versions[SCOPE_PROTOCOL] == PROTOCOL_VERSION
        assert versions[SCOPE_BINDINGS] == BINDINGS_VERSION
        assert versions[SCOPE_REGISTRY] == REGISTRY_VERSION


# ---------------------------------------------------------------------------
# 三、更新的库要被拒
# ---------------------------------------------------------------------------

def test_database_newer_than_code_is_refused_per_scope(tmp_path: Path) -> None:
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db):
        pass

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE schema_meta SET version = ? WHERE scope = ?",
            (PROTOCOL_VERSION + 3, SCOPE_PROTOCOL),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RepositoryError) as exc:
        ExecutionRepository(db)
    assert exc.value.code == ErrorCode.SCHEMA_INVALID.value
    assert SCOPE_PROTOCOL in str(exc.value)


def test_legacy_user_version_newer_than_code_is_refused(tmp_path: Path) -> None:
    """没有任何 `schema_meta` 的老库，起点只能取 `user_version` —— 它太新也要拒。"""
    db = tmp_path / "protocol.db"
    _make_v1_database(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(f"PRAGMA user_version = {PROTOCOL_VERSION + 5}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RepositoryError) as exc:
        ExecutionRepository(db)
    assert exc.value.code == ErrorCode.SCHEMA_INVALID.value


def test_a_rejected_migration_leaves_no_schema_meta_behind(tmp_path: Path) -> None:
    """迁移失败必须整体回滚：不留下"版本表已建、表却没升"的半成品状态。"""
    db = tmp_path / "protocol.db"
    _make_v1_database(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(f"PRAGMA user_version = {PROTOCOL_VERSION + 5}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RepositoryError):
        ExecutionRepository(db)

    assert "schema_meta" not in _table_names(db)
