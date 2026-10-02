"""缓存列语义 + 竞态错误码 + 序号连续性（P1 实现审查的收口测试）。

三组断言，各自对应审查里提出的一个"隐式行为"：

1. `executions.error_code` / `output_ref` 是**事件流的受校验缓存**，
   折叠规则写死在 `contracts/execution-state-machine.md` §四之二：
   `error_code` 最后一个非空值胜出；`output_ref` 只认终态事件的 `payload_ref`。
2. `RETRY_EXHAUSTED`（次数用尽）与 `RETRY_RACE_LOST`（attempt 槽位被占）是两件事。
   并且要证明：**真正并发时后者不会出现**，因为 `BEGIN IMMEDIATE` 已把写者串行化。
3. 事件序号必须连续 —— 只比总数会漏掉"中间被删、首尾仍连续"的改坏。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from runtime import ExecutionRepository
from runtime.errors import RepositoryError
from validator.errors import ErrorCode
from validator.state_machine import ExecutionState

SIX_STEPS = ("understand", "reference", "generate", "evaluate", "refine", "deliver")


def _bind(repo: ExecutionRepository, request_id: str) -> str:
    return repo.bind_request(
        request_id=request_id,
        workflow_id="article_generation",
        workflow_version=1,
        input_snapshot={"prompt": "画一只猫"},
    ).execution_id


def _fail(repo: ExecutionRepository, execution_id: str, code: str = "BUDGET_EXCEEDED") -> None:
    repo.append_event(execution_id, "engine_started")
    repo.append_event(execution_id, "engine_failed", error_code=code)


# ---------------------------------------------------------------------------
# 一、缓存列的折叠规则
# ---------------------------------------------------------------------------

def test_error_code_is_last_non_null_wins(tmp_path: Path) -> None:
    """`error_code` 是"最近一次出现的错误码"：后来的非空值覆盖，空值不覆盖。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        execution_id = _bind(repo, "req_err")
        repo.append_event(execution_id, "engine_started")

        # 非终态事件也能带 error_code（schema 允许），它同样进缓存
        repo.append_event(
            execution_id, "engine_step_ended", step="generate",
            error_code=ErrorCode.SEMANTIC_INVALID.value,
        )
        assert repo.get(execution_id).error_code == ErrorCode.SEMANTIC_INVALID.value

        # 后来的非空值覆盖前一个
        repo.append_event(execution_id, "engine_failed", error_code=ErrorCode.BUDGET_EXCEEDED.value)
        assert repo.get(execution_id).error_code == ErrorCode.BUDGET_EXCEEDED.value

        # 缓存一致 —— 这里是"最后一个非空值胜出"能被验回去的证据
        assert repo.replay(execution_id).error_code == ErrorCode.BUDGET_EXCEEDED.value
        repo.verify_consistency(execution_id)
    finally:
        repo.close()


def test_error_code_survives_a_later_event_without_one(tmp_path: Path) -> None:
    """空值不覆盖：中途记了错，之后跑成功也仍看得到那个码。

    这是**有意的**：`error_code` 表示"最近一次出现的错误码"，不是
    "导致终止的错误码"。若要把语义改成后者，得连同 `_replay_in()` 一起改。
    """
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        execution_id = _bind(repo, "req_keep_code")
        repo.append_event(execution_id, "engine_started")
        repo.append_event(
            execution_id, "engine_step_ended", step="evaluate",
            error_code=ErrorCode.MATCH_TIMEOUT.value,
        )
        assert repo.get(execution_id).error_code == ErrorCode.MATCH_TIMEOUT.value

        repo.append_event(execution_id, "engine_completed", payload_ref="artifacts/final.json")
        record = repo.get(execution_id)
        assert record.state == ExecutionState.COMPLETED.value
        assert record.error_code == ErrorCode.MATCH_TIMEOUT.value  # 没被清掉
        repo.verify_consistency(execution_id)
    finally:
        repo.close()


def test_output_ref_comes_only_from_the_terminal_event(tmp_path: Path) -> None:
    """中途步骤的 `payload_ref` 是中间产物，不上浮成交付指针。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        execution_id = _bind(repo, "req_ref")
        repo.append_event(execution_id, "engine_started")
        for step in SIX_STEPS[:-1]:
            repo.append_event(
                execution_id, "engine_step_ended", step=step,
                payload_ref=f"artifacts/{step}.json",
            )
        # 还没到终态 → 交付指针必须仍为空
        assert repo.get(execution_id).output_ref is None

        repo.append_event(
            execution_id, "engine_completed", step="deliver",
            payload_ref="artifacts/delivered.json",
        )
        assert repo.get(execution_id).output_ref == "artifacts/delivered.json"
        repo.verify_consistency(execution_id)
    finally:
        repo.close()


def test_tampering_with_a_cached_column_is_detected(tmp_path: Path) -> None:
    """三个缓存列逐个改坏，`verify_consistency()` 都必须炸出来。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        execution_id = _bind(repo, "req_tamper")
        repo.append_event(execution_id, "engine_started")
        repo.append_event(
            execution_id, "engine_failed",
            error_code=ErrorCode.BUDGET_EXCEEDED.value,
            payload_ref="artifacts/failed.json",
        )
        repo.verify_consistency(execution_id)
        good = repo.get(execution_id)

        for column, bad in (
            ("state", ExecutionState.COMPLETED.value),
            ("error_code", ErrorCode.SEMANTIC_INVALID.value),
            ("output_ref", "artifacts/forged.json"),
        ):
            repo._conn.execute(
                f"UPDATE executions SET {column} = ? WHERE execution_id = ?",
                (bad, execution_id),
            )
            repo._conn.commit()
            with pytest.raises(RepositoryError) as exc:
                repo.verify_consistency(execution_id)
            assert exc.value.code == ErrorCode.INVALID_TRANSITION.value
            assert column in str(exc.value)
            # 复原，继续验下一列
            repo._conn.execute(
                f"UPDATE executions SET {column} = ? WHERE execution_id = ?",
                (getattr(good, column), execution_id),
            )
            repo._conn.commit()
            repo.verify_consistency(execution_id)
    finally:
        repo.close()


def test_output_ref_ignores_diagnostic_payloads_of_failed_states(tmp_path: Path) -> None:
    """`FAILED` 等终态的 `payload_ref` 是诊断产物，不得混进交付指针。

    契约 §四之二：只有进入 `COMPLETED` 的事件才算交付。若把诊断产物也上浮，
    Replay 就分不清"这个执行交付了什么"和"它留下了什么现场"。
    诊断产物没丢 —— 它仍在事件行里，`list_events()` 取得到。
    """
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        cases = (
            ("failed", ["engine_started", "engine_failed"], ExecutionState.FAILED),
            ("timeout", ["engine_started", "engine_timeout"], ExecutionState.TIMEOUT),
            ("aborted", ["abort_requested"], ExecutionState.ABORTED),
            ("budget", ["engine_started", "budget_exceeded"], ExecutionState.BUDGET_EXCEEDED),
        )
        for label, trail, expected in cases:
            execution_id = _bind(repo, f"req_diag_{label}")
            for event in trail[:-1]:
                repo.append_event(execution_id, event)
            trace = f"artifacts/{label}_trace.json"
            repo.append_event(execution_id, trail[-1], payload_ref=trace)

            record = repo.get(execution_id)
            assert record.state == expected.value
            assert record.output_ref is None, f"{expected.value} 不该有交付指针"
            repo.verify_consistency(execution_id)

            # 诊断产物没丢，仍在事件行里
            carried = [e.payload_ref for e in repo.list_events(execution_id) if e.payload_ref]
            assert carried == [trace]

        # 反面对照：只有 COMPLETED 上浮
        done = _bind(repo, "req_diag_done")
        repo.append_event(done, "engine_started")
        repo.append_event(done, "engine_completed", payload_ref="artifacts/delivered.json")
        assert repo.get(done).output_ref == "artifacts/delivered.json"
        repo.verify_consistency(done)
    finally:
        repo.close()


def test_public_readers_never_bypass_the_snapshot_boundary() -> None:
    """架构不变量：公共读方法一律经 `_read()`，不直连 `self._conn`。

    这条规则必须被锁住，否则将来某个读者从单语句长成多语句时会**静默**丢掉快照保护 ——
    嵌套路径（`verify_consistency` 内部）复用外层快照，现有测试根本测不出来。
    """
    import inspect

    # 真正执行读取、必须自带快照边界的方法
    direct = (
        "get", "get_by_request", "list_attempts",
        "list_events", "replay", "verify_consistency",
    )
    for name in direct:
        source = inspect.getsource(getattr(ExecutionRepository, name))
        assert "self._read(" in source, f"{name}() 没有走 _read() 快照边界"
        assert "self._conn.execute(" not in source, f"{name}() 直连了 self._conn"

    # 薄封装：委托给上面某个读者，因此不得自己碰连接
    wrappers = {"replay_state": "self.replay("}
    for name, delegate in wrappers.items():
        source = inspect.getsource(getattr(ExecutionRepository, name))
        assert delegate in source, f"{name}() 应委托给 {delegate}"
        assert "self._conn.execute(" not in source, f"{name}() 直连了 self._conn"


def test_read_snapshot_is_reentrant_and_leaves_no_transaction(tmp_path: Path) -> None:
    """`_read()` 嵌套时复用外层事务；最外层调用后不残留事务。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        execution_id = _bind(repo, "req_nested")
        repo.append_event(execution_id, "engine_started")

        repo.get(execution_id)
        assert repo._conn.in_transaction is False
        repo.list_events(execution_id)
        assert repo._conn.in_transaction is False
        repo.verify_consistency(execution_id)   # 内部多层嵌套
        assert repo._conn.in_transaction is False
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 二、序号连续性
# ---------------------------------------------------------------------------

def test_event_seq_no_gap(tmp_path: Path) -> None:
    """把中间一条事件删掉、同时把 seq 计数改小 —— 只比总数会漏，比序号连续性不会。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        execution_id = _bind(repo, "req_gap")
        repo.append_event(execution_id, "engine_started")
        for step in SIX_STEPS:
            repo.append_event(execution_id, "engine_step_ended", step=step)
        assert repo.get(execution_id).seq == 7

        # 删掉第 3 条，并把缓存 seq 同步改成 6（总数就又"对得上"了）
        repo._conn.execute(
            "DELETE FROM execution_events WHERE execution_id = ? AND seq = ?",
            (execution_id, 3),
        )
        repo._conn.execute(
            "UPDATE executions SET seq = 6 WHERE execution_id = ?", (execution_id,)
        )
        repo._conn.commit()
        assert len(repo.list_events(execution_id)) == repo.get(execution_id).seq  # 总数假相符

        with pytest.raises(RepositoryError) as exc:
            repo.verify_consistency(execution_id)
        assert exc.value.code == ErrorCode.INVALID_TRANSITION.value
        assert "seq" in str(exc.value)
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 三、retry 的两种拒绝
# ---------------------------------------------------------------------------

def test_retry_race_lost_is_reported_as_such(tmp_path: Path) -> None:
    """attempt 槽位被占 → `RETRY_RACE_LOST`，不能报成"次数用尽"。

    构造手法：先用高 `max_retries` 占掉 `attempt=1`，再对**原 execution** 发起 retry ——
    它的下一个 attempt 恰好也是 1，于是撞 `UNIQUE(root_execution_id, attempt)`。
    这与"两个连接同时抢同一个槽位"落到的是同一个约束。
    """
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        first = _bind(repo, "req_race")
        _fail(repo, first)
        occupied = repo.retry(first, max_retries=5)
        assert occupied.record.attempt == 1

        # 次数没耗尽（1 < 5），所以能过 can_retry，撞在 UNIQUE 上
        with pytest.raises(RepositoryError) as exc:
            repo.retry(first, max_retries=5)
        assert exc.value.code == ErrorCode.RETRY_RACE_LOST.value
        assert exc.value.code != ErrorCode.RETRY_EXHAUSTED.value
        assert [r.attempt for r in repo.list_attempts("req_race")] == [0, 1]
    finally:
        repo.close()


def test_retry_exhaustion_still_reports_exhausted(tmp_path: Path) -> None:
    """次数判定与竞态判定不能混：正常的次数用尽仍是 `RETRY_EXHAUSTED`。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        first = _bind(repo, "req_exh")
        _fail(repo, first)
        child = repo.retry(first, max_retries=1)
        _fail(repo, child.execution_id)

        with pytest.raises(RepositoryError) as exc:
            repo.retry(child.execution_id, max_retries=1)
        assert exc.value.code == ErrorCode.RETRY_EXHAUSTED.value
    finally:
        repo.close()


def test_concurrent_retry_loser_goes_through_budget_check(tmp_path: Path) -> None:
    """锁住机制：并发 retry 的输家被 `can_retry` 拒掉，而不是撞唯一索引。

    因为写事务是 `BEGIN IMMEDIATE`，第二个 retry 会先等赢家提交，再重新计数 ——
    它看到的是「次数已用掉」。所以输家拿到 `RETRY_EXHAUSTED`，
    `RETRY_RACE_LOST` 在真实并发下不出现。这条断言就是这件事的证据。

    **本测试同时锁住了写事务的隔离策略。** 断言 `"retries used"` 成立的前提是
    `BEGIN IMMEDIATE` 让写者串行化；若有人为了提并发吞吐把写事务改成
    `BEGIN DEFERRED`，输家就可能真的撞上唯一索引、拿到 `RETRY_RACE_LOST`，
    这里会失败。那不是"过时断言"，而是隔离策略被动了 —— 先确认新策略再改本测试。
    """
    db = tmp_path / "protocol.db"
    primary = ExecutionRepository(db)
    secondary = ExecutionRepository(db)
    try:
        bound = _bind(primary, "req_loser")
        _fail(primary, bound)

        barrier = threading.Barrier(2)
        lock = threading.Lock()
        outcomes: list[tuple[str, str]] = []

        def worker(instance: ExecutionRepository) -> None:
            barrier.wait(timeout=5)
            try:
                instance.retry(bound, max_retries=1)
                with lock:
                    outcomes.append(("granted", ""))
            except RepositoryError as exc:
                with lock:
                    outcomes.append((exc.code, str(exc)))

        threads = [
            threading.Thread(target=worker, args=(instance,))
            for instance in (primary, secondary)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert sorted(code for code, _ in outcomes) == [
            ErrorCode.RETRY_EXHAUSTED.value, "granted",
        ]
        rejected = next(text for code, text in outcomes if code == ErrorCode.RETRY_EXHAUSTED.value)
        assert "retries used" in rejected          # 来自 can_retry，不是唯一索引冲突
        assert ErrorCode.RETRY_RACE_LOST.value not in rejected
    finally:
        primary.close()
        secondary.close()


# ---------------------------------------------------------------------------
# 四、事务边界
# ---------------------------------------------------------------------------

def test_concurrent_bind_across_separate_connections(tmp_path: Path) -> None:
    """8 个**独立连接**抢同一个 `request_id`：靠 BEGIN IMMEDIATE + 唯一索引，不靠进程内锁。"""
    db = tmp_path / "protocol.db"
    repos = [ExecutionRepository(db) for _ in range(8)]
    try:
        barrier = threading.Barrier(len(repos))
        lock = threading.Lock()
        results: list[str] = []
        created: list[bool] = []
        failures: list[BaseException] = []

        def worker(instance: ExecutionRepository) -> None:
            try:
                barrier.wait(timeout=5)
                outcome = instance.bind_request(
                    request_id="req_cross",
                    workflow_id="article_generation",
                    workflow_version=1,
                    input_snapshot={"prompt": "画一只猫"},
                )
            except BaseException as exc:  # noqa: BLE001 - 原样带出去
                with lock:
                    failures.append(exc)
                return
            with lock:
                results.append(outcome.execution_id)
                created.append(outcome.created)

        threads = [threading.Thread(target=worker, args=(r,)) for r in repos]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert failures == []
        assert len(set(results)) == 1
        assert sum(1 for flag in created if flag) == 1
        assert len(repos[0].list_attempts("req_cross")) == 1
    finally:
        for instance in repos:
            instance.close()


def test_repeated_bind_leaves_no_dangling_transaction(tmp_path: Path) -> None:
    """幂等命中之后事务必须收干净：不残留事务，后续写照常。"""
    repo = ExecutionRepository(tmp_path / "protocol.db")
    try:
        first = _bind(repo, "req_sv")
        for _ in range(3):
            again = repo.bind_request(
                request_id="req_sv",
                workflow_id="article_generation",
                workflow_version=1,
                input_snapshot={"prompt": "画一只猫"},
            )
            assert again.execution_id == first
            assert repo._conn.in_transaction is False      # 没有悬挂事务
            assert repo._conn.execute(
                "SELECT COUNT(*) FROM executions"
            ).fetchone()[0] == 1

        repo.append_event(first, "engine_started")          # 之后照常能写
        assert repo.get(first).state == ExecutionState.RUNNING.value
    finally:
        repo.close()


def test_idempotent_hit_keeps_the_transaction_usable(tmp_path: Path) -> None:
    """直接用 SQL 钉住 `bind_request` 依赖的那条性质。

    幂等命中后要在**同一个事务**里继续 SELECT，前提是撞主键不会中止事务。
    SQLite 默认 `ON CONFLICT ABORT` 只回滚出错的那条语句 —— 这个用例把这条
    隐含前提变成显式断言：换方言、或有人把 DDL 改成 `ON CONFLICT ROLLBACK`
    （实测那样会连 SAVEPOINT 一起回滚），这里就会先炸，而不是等运行时才暴露。
    """
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db) as repo:
        repo._conn  # 建库

    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO request_bindings(request_id, execution_id, created_at) "
            "VALUES(?, ?, ?)",
            ("req_probe", "exec_probe", "2026-09-29T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO request_bindings(request_id, execution_id, created_at) "
                "VALUES(?, ?, ?)",
                ("req_probe", "exec_other", "2026-09-29T00:00:00Z"),
            )

        # 关键断言：事务还活着，后续语句与 COMMIT 都能走完
        assert conn.in_transaction is True
        assert conn.execute("SELECT COUNT(*) FROM request_bindings").fetchone()[0] == 1
        conn.execute("COMMIT")
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_original_error_survives_a_dead_transaction(tmp_path: Path) -> None:
    """回滚失败不得顶掉真正的异常。

    构造：让写操作自己把事务弄没（模拟 `ON CONFLICT ROLLBACK` 那种整事务回滚），
    再抛一个业务异常。若清理逻辑无脑 `ROLLBACK`，它抛的
    `cannot rollback - no transaction is active` 会盖掉业务异常，
    排查时看到的就完全不是根因。
    """
    repo = ExecutionRepository(tmp_path / "protocol.db")

    class Boom(Exception):
        pass

    def sabotaged(_conn: sqlite3.Connection) -> None:
        repo._conn.execute("ROLLBACK")   # 事务没了
        raise Boom("真正的原因")

    try:
        with pytest.raises(Boom, match="真正的原因"):
            repo._write(sabotaged)
    finally:
        repo.close()
