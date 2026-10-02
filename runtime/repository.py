"""执行仓库（P1）—— 无头层的状态与事件持久化。

三条硬约束：

1. **事件只追加**（append-only）。当前状态是事件流重放的结果，
   `executions.state` 只是一份缓存列，任何时候都必须等于 `replay_state()`。
   所以没有「改状态但忘了记事件」这种可能，也没有删除或改写事件的 API。
2. **幂等靠数据库唯一索引**，不靠「先查后写」。并发抢同一个 `request_id` 时，
   只有一条能 INSERT 进 `request_bindings`；抢输的那条读到既有绑定并原样返回。
3. **不 import PySide6、不 import `app.*`**。数据库路径一律由调用方注入（见 `--db`）。

表结构（本文件只管**协议域**，建表与迁移在 `runtime/schema.py`）：

| 表 | 作用 |
|---|---|
| `request_bindings` | `request_id` → **首次**绑定的 `execution_id`。retry 不迁移它 —— 这是幂等的锚点 |
| `executions` | 一次执行的头部：workflow / 版本 / 定义指纹 / 信任级别 / input_snapshot / 当前状态 |
| `execution_events` | 追加式事件流，重放与审计的唯一依据 |

事务纪律（连接、锁、读写边界）继承自 `runtime/base.py::DomainDatabase` ——
`executions` 与 `workflows` 共用同一个物理库，但那套纪律只能有一份。

关于 retry：它新建一个 execution（同 `request_id`、带 `parent_execution_id`、
`attempt + 1`、**原样复制** `input_snapshot`），而 `request_bindings` 不动。
于是 `bind_request(同一个 request_id)` 永远返回**首次**那个 execution，
retry 出来的新执行只能通过 `retry()` 的返回值拿到 —— 这正是 v1.0 契约要的行为。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from validator.errors import ErrorCode
from validator.state_machine import (
    ExecutionState,
    apply,
    can_retry,
    coerce_state,
    is_terminal,
)

from .base import DomainDatabase
from .errors import RepositoryError
from .hashing import canonical_json as _dump
from .hashing import sha256_of_text as _hash
from .schema import PROTOCOL_VERSION

# 协议域（`executions` / `execution_events`）的版本。分域版本表见 `runtime/schema.py`，
# 这里保留这个名字是因为它是"协议族"的版本号，外部与测试都在看它。
SCHEMA_VERSION = PROTOCOL_VERSION
DEFAULT_DB_FILENAME = "protocol.db"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")

_COLUMNS = (
    "execution_id, request_id, workflow_id, workflow_version, trust_level_at_creation, "
    "workflow_definition_hash, workflow_definition_ref, "
    "root_execution_id, parent_execution_id, attempt, state, input_snapshot, "
    "input_hash, output_ref, error_code, seq, created_at, updated_at"
)

# 建表与迁移都在 `runtime/schema.py`（含 `schema_meta` 分域版本表）。
# 这里不再自带一份 DDL —— DDL 存两份必然漂，而且"哪个域升到第几版"只有那一处说了算。


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class ExecutionRecord:
    """`executions` 表的一行。"""

    execution_id: str
    request_id: str
    workflow_id: str
    workflow_version: int
    # 创建时的信任级**快照**，不是「当前信任级」。信任级可被 system 调高，
    # 但已存在的 execution 必须按发起时的档位判定，否则重放会得出不同结论。
    trust_level_at_creation: str | None
    # 这次执行**实际跑的是哪一版定义**（内容指纹 + 内容地址）。
    # 只有 `workflow_version` 不足以复现 —— 版本号不等于内容。
    workflow_definition_hash: str | None
    workflow_definition_ref: str | None
    root_execution_id: str
    parent_execution_id: str | None
    attempt: int
    state: str
    input_snapshot: dict[str, Any]
    input_hash: str
    output_ref: str | None
    error_code: str | None
    seq: int
    created_at: str
    updated_at: str

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    @property
    def is_retry(self) -> bool:
        return self.attempt > 0


@dataclass(frozen=True)
class EventRecord:
    """`execution_events` 表的一行，可直接导出成 schema 合法的事件文档。"""

    event_id: str
    execution_id: str
    request_id: str
    seq: int
    type: str
    status_before: str
    status_after: str
    at: str
    step: str | None = None
    error_code: str | None = None
    message: str | None = None
    payload_ref: str | None = None
    payload_hash: str | None = None

    def to_document(self) -> dict[str, Any]:
        """过 `execution-event.schema.json` 的形态（附加字段一个都不多）。"""
        doc: dict[str, Any] = {
            "schema_version": "1.0",
            "event_id": self.event_id,
            "execution_id": self.execution_id,
            "request_id": self.request_id,
            "seq": self.seq,
            "type": self.type,
            "at": self.at,
            "status_before": self.status_before,
            "status_after": self.status_after,
        }
        for key in ("step", "error_code", "message", "payload_ref", "payload_hash"):
            value = getattr(self, key)
            if value is not None:
                doc[key] = value
        return doc


@dataclass(frozen=True)
class BindResult:
    """`bind_request()` / `retry()` 的返回值。"""

    record: ExecutionRecord
    created: bool
    input_mismatch: bool = False
    """同一个 `request_id` 被喂了不同 input。契约要求返回既有 execution，
    所以这里不报错，只把事实带出来让上层处置（这是客户端 bug，不该静默）。"""

    @property
    def execution_id(self) -> str:
        return self.record.execution_id


@dataclass(frozen=True)
class ReplayOutcome:
    """事件流折叠出的**权威**结果。

    `executions` 的 `state` / `error_code` / `output_ref` 三列都只是它的缓存，
    由 `verify_consistency()` 逐一比对。缓存列没有任何独立写入通道 ——
    这正是「事件流是真相」在代码里的落点。
    """

    state: ExecutionState
    error_code: str | None = None
    output_ref: str | None = None


class ExecutionRepository(DomainDatabase):
    """SQLite 执行仓库。线程安全（单连接 + 可重入锁），足够 stdio 服务用。

    连接、锁、`_read` / `_write` 的快照边界都来自 `DomainDatabase`（见 `runtime/base.py`）——
    与 `WorkflowRegistry` 共用同一套事务纪律，因为那段代码是全项目最脆的地方，
    存两份 = 迟早只修好一份。
    """

    @property
    def schema_version(self) -> int:
        """**协议域**的版本（与 `PRAGMA user_version` 镜像一致）。

        要其它域（`registry` / `bindings`）的版本用继承来的 `schema_versions()`。
        """
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- 幂等入口 ---------------------------------------------------------

    def bind_request(
        self,
        *,
        request_id: str,
        workflow_id: str,
        workflow_version: int,
        input_snapshot: dict[str, Any] | None = None,
        trust_level: str | None = None,
        workflow_definition_hash: str | None = None,
        workflow_definition_ref: str | None = None,
    ) -> BindResult:
        """`execute_workflow` 的幂等锚点。

        返回 `created=True` 表示本次真的新建了一个 execution；
        `created=False` 表示命中既有绑定（重复调用），返回的永远是**首次**那一个。

        `trust_level` 是**发起时**的档位；落库后固化为 `trust_level_at_creation`
        （只读快照，retry 继承、不可被后续的信任级变更追溯影响）。

        `workflow_definition_hash` / `_ref` 是这次执行**实际跑的那一版定义**的内容指纹与
        内容地址，由调用方从 `WorkflowRegistry` 取到后传进来 —— 本仓库**刻意不持有
        registry 引用**（两者共用 `db_path` 但互不依赖，将来要物理分离只换路径）。
        钉住它是因为 `workflow_version` 只是版本号，版本号不等于内容：
        将来 registry 的 active 版本变了、或历史数据不干净，靠这两列仍能精确说
        "这个 execution 跑的是哪一版定义"。
        """
        self._check_request_id(request_id)
        snapshot = input_snapshot if input_snapshot is not None else {}
        snapshot_json = self._dump_snapshot(snapshot)
        snapshot_hash = _hash(snapshot_json)

        def op(conn: sqlite3.Connection) -> BindResult:
            execution_id = _new_id("exec")
            now = self._clock()
            try:
                conn.execute(
                    "INSERT INTO request_bindings(request_id, execution_id, created_at) "
                    "VALUES(?, ?, ?)",
                    (request_id, execution_id, now),
                )
            except sqlite3.IntegrityError:
                # 幂等命中：不是错误，读回既有绑定即可。
                #
                # 这里能继续用同一个事务去 SELECT，靠的是 SQLite 的默认冲突策略
                # `ON CONFLICT ABORT` —— 只回滚**出错的语句**，不回滚事务。
                # 这条依赖是**有意接受且被测试锁住的**
                # （tests/test_repository_semantics.py::test_idempotent_hit_keeps_the_transaction_usable）：
                # 本模块整体就是 sqlite3 专用（`sqlite3.Row` / `PRAGMA` / `protocol.db`），
                # 为"换个方言也能用"包 SAVEPOINT 是假收益 —— 实测 SQLite 的
                # `ON CONFLICT ROLLBACK` 会连 SAVEPOINT 一起回滚，`ROLLBACK TO` 直接报
                # `no such savepoint`，兜不住。真正要换库时，这段必须重写而不是加壳。
                row = conn.execute(
                    "SELECT execution_id FROM request_bindings WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                existing = self._execution_in(conn, row["execution_id"])
                return BindResult(
                    record=existing,
                    created=False,
                    input_mismatch=existing.input_hash != snapshot_hash,
                )

            conn.execute(
                f"INSERT INTO executions({_COLUMNS}) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution_id, request_id, workflow_id, int(workflow_version),
                    trust_level, workflow_definition_hash, workflow_definition_ref,
                    execution_id, None, 0,
                    ExecutionState.PENDING.value, snapshot_json, snapshot_hash,
                    None, None, 0, now, now,
                ),
            )
            return BindResult(record=self._execution_in(conn, execution_id), created=True)

        return self._write(op)

    def retry(
        self,
        execution_id: str,
        *,
        max_retries: int,
        budget_raised: bool = False,
    ) -> BindResult:
        """从终态发起一次重试。

        **没有 `input_snapshot` 参数，也没有任何 override 参数** —— 这是签名层面的保证：
        新 execution 严格复制原 `input_snapshot`，调用方无从篡改。
        `request_bindings` 保持不变，所以 `bind_request(request_id)` 仍返回首次那个。
        """

        def op(conn: sqlite3.Connection) -> BindResult:
            original = self._execution_in(conn, execution_id)

            root_row = conn.execute(
                "SELECT execution_id FROM executions WHERE execution_id = ?",
                (original.root_execution_id,),
            ).fetchone()
            if root_row is None:
                raise RepositoryError(
                    ErrorCode.ORIGINAL_NOT_FOUND,
                    "root execution of this retry chain is missing",
                    execution_id=execution_id,
                    root_execution_id=original.root_execution_id,
                )

            retries_used = int(
                conn.execute(
                    "SELECT COUNT(*) FROM executions WHERE root_execution_id = ? AND attempt > 0",
                    (original.root_execution_id,),
                ).fetchone()[0]
            )
            decision = can_retry(
                original.state,
                retries_used=retries_used,
                max_retries=max_retries,
                budget_raised=budget_raised,
            )
            if not decision.ok:
                raise RepositoryError(
                    decision.error_code or ErrorCode.NOT_TERMINAL,
                    decision.message,
                    execution_id=execution_id,
                    state=original.state,
                    retries_used=retries_used,
                    max_retries=max_retries,
                )

            new_id = _new_id("exec")
            now = self._clock()
            try:
                conn.execute(
                    f"INSERT INTO executions({_COLUMNS}) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id, original.request_id, original.workflow_id,
                        original.workflow_version, original.trust_level_at_creation,
                        # 定义指纹是**继承**的：retry 必须跑回它原来那一版定义，
                        # 哪怕 registry 里 active 已经换版 —— 否则"重试"就变成了"跑新定义"。
                        original.workflow_definition_hash, original.workflow_definition_ref,
                        original.root_execution_id, original.execution_id,
                        original.attempt + 1, ExecutionState.PENDING.value,
                        _dump(original.input_snapshot), original.input_hash,
                        None, None, 0, now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # UNIQUE(root_execution_id, attempt)：该 attempt 已被占。
                #
                # 这是**防御性分支**，不是并发 retry 的常规结局：写事务是
                # BEGIN IMMEDIATE，同一时刻只有一个连接持写锁，所以第二个 retry 会先
                # 阻塞、等赢家提交后再读 COUNT —— 那时它看到的是「次数已用掉」，
                # 在 can_retry 就被拒了（见 test_concurrent_retry_loser_goes_through_budget_check）。
                # 只有绕开本仓库的事务边界（自建连接直写、或库里已有同名 attempt 的历史行）
                # 才会走到这里。既然走到了，就不能报 RETRY_EXHAUSTED —— 那是另一回事。
                raise RepositoryError(
                    ErrorCode.RETRY_RACE_LOST,
                    "attempt slot already taken for this root execution",
                    execution_id=execution_id,
                    root_execution_id=original.root_execution_id,
                    attempt=original.attempt + 1,
                ) from exc
            return BindResult(record=self._execution_in(conn, new_id), created=True)

        return self._write(op)

    # -- 读 ---------------------------------------------------------------

    def get(self, execution_id: str) -> ExecutionRecord:
        """取单个 execution。

        虽然当前是单语句 SELECT（SQLite 单语句天然原子），仍然走 `_read()`：
        `_read` 在 `in_transaction` 时零开销复用外层事务，所以在
        `verify_consistency()` 这类嵌套调用里不花代价；而一旦将来这里长成
        多语句（加关联、拆两张表），快照边界自动就位，不会**静默**漏掉保护
        （漏掉的话现有测试是测不出来的——嵌套路径复用外层快照，掩盖了单读路径）。
        """
        return self._read(lambda conn: self._execution_in(conn, execution_id))

    def get_by_request(self, request_id: str) -> ExecutionRecord | None:
        """按 `request_id` 取**首次**绑定的 execution（不是最新的一次 retry）。"""

        def op(conn: sqlite3.Connection) -> ExecutionRecord | None:
            row = conn.execute(
                "SELECT execution_id FROM request_bindings WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            return self._execution_in(conn, row["execution_id"])

        return self._read(op)

    def list_attempts(self, request_id: str) -> tuple[ExecutionRecord, ...]:
        """同一 `request_id` 家族的全部执行，按 attempt 升序（首次在最前面）。"""

        def op(conn: sqlite3.Connection) -> tuple[ExecutionRecord, ...]:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM executions WHERE request_id = ? ORDER BY attempt ASC",
                (request_id,),
            ).fetchall()
            return tuple(self._row_to_execution(r) for r in rows)

        return self._read(op)

    def list_events(self, execution_id: str) -> tuple[EventRecord, ...]:
        def op(conn: sqlite3.Connection) -> tuple[EventRecord, ...]:
            record = self._execution_in(conn, execution_id)  # 存在性检查 + 取 request_id
            return self._events_in(conn, execution_id, record.request_id)

        return self._read(op)

    def replay_state(self, execution_id: str) -> str:
        """把事件流从头折叠一遍，返回重放得到的状态。

        任何一步对不上（事件在当前状态下不可达、记录的 before/after 与推导不符）
        都会抛 `INVALID_TRANSITION` —— 数据被改坏了必须炸出来，不能假装没事。
        """
        return self.replay(execution_id).state.value

    def replay(self, execution_id: str) -> ReplayOutcome:
        """重放的完整结果（`state` + `error_code` + `output_ref`）。

        `list_events()` 内部已做存在性检查，这里不再单独 `get()` 一遍 ——
        既省一次冗余查询，也避免把 `_read` 的重入绕一圈。
        """
        return self._read(
            lambda _conn: self._replay_in(execution_id, self.list_events(execution_id))
        )

    def verify_consistency(self, execution_id: str) -> ExecutionRecord:
        """自检：缓存列必须能被事件流完整解释。

        `executions` 的 `state` / `error_code` / `output_ref` 都只是缓存，
        事件流才是真相。三者任一与重放结果分叉（有人删了事件、改了 status、
        或绕过 API 写了缓存列），这里必须炸出来。

        整段自检在**一个读快照**里完成：这里刻意继续用 `get()` / `list_events()` /
        `replay_state()` 组合，靠 `_read` 的重入保护让嵌套调用落回同一事务 ——
        否则并发写入会让它把健康数据误报成损坏。
        """

        def op(_conn: sqlite3.Connection) -> ExecutionRecord:
            record = self.get(execution_id)
            events = self.list_events(execution_id)

            # 精确到「序号连续」：只比总数会漏掉中间被删、首尾看起来仍连续的情况。
            expected_seqs = list(range(1, record.seq + 1))
            actual_seqs = [event.seq for event in events]
            if actual_seqs != expected_seqs:
                raise RepositoryError(
                    ErrorCode.INVALID_TRANSITION,
                    f"cached seq={record.seq} but event seqs are {actual_seqs}",
                    execution_id=execution_id,
                )

            outcome = self._replay_in(execution_id, events)
            if outcome.state.value != record.state:
                raise RepositoryError(
                    ErrorCode.INVALID_TRANSITION,
                    f"cached state={record.state} but replay gives {outcome.state.value}",
                    execution_id=execution_id,
                )
            if outcome.error_code != record.error_code:
                raise RepositoryError(
                    ErrorCode.INVALID_TRANSITION,
                    f"cached error_code={record.error_code} but replay gives "
                    f"{outcome.error_code}",
                    execution_id=execution_id,
                )
            if outcome.output_ref != record.output_ref:
                raise RepositoryError(
                    ErrorCode.INVALID_TRANSITION,
                    f"cached output_ref={record.output_ref} but replay gives "
                    f"{outcome.output_ref}",
                    execution_id=execution_id,
                )
            return record

        return self._read(op)

    # -- 写：状态推进只能通过事件 -----------------------------------------

    def append_event(
        self,
        execution_id: str,
        event: str,
        *,
        step: str | None = None,
        error_code: str | None = None,
        message: str | None = None,
        payload_ref: str | None = None,
        payload_hash: str | None = None,
    ) -> EventRecord:
        """追加一个事件并推进状态。非法转移不落任何行，状态保持原样。

        事件名与 `validator.state_machine.ExecutionEvent` 严格对齐；
        未知事件名是编程错误，直接 `ValueError`（不是 `RepositoryError`）。

        **缓存列的写入规则**（与 `_replay_in()` 的折叠一一对应，改一边必须改另一边）：

        - `state` ← 本事件的 `apply()` 结果
        - `error_code` ← 本事件的 `error_code`；本事件不带就**保留原值**
          （即"最后一个非空值胜出"）。所以 `error_code` 是"最近一次出现的错误码"，
          不是"导致终止的那个错误码"—— schema 允许任意事件携带 `error_code`，
          仓库层不额外收紧，否则会造出 schema 与实现的第二种不一致。
        - `output_ref` ← **只有进入 `COMPLETED` 的事件**的 `payload_ref` 才算交付指针。
          其余终态（`FAILED` / `TIMEOUT` / `ABORTED` / `BUDGET_EXCEEDED`）没有交付语义，
          它们事件上的 `payload_ref` 是诊断产物，只留在事件行里，不上浮。
          中途步骤的 `payload_ref`（中间产物）同理不上浮。

        原先还有一个独立的 `output_ref` 入参，但它只写进 `executions` 列、不进事件流，
        于是那列无法从事件流重建（`verify_consistency()` 验不了它），
        与「事件流是真相」直接冲突 —— 现在交付指针**只有一个来源**：终态事件的 `payload_ref`。
        """

        def op(conn: sqlite3.Connection) -> EventRecord:
            record = self._execution_in(conn, execution_id)
            current = coerce_state(record.state)
            result = apply(current, event)
            if not result.ok:
                raise RepositoryError(
                    result.error_code or ErrorCode.INVALID_TRANSITION,
                    result.message,
                    execution_id=execution_id,
                    state=current.value,
                    event=str(event),
                )

            seq = record.seq + 1
            at = self._clock()
            event_id = _new_id("evt")
            conn.execute(
                "INSERT INTO execution_events("
                "event_id, execution_id, seq, type, status_before, status_after, step, "
                "error_code, message, payload_ref, payload_hash, at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, execution_id, seq, str(event), current.value,
                    result.state.value, step, error_code, message,
                    payload_ref, payload_hash, at,
                ),
            )
            # 交付指针只在**进入 COMPLETED** 时上浮。`FAILED` / `TIMEOUT` / `ABORTED` /
            # `BUDGET_EXCEEDED` 也是终态，但它们没有"交付"语义 —— 那些终态事件上的
            # `payload_ref`（比如 error_trace）是**诊断产物**，留在事件行里由
            # `list_events()` 取用，不混进 output_ref 这一列。
            delivered_ref = (
                payload_ref if result.state is ExecutionState.COMPLETED else None
            )
            conn.execute(
                "UPDATE executions SET state = ?, seq = ?, updated_at = ?, "
                "error_code = COALESCE(?, error_code), output_ref = COALESCE(?, output_ref) "
                "WHERE execution_id = ?",
                (result.state.value, seq, at, error_code, delivered_ref, execution_id),
            )
            return EventRecord(
                event_id=event_id,
                execution_id=execution_id,
                request_id=record.request_id,
                seq=seq,
                type=str(event),
                status_before=current.value,
                status_after=result.state.value,
                at=at,
                step=step,
                error_code=error_code,
                message=message,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
            )

        return self._write(op)

    # -- 内部 -------------------------------------------------------------
    # 连接、建表、`_read` / `_write` 快照边界、`_rollback_quietly` 都在基类
    # `DomainDatabase`（runtime/base.py）。下面只放本域自己的助手。

    def _check_request_id(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
            raise RepositoryError(
                ErrorCode.INPUT_SCHEMA_INVALID,
                "request_id must match ^[A-Za-z0-9_.:-]{1,128}$",
                request_id=request_id if isinstance(request_id, str) else type(request_id).__name__,
            )

    def _dump_snapshot(self, snapshot: dict[str, Any]) -> str:
        if not isinstance(snapshot, dict):
            raise RepositoryError(
                ErrorCode.INPUT_SCHEMA_INVALID,
                "input_snapshot must be a JSON object",
                got=type(snapshot).__name__,
            )
        try:
            return _dump(snapshot)
        except TypeError as exc:
            raise RepositoryError(
                ErrorCode.INPUT_SCHEMA_INVALID,
                f"input_snapshot is not JSON-serializable: {exc}",
            ) from exc

    def _execution_in(self, conn: sqlite3.Connection, execution_id: str) -> ExecutionRecord:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise RepositoryError(
                ErrorCode.EXECUTION_NOT_FOUND,
                "no such execution",
                execution_id=execution_id,
            )
        return self._row_to_execution(row)

    def _events_in(
        self,
        conn: sqlite3.Connection,
        execution_id: str,
        request_id: str,
    ) -> tuple[EventRecord, ...]:
        """`request_id` 不给默认值：它是 `EventRecord` 的必填字段，
        漏传应当直接 `TypeError`，而不是静默产出一批 `request_id=""` 的事件。"""
        rows = conn.execute(
            "SELECT * FROM execution_events WHERE execution_id = ? ORDER BY seq ASC",
            (execution_id,),
        ).fetchall()
        return tuple(self._row_to_event(r, request_id) for r in rows)

    @staticmethod
    def _replay_in(
        execution_id: str,
        events: tuple[EventRecord, ...],
    ) -> ReplayOutcome:
        """把事件流折叠成权威结果。`events` 必须来自同一读快照（见 `_read`）。

        三个缓存列的折叠规则与 `append_event()` 的写入严格同构，否则自检会自相矛盾：

        - `state`：逐个 `apply()`
        - `error_code`：**最后一个非空值胜出**（对应 `COALESCE(?, error_code)`）
        - `output_ref`：只有**达到终态**的那个事件携带的 `payload_ref` 才算交付指针
          （中途步骤的 `payload_ref` 是中间产物，留在事件行里，不上浮到 executions）
        """
        state = ExecutionState.PENDING
        error_code: str | None = None
        output_ref: str | None = None

        for event in events:
            result = apply(state, event.type)
            if not result.ok:
                raise RepositoryError(
                    result.error_code or ErrorCode.INVALID_TRANSITION,
                    f"event #{event.seq} ({event.type}) is not replayable from {state.value}",
                    execution_id=execution_id,
                    seq=event.seq,
                )
            if event.status_before != state.value:
                raise RepositoryError(
                    ErrorCode.INVALID_TRANSITION,
                    f"event #{event.seq} recorded status_before={event.status_before}, "
                    f"replay had {state.value}",
                    execution_id=execution_id,
                    seq=event.seq,
                )
            if event.status_after != result.state.value:
                raise RepositoryError(
                    ErrorCode.INVALID_TRANSITION,
                    f"event #{event.seq} recorded status_after={event.status_after}, "
                    f"replay gives {result.state.value}",
                    execution_id=execution_id,
                    seq=event.seq,
                )
            state = result.state
            if event.error_code is not None:
                error_code = event.error_code
            # 与 append_event 的写入同构：只有进入 COMPLETED 才认交付指针
            if event.payload_ref is not None and result.state is ExecutionState.COMPLETED:
                output_ref = event.payload_ref

        return ReplayOutcome(state=state, error_code=error_code, output_ref=output_ref)

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=row["execution_id"],
            request_id=row["request_id"],
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            trust_level_at_creation=row["trust_level_at_creation"],
            workflow_definition_hash=row["workflow_definition_hash"],
            workflow_definition_ref=row["workflow_definition_ref"],
            root_execution_id=row["root_execution_id"],
            parent_execution_id=row["parent_execution_id"],
            attempt=row["attempt"],
            state=row["state"],
            input_snapshot=json.loads(row["input_snapshot"]),
            input_hash=row["input_hash"],
            output_ref=row["output_ref"],
            error_code=row["error_code"],
            seq=row["seq"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row, request_id: str) -> EventRecord:
        """`request_id` 刻意不给默认值：`EventRecord.request_id` 是必填字段，
        漏传应当直接 `TypeError`，而不是静默产出一批 `request_id=""` 的事件。"""
        return EventRecord(
            event_id=row["event_id"],
            execution_id=row["execution_id"],
            request_id=request_id,
            seq=row["seq"],
            type=row["type"],
            status_before=row["status_before"],
            status_after=row["status_after"],
            at=row["at"],
            step=row["step"],
            error_code=row["error_code"],
            message=row["message"],
            payload_ref=row["payload_ref"],
            payload_hash=row["payload_hash"],
        )


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_DB_FILENAME",
    "ExecutionRepository",
    "ExecutionRecord",
    "EventRecord",
    "BindResult",
    "ReplayOutcome",
]
