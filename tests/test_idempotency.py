"""幂等与 retry：`request_id` 的绑定语义、并发、以及「无 override」的签名保证。

四条不变式（对应 P1 实施计划的 A4 / A7）：

1. 同一 `request_id` 无论调多少次，返回的永远是**首次**那个 execution。
2. 并发抢同一个 `request_id` 时只有一条能落地（靠唯一索引，不靠先查后写）。
3. retry 新建 execution，但**不迁移绑定** —— 绑定永远指向首次。
4. retry 没有任何 override 通道：签名里就不存在能篡改输入的参数。
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path

import pytest

from runtime import BindResult, ExecutionRepository, RepositoryError
from validator.errors import ErrorCode
from validator.state_machine import ExecutionState


@pytest.fixture
def repo(tmp_path: Path):
    instance = ExecutionRepository(tmp_path / "protocol.db")
    yield instance
    instance.close()


def _bind(repo: ExecutionRepository, request_id: str, **overrides) -> BindResult:
    payload = {
        "request_id": request_id,
        "workflow_id": "article_generation",
        "workflow_version": 1,
        "input_snapshot": {"prompt": "画一只猫"},
    }
    payload.update(overrides)
    return repo.bind_request(**payload)


def _fail(repo: ExecutionRepository, execution_id: str) -> None:
    repo.append_event(execution_id, "engine_started")
    repo.append_event(execution_id, "engine_failed", error_code=ErrorCode.BUDGET_EXCEEDED.value)


def _complete(repo: ExecutionRepository, execution_id: str) -> None:
    repo.append_event(execution_id, "engine_started")
    repo.append_event(execution_id, "engine_completed")


# ---------------------------------------------------------------------------
# 不变式 1：绑定不漂移
# ---------------------------------------------------------------------------

def test_same_request_id_always_returns_the_first_execution(repo: ExecutionRepository) -> None:
    first = _bind(repo, "req_i")
    assert first.created is True

    for _ in range(4):
        again = _bind(repo, "req_i")
        assert again.created is False
        assert again.execution_id == first.execution_id

    assert len(repo.list_attempts("req_i")) == 1
    assert repo.get(first.execution_id).attempt == 0


def test_interleaved_request_ids_do_not_cross_talk(repo: ExecutionRepository) -> None:
    a1 = _bind(repo, "req_a")
    b1 = _bind(repo, "req_b")
    a2 = _bind(repo, "req_a")
    b2 = _bind(repo, "req_b")

    assert a2.execution_id == a1.execution_id
    assert b2.execution_id == b1.execution_id
    assert a1.execution_id != b1.execution_id


def test_different_request_ids_get_different_executions(repo: ExecutionRepository) -> None:
    ids = {_bind(repo, f"req_{i}").execution_id for i in range(5)}
    assert len(ids) == 5


def test_input_mismatch_is_flagged_but_still_returns_the_bound_execution(
    repo: ExecutionRepository,
) -> None:
    first = _bind(repo, "req_m", input_snapshot={"prompt": "猫"})

    mismatched = _bind(repo, "req_m", input_snapshot={"prompt": "狗"})
    assert mismatched.created is False
    assert mismatched.execution_id == first.execution_id
    assert mismatched.input_mismatch is True

    same = _bind(repo, "req_m", input_snapshot={"prompt": "猫"})
    assert same.input_mismatch is False
    # 契约要求返回既有 execution，所以原输入不被覆盖
    assert repo.get(first.execution_id).input_snapshot == {"prompt": "猫"}


# ---------------------------------------------------------------------------
# 不变式 2：并发只落一条
# ---------------------------------------------------------------------------

def test_concurrent_bind_yields_a_single_execution(tmp_path: Path) -> None:
    repo = ExecutionRepository(tmp_path / "protocol.db")
    workers = 8
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    results: list[BindResult] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            outcome = _bind(repo, "req_concurrent")
        except BaseException as exc:  # noqa: BLE001 - 测试要把失败原样带出来
            with lock:
                failures.append(exc)
            return
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    try:
        assert failures == []
        assert len(results) == workers
        assert len({r.execution_id for r in results}) == 1
        assert sum(1 for r in results if r.created) == 1
        assert len(repo.list_attempts("req_concurrent")) == 1
    finally:
        repo.close()


def test_concurrent_retry_cannot_duplicate_an_attempt(tmp_path: Path) -> None:
    db = tmp_path / "protocol.db"
    primary = ExecutionRepository(db)
    secondary = ExecutionRepository(db)
    try:
        bound = _bind(primary, "req_race")
        _fail(primary, bound.execution_id)

        barrier = threading.Barrier(2)
        lock = threading.Lock()
        granted: list[BindResult] = []
        refused: list[str] = []

        def worker(instance: ExecutionRepository) -> None:
            barrier.wait(timeout=5)
            try:
                outcome = instance.retry(bound.execution_id, max_retries=1)
            except RepositoryError as exc:
                with lock:
                    refused.append(exc.code)
                return
            with lock:
                granted.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(instance,))
            for instance in (primary, secondary)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert len(granted) == 1
        assert refused == [ErrorCode.RETRY_EXHAUSTED.value]
        # 首次 + 只成功一次 retry
        assert [r.attempt for r in primary.list_attempts("req_race")] == [0, 1]
    finally:
        primary.close()
        secondary.close()


# ---------------------------------------------------------------------------
# 不变式 3：retry 不迁移绑定
# ---------------------------------------------------------------------------

def test_retry_creates_a_child_but_binding_stays_on_the_first(repo: ExecutionRepository) -> None:
    first = _bind(repo, "req_x")
    _fail(repo, first.execution_id)

    child = repo.retry(first.execution_id, max_retries=2)
    assert child.created is True
    assert child.execution_id != first.execution_id

    assert repo.get_by_request("req_x").execution_id == first.execution_id
    assert _bind(repo, "req_x").execution_id == first.execution_id
    assert [r.attempt for r in repo.list_attempts("req_x")] == [0, 1]


def test_retry_chain_increments_attempt_and_keeps_the_root(repo: ExecutionRepository) -> None:
    first = _bind(repo, "req_chain")
    _fail(repo, first.execution_id)
    second = repo.retry(first.execution_id, max_retries=2)
    _fail(repo, second.execution_id)
    third = repo.retry(second.execution_id, max_retries=2)

    assert [r.attempt for r in repo.list_attempts("req_chain")] == [0, 1, 2]
    assert third.record.parent_execution_id == second.execution_id
    assert third.record.root_execution_id == first.execution_id
    assert third.record.request_id == "req_chain"
    assert third.record.state == ExecutionState.PENDING.value
    assert third.record.seq == 0


def test_retry_leaves_the_original_untouched(repo: ExecutionRepository) -> None:
    first = _bind(repo, "req_keep")
    _fail(repo, first.execution_id)
    record_before = repo.get(first.execution_id)
    events_before = repo.list_events(first.execution_id)

    repo.retry(first.execution_id, max_retries=1)

    assert repo.list_events(first.execution_id) == events_before
    assert repo.get(first.execution_id) == record_before


def test_retry_of_unknown_execution_is_not_found(repo: ExecutionRepository) -> None:
    with pytest.raises(RepositoryError) as exc:
        repo.retry("exec_nope", max_retries=1)
    assert exc.value.code == ErrorCode.EXECUTION_NOT_FOUND.value


# ---------------------------------------------------------------------------
# 不变式 4：无 override
# ---------------------------------------------------------------------------

def test_retry_signature_has_no_override_channel() -> None:
    params = list(inspect.signature(ExecutionRepository.retry).parameters)
    assert params == ["self", "execution_id", "max_retries", "budget_raised"]

    banned = ("override", "input", "snapshot", "prompt", "workflow")
    assert [name for name in params if any(word in name for word in banned)] == []


def test_retry_copies_input_snapshot_verbatim(repo: ExecutionRepository) -> None:
    snapshot = {"prompt": "画一只猫", "nested": {"k": [1, 2, 3]}, "画幅": "16:9"}
    first = _bind(repo, "req_copy", input_snapshot=snapshot)
    _fail(repo, first.execution_id)

    child = repo.retry(first.execution_id, max_retries=1)

    assert child.record.input_snapshot == snapshot
    assert child.record.input_hash == first.record.input_hash


# ---------------------------------------------------------------------------
# retry 门槛
# ---------------------------------------------------------------------------

def test_completed_cannot_be_retried(repo: ExecutionRepository) -> None:
    bound = _bind(repo, "req_done")
    _complete(repo, bound.execution_id)

    with pytest.raises(RepositoryError) as exc:
        repo.retry(bound.execution_id, max_retries=3)
    assert exc.value.code == ErrorCode.RETRY_NOT_ALLOWED_FOR_COMPLETED.value
    assert len(repo.list_attempts("req_done")) == 1


def test_running_cannot_be_retried(repo: ExecutionRepository) -> None:
    bound = _bind(repo, "req_live")
    repo.append_event(bound.execution_id, "engine_started")

    with pytest.raises(RepositoryError) as exc:
        repo.retry(bound.execution_id, max_retries=3)
    assert exc.value.code == ErrorCode.NOT_TERMINAL.value


def test_budget_exceeded_needs_an_explicitly_raised_budget(repo: ExecutionRepository) -> None:
    bound = _bind(repo, "req_budget")
    repo.append_event(bound.execution_id, "engine_started")
    repo.append_event(bound.execution_id, "budget_exceeded")
    assert repo.get(bound.execution_id).state == ExecutionState.BUDGET_EXCEEDED.value

    with pytest.raises(RepositoryError) as exc:
        repo.retry(bound.execution_id, max_retries=2)
    assert exc.value.code == ErrorCode.RETRY_BUDGET_NOT_RAISED.value

    raised = repo.retry(bound.execution_id, max_retries=2, budget_raised=True)
    assert raised.created is True
    assert raised.record.attempt == 1


def test_retry_exhausted_after_max_retries(repo: ExecutionRepository) -> None:
    first = _bind(repo, "req_exh")
    _fail(repo, first.execution_id)
    child = repo.retry(first.execution_id, max_retries=1)
    _fail(repo, child.execution_id)

    with pytest.raises(RepositoryError) as exc:
        repo.retry(child.execution_id, max_retries=1)
    assert exc.value.code == ErrorCode.RETRY_EXHAUSTED.value


def test_zero_max_retries_refuses_the_first_retry(repo: ExecutionRepository) -> None:
    bound = _bind(repo, "req_zero")
    _fail(repo, bound.execution_id)

    with pytest.raises(RepositoryError) as exc:
        repo.retry(bound.execution_id, max_retries=0)
    assert exc.value.code == ErrorCode.RETRY_EXHAUSTED.value


def test_retried_execution_can_run_again(repo: ExecutionRepository) -> None:
    """retry 出来的新执行是可跑的：PENDING → RUNNING → COMPLETED，且自检一致。"""
    first = _bind(repo, "req_replay")
    _fail(repo, first.execution_id)
    child = repo.retry(first.execution_id, max_retries=1)

    _complete(repo, child.execution_id)

    record = repo.verify_consistency(child.execution_id)
    assert record.state == ExecutionState.COMPLETED.value
    assert repo.replay_state(child.execution_id) == ExecutionState.COMPLETED.value
    assert repo.get_by_request("req_replay").execution_id == first.execution_id
