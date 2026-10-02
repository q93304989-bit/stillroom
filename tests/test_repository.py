"""执行仓库单测：追加式事件、状态重放、非法转移不落行。

全部离线，数据落在 `tmp_path` 里，不碰真实数据目录。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import runtime.repository as repository_module
from runtime import ExecutionRepository, RepositoryError, SCHEMA_VERSION
from validator.errors import ErrorCode
from validator.state_machine import ExecutionState

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schemas" / "execution-event.schema.json"

SIX_STEPS = ("understand", "reference", "generate", "evaluate", "refine", "deliver")


@pytest.fixture
def repo(tmp_path: Path):
    instance = ExecutionRepository(tmp_path / "protocol.db")
    yield instance
    instance.close()


def _bind(repo: ExecutionRepository, request_id: str = "req_1", **overrides):
    payload = {
        "request_id": request_id,
        "workflow_id": "article_generation",
        "workflow_version": 1,
        "input_snapshot": {"prompt": "画一只猫"},
    }
    payload.update(overrides)
    return repo.bind_request(**payload)


def _drive_to_completed(repo: ExecutionRepository, execution_id: str) -> None:
    repo.append_event(execution_id, "engine_started")
    for step in SIX_STEPS:
        repo.append_event(execution_id, "engine_step_ended", step=step)
    repo.append_event(execution_id, "engine_completed", payload_ref="artifacts/exec/final.json")


def _drive_to_failed(repo: ExecutionRepository, execution_id: str) -> None:
    repo.append_event(execution_id, "engine_started")
    repo.append_event(execution_id, "engine_failed", error_code=ErrorCode.BUDGET_EXCEEDED.value,
                      message="provider said no")


# ---------------------------------------------------------------------------
# 建库与生命周期
# ---------------------------------------------------------------------------

def test_fresh_database_reaches_current_schema_version(tmp_path: Path) -> None:
    with ExecutionRepository(tmp_path / "protocol.db") as repo:
        assert repo.schema_version == SCHEMA_VERSION


def test_database_uses_wal_and_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "protocol.db"
    with ExecutionRepository(nested) as repo:
        assert nested.exists()
        mode = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_database_newer_than_code_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "protocol.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.close()

    with pytest.raises(RepositoryError) as exc:
        ExecutionRepository(db)
    assert exc.value.code == ErrorCode.SCHEMA_INVALID.value


def test_two_repositories_on_same_file_see_the_same_rows(tmp_path: Path) -> None:
    db = tmp_path / "protocol.db"
    first = ExecutionRepository(db)
    second = ExecutionRepository(db)
    try:
        bound = _bind(first, "req_shared")
        assert second.get(bound.execution_id).request_id == "req_shared"
        second.append_event(bound.execution_id, "engine_started")
        assert first.get(bound.execution_id).state == ExecutionState.RUNNING.value
    finally:
        first.close()
        second.close()


# ---------------------------------------------------------------------------
# 绑定与读取
# ---------------------------------------------------------------------------

def test_bind_creates_pending_execution(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    assert bound.created is True
    record = bound.record
    assert record.state == ExecutionState.PENDING.value
    assert record.attempt == 0
    assert record.parent_execution_id is None
    assert record.root_execution_id == record.execution_id
    assert record.seq == 0
    assert record.is_terminal is False
    assert record.is_retry is False


def test_get_unknown_execution_raises(repo: ExecutionRepository) -> None:
    with pytest.raises(RepositoryError) as exc:
        repo.get("exec_does_not_exist")
    assert exc.value.code == ErrorCode.EXECUTION_NOT_FOUND.value


def test_get_by_request_unknown_returns_none(repo: ExecutionRepository) -> None:
    assert repo.get_by_request("req_never_seen") is None


def test_list_events_of_unknown_execution_raises(repo: ExecutionRepository) -> None:
    with pytest.raises(RepositoryError) as exc:
        repo.list_events("exec_does_not_exist")
    assert exc.value.code == ErrorCode.EXECUTION_NOT_FOUND.value


def test_request_id_is_validated(repo: ExecutionRepository) -> None:
    for bad in ("", "has space", "a" * 129, "semi;colon"):
        with pytest.raises(RepositoryError) as exc:
            _bind(repo, bad)
        assert exc.value.code == ErrorCode.INPUT_SCHEMA_INVALID.value, bad


def test_input_snapshot_must_be_a_json_object(repo: ExecutionRepository) -> None:
    with pytest.raises(RepositoryError) as exc:
        _bind(repo, "req_bad_input", input_snapshot=["not", "an", "object"])
    assert exc.value.code == ErrorCode.INPUT_SCHEMA_INVALID.value

    with pytest.raises(RepositoryError) as exc:
        _bind(repo, "req_bad_input2", input_snapshot={"bad": object()})
    assert exc.value.code == ErrorCode.INPUT_SCHEMA_INVALID.value


def test_input_hash_is_stable_across_key_order(repo: ExecutionRepository) -> None:
    a = _bind(repo, "req_a", input_snapshot={"x": 1, "y": [1, 2]})
    b = _bind(repo, "req_b", input_snapshot={"y": [1, 2], "x": 1})
    assert a.record.input_hash == b.record.input_hash


# ---------------------------------------------------------------------------
# 事件推进
# ---------------------------------------------------------------------------

def test_append_event_advances_state_and_seq(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    event = repo.append_event(bound.execution_id, "engine_started")

    assert event.seq == 1
    assert event.status_before == ExecutionState.PENDING.value
    assert event.status_after == ExecutionState.RUNNING.value
    assert event.type == "engine_started"

    record = repo.get(bound.execution_id)
    assert record.state == ExecutionState.RUNNING.value
    assert record.seq == 1


def test_seq_increments_monotonically(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    _drive_to_completed(repo, bound.execution_id)
    events = repo.list_events(bound.execution_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_unknown_event_name_is_a_programming_error(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    with pytest.raises(ValueError):
        repo.append_event(bound.execution_id, "not_an_event")


def test_full_pipeline_reaches_completed(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    _drive_to_completed(repo, bound.execution_id)

    record = repo.get(bound.execution_id)
    assert record.state == ExecutionState.COMPLETED.value
    # 交付指针来自达到终态的那个事件的 payload_ref（不是独立入参）
    assert record.output_ref == "artifacts/exec/final.json"
    assert record.is_terminal is True
    # 1 次 start + 6 次 step_ended + 1 次 completed
    assert len(repo.list_events(bound.execution_id)) == 8


def test_abort_is_two_phase(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")

    first = repo.append_event(bound.execution_id, "abort_requested")
    assert first.status_after == ExecutionState.ABORT_PENDING.value
    assert repo.get(bound.execution_id).is_terminal is False

    second = repo.append_event(bound.execution_id, "engine_step_ended", step="generate")
    assert second.status_after == ExecutionState.ABORTED.value
    assert repo.get(bound.execution_id).is_terminal is True


def test_abort_pending_has_no_other_exit(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")
    repo.append_event(bound.execution_id, "abort_requested")
    before = repo.list_events(bound.execution_id)

    for event in ("engine_completed", "engine_failed", "abort_requested", "input_required"):
        with pytest.raises(RepositoryError) as exc:
            repo.append_event(bound.execution_id, event)
        assert exc.value.code == ErrorCode.INVALID_TRANSITION.value, event

    # 状态没动，也没多出任何事件行
    assert repo.get(bound.execution_id).state == ExecutionState.ABORT_PENDING.value
    assert repo.list_events(bound.execution_id) == before


def test_terminal_execution_rejects_every_event(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    _drive_to_completed(repo, bound.execution_id)
    before = repo.list_events(bound.execution_id)

    for event in ("engine_started", "engine_step_ended", "abort_requested", "engine_failed"):
        with pytest.raises(RepositoryError) as exc:
            repo.append_event(bound.execution_id, event)
        assert exc.value.code == ErrorCode.ALREADY_TERMINAL.value, event

    assert repo.list_events(bound.execution_id) == before


def test_invalid_transition_writes_nothing(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    with pytest.raises(RepositoryError) as exc:
        repo.append_event(bound.execution_id, "engine_completed")
    assert exc.value.code == ErrorCode.INVALID_TRANSITION.value

    assert repo.get(bound.execution_id).state == ExecutionState.PENDING.value
    assert repo.list_events(bound.execution_id) == ()


def test_waiting_input_round_trip(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")
    repo.append_event(bound.execution_id, "input_required")
    assert repo.get(bound.execution_id).state == ExecutionState.WAITING_INPUT.value

    resumed = repo.append_event(bound.execution_id, "resume_requested")
    assert resumed.status_after == ExecutionState.RUNNING.value


def test_resume_outside_waiting_input_is_rejected(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")
    with pytest.raises(RepositoryError) as exc:
        repo.append_event(bound.execution_id, "resume_requested")
    assert exc.value.code == ErrorCode.NOT_WAITING_INPUT.value


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "driver,expected",
    [
        (lambda r, e: None, ExecutionState.PENDING.value),
        (lambda r, e: r.append_event(e, "engine_started"), ExecutionState.RUNNING.value),
        (_drive_to_completed, ExecutionState.COMPLETED.value),
        (_drive_to_failed, ExecutionState.FAILED.value),
    ],
)
def test_replay_matches_cached_state(repo, driver, expected) -> None:
    bound = _bind(repo)
    driver(repo, bound.execution_id)

    assert repo.get(bound.execution_id).state == expected
    assert repo.replay_state(bound.execution_id) == expected


def test_replay_of_abort_chain(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")
    repo.append_event(bound.execution_id, "abort_requested")
    repo.append_event(bound.execution_id, "engine_step_ended", step="evaluate")

    assert repo.replay_state(bound.execution_id) == ExecutionState.ABORTED.value


def test_replay_detects_tampered_event(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    _drive_to_completed(repo, bound.execution_id)

    conn = sqlite3.connect(str(repo.db_path))
    conn.execute(
        "UPDATE execution_events SET status_after = ? WHERE execution_id = ? AND seq = 1",
        (ExecutionState.FAILED.value, bound.execution_id),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RepositoryError) as exc:
        repo.replay_state(bound.execution_id)
    assert exc.value.code == ErrorCode.INVALID_TRANSITION.value


def test_verify_consistency_passes_on_intact_chain(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    _drive_to_completed(repo, bound.execution_id)
    assert repo.verify_consistency(bound.execution_id).state == ExecutionState.COMPLETED.value


def test_verify_consistency_detects_missing_event_row(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")
    repo.append_event(bound.execution_id, "input_required")

    conn = sqlite3.connect(str(repo.db_path))
    conn.execute(
        "DELETE FROM execution_events WHERE execution_id = ? AND seq = 2",
        (bound.execution_id,),
    )
    conn.commit()
    conn.close()

    # 缓存状态还停在 WAITING_INPUT，但事件流已无法解释它
    assert repo.replay_state(bound.execution_id) == ExecutionState.RUNNING.value
    with pytest.raises(RepositoryError) as exc:
        repo.verify_consistency(bound.execution_id)
    assert exc.value.code == ErrorCode.INVALID_TRANSITION.value


def test_verify_consistency_detects_bypassed_state_write(repo: ExecutionRepository) -> None:
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")

    conn = sqlite3.connect(str(repo.db_path))
    conn.execute(
        "UPDATE executions SET state = ? WHERE execution_id = ?",
        (ExecutionState.COMPLETED.value, bound.execution_id),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RepositoryError) as exc:
        repo.verify_consistency(bound.execution_id)
    assert exc.value.code == ErrorCode.INVALID_TRANSITION.value


# ---------------------------------------------------------------------------
# 结构与契约
# ---------------------------------------------------------------------------

def test_repository_source_never_deletes_or_rewrites_events() -> None:
    """追加式不是靠自觉：源码里不允许出现删事件或改事件的语句。"""
    source = Path(repository_module.__file__).read_text(encoding="utf-8")
    assert "DELETE FROM" not in source
    assert "UPDATE execution_events" not in source


def test_repository_public_surface_has_no_destructive_methods(repo: ExecutionRepository) -> None:
    public = {name for name in dir(repo) if not name.startswith("_")}
    assert public.isdisjoint({"delete", "remove", "drop", "purge", "reset", "truncate"})


def test_event_document_passes_frozen_schema(repo: ExecutionRepository) -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))
    bound = _bind(repo)
    repo.append_event(bound.execution_id, "engine_started")
    repo.append_event(bound.execution_id, "engine_step_ended", step="understand")

    for event in repo.list_events(bound.execution_id):
        document = event.to_document()
        assert not list(validator.iter_errors(document)), document
        assert document["request_id"] == bound.record.request_id
