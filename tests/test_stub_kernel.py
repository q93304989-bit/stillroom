"""Stub 内核：六步推进 + A6「中止两段式」。

A6 的核心是**中止不掐断正在跑的步骤**：`abort_requested` 只把执行置
`ABORT_PENDING`，等当前步骤自己收尾（`engine_step_ended`）才 → `ABORTED`。
这样才能不留半成品，也才能被 Replay。

三个时序用例（审查提的三种边界）：

| 用例 | 时序 | 期望 |
|---|---|---|
| 中途中止 | `RUNNING(generate)` → abort → `ABORT_PENDING` → `advance()` | `ABORTED`；被打断步骤的 `payload_ref` 只在事件行，不上浮 |
| 最后一步中止 | `RUNNING(deliver)` → abort → `ABORT_PENDING` → `advance()` | `ABORTED`；**`deliver` 也不上浮**（终态不是 COMPLETED） |
| 完成后中止 | `COMPLETED` → abort | `ALREADY_TERMINAL`；`output_ref` 保留 |

第二个用例正是 `output_ref` 收紧为"只认 COMPLETED"的验证点：
`deliver` 是交付步骤，但执行以 `ABORTED` 收场，交付指针就不该被写上去。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from runtime import ExecutionRepository, KernelError, StubKernel
from runtime.errors import RepositoryError
from validator.errors import ErrorCode
from validator.pipeline import PIPELINE_STEPS
from validator.state_machine import ExecutionEvent, ExecutionState

_EVENT_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "execution-event.schema.json").read_text(
        encoding="utf-8"
    )
)
EVENT_VALIDATOR = Draft202012Validator(_EVENT_SCHEMA)


@pytest.fixture
def kernel(tmp_path: Path):
    repo = ExecutionRepository(tmp_path / "protocol.db")
    execution_id = repo.bind_request(
        request_id="req_kernel",
        workflow_id="article_generation",
        workflow_version=1,
        input_snapshot={"prompt": "画一只猫"},
    ).execution_id
    stub = StubKernel(repo, execution_id)
    yield stub, repo
    repo.close()


def _drive_to(kernel: StubKernel, step: str) -> None:
    """start 并推进到 `step` 正在跑（即让前面若干步收尾）。"""
    kernel.start()
    while kernel.current_step() != step:
        outcome = kernel.advance()
        assert outcome.state == ExecutionState.RUNNING.value


def _event_payloads(repo: ExecutionRepository, execution_id: str) -> dict[str, str]:
    """步骤 → 该步事件行上的 payload_ref。"""
    return {
        event.step: event.payload_ref
        for event in repo.list_events(execution_id)
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value and event.step
    }


# ---------------------------------------------------------------------------
# 正常推进
# ---------------------------------------------------------------------------

def test_forward_progress_walks_all_six_steps(kernel) -> None:
    stub, repo = kernel
    assert stub.state == ExecutionState.PENDING.value
    assert stub.current_step() is None          # 还没 start，没有步骤在跑

    assert stub.start() == ExecutionState.RUNNING.value
    assert stub.current_step() == "understand"

    seen = []
    for _ in range(len(PIPELINE_STEPS)):
        outcome = stub.advance()
        seen.append(outcome.step_ended)
        assert outcome.state == ExecutionState.RUNNING.value
    assert tuple(seen) == PIPELINE_STEPS

    # 六步都收尾了 → 没有步骤在跑，该交付了
    assert stub.current_step() is None
    assert stub.complete() == ExecutionState.COMPLETED.value
    assert repo.get(stub.execution_id).output_ref == (
        f"artifacts/{stub.execution_id}/deliver.json"
    )
    repo.verify_consistency(stub.execution_id)


def test_run_to_completion_delivers_and_self_checks(kernel) -> None:
    stub, repo = kernel
    assert stub.run_to_completion() == ExecutionState.COMPLETED.value

    record = repo.get(stub.execution_id)
    assert record.seq == len(PIPELINE_STEPS) + 2      # start + 六步 + complete
    assert repo.replay_state(stub.execution_id) == ExecutionState.COMPLETED.value
    repo.verify_consistency(stub.execution_id)


# ---------------------------------------------------------------------------
# A6：中止两段式
# ---------------------------------------------------------------------------

def test_abort_mid_flight_finishes_the_current_step_then_aborts(kernel) -> None:
    """中止**不掐断**正在跑的步骤：先 ABORT_PENDING，步骤收尾才 ABORTED。"""
    stub, repo = kernel
    _drive_to(stub, "generate")

    assert stub.abort() == ExecutionState.ABORT_PENDING.value
    # 关键：此刻还是 ABORT_PENDING，步骤没有被掐断
    assert stub.state == ExecutionState.ABORT_PENDING.value
    assert stub.current_step() == "generate"
    # start + understand + reference + abort_requested：注意 abort 本身也是一条事件
    assert repo.get(stub.execution_id).seq == 4

    outcome = stub.advance()
    assert outcome.step_ended == "generate"          # 收尾的是被打断那一步
    assert outcome.state == ExecutionState.ABORTED.value
    assert outcome.aborted is True
    # 中止收尾后**没有下一步** —— 不许报出 evaluate 去忽悠调度器再开一步
    assert outcome.next_step is None

    record = repo.get(stub.execution_id)
    assert record.state == ExecutionState.ABORTED.value
    # 被打断步骤的产物只在事件行里，不上浮成交付指针
    assert record.output_ref is None
    assert _event_payloads(repo, stub.execution_id)["generate"] == outcome.payload_ref
    repo.verify_consistency(stub.execution_id)


def test_abort_on_the_last_step_does_not_float_the_delivery(kernel) -> None:
    """`deliver` 已收尾、但执行以 ABORTED 收场 —— 交付指针仍不得写上去。"""
    stub, repo = kernel
    _drive_to(stub, "deliver")

    assert stub.abort() == ExecutionState.ABORT_PENDING.value
    outcome = stub.advance()
    assert outcome.step_ended == "deliver"
    assert outcome.state == ExecutionState.ABORTED.value

    record = repo.get(stub.execution_id)
    assert record.state == ExecutionState.ABORTED.value
    assert record.output_ref is None, "ABORTED 不是 COMPLETED，交付指针不该上浮"
    # 产物没丢，在事件行里
    assert _event_payloads(repo, stub.execution_id)["deliver"].endswith("/deliver.json")
    # 也不该有"下一步"
    assert outcome.next_step is None
    repo.verify_consistency(stub.execution_id)


def test_abort_after_completion_is_already_terminal(kernel) -> None:
    """中止发生在完成之后：`engine_completed` 已经赢了，abort 只能吃 ALREADY_TERMINAL。"""
    stub, repo = kernel
    stub.run_to_completion()
    output_before = repo.get(stub.execution_id).output_ref

    with pytest.raises(RepositoryError) as exc:
        stub.abort()
    assert exc.value.code == ErrorCode.ALREADY_TERMINAL.value

    record = repo.get(stub.execution_id)
    assert record.state == ExecutionState.COMPLETED.value
    assert record.output_ref == output_before          # 交付指针保留
    repo.verify_consistency(stub.execution_id)


def test_abort_on_pending_is_immediate_without_two_phases(kernel) -> None:
    """`PENDING` 上没有步骤在跑，中止即时生效 —— 两段式只为"正在跑的步骤"存在。"""
    stub, repo = kernel
    assert stub.abort() == ExecutionState.ABORTED.value
    assert stub.current_step() is None
    assert repo.get(stub.execution_id).output_ref is None


def test_abort_while_waiting_for_input_aborts_immediately(kernel) -> None:
    stub, repo = kernel
    stub.start()
    stub.advance()
    repo.append_event(stub.execution_id, ExecutionEvent.INPUT_REQUIRED.value)
    assert stub.state == ExecutionState.WAITING_INPUT.value

    assert stub.abort() == ExecutionState.ABORTED.value


# ---------------------------------------------------------------------------
# 游标不许飘
# ---------------------------------------------------------------------------

def test_advance_in_abort_pending_does_not_move_a_cursor(kernel) -> None:
    """收尾之后没有"下一步"：不能再推出一个步骤来。"""
    stub, repo = kernel
    _drive_to(stub, "generate")
    stub.abort()
    stub.advance()                                    # → ABORTED

    assert stub.state == ExecutionState.ABORTED.value
    assert stub.current_step() is None
    # 事件流里最后一个 engine_step_ended 仍是 generate —— 没有伪造出 evaluate
    ended = [
        event.step
        for event in repo.list_events(stub.execution_id)
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value
    ]
    assert ended == ["understand", "reference", "generate"]


def test_cursor_is_derived_from_the_event_stream_not_memory(kernel) -> None:
    """换一个 `StubKernel` 实例接着驱动，看到的必须是同一步骤。

    这条守的是"事件流是真相"：游标若只活在内存里，
    进程重启或另一个调度器接手就会从头再跑一遍。
    """
    stub, repo = kernel
    _drive_to(stub, "evaluate")
    assert stub.current_step() == "evaluate"

    fresh = StubKernel(repo, stub.execution_id)        # 完全新实例，没有共享状态
    assert fresh.current_step() == "evaluate"
    assert fresh.state == ExecutionState.RUNNING.value

    outcome = fresh.advance()
    assert outcome.step_ended == "evaluate"
    assert outcome.next_step == "refine"
    assert stub.current_step() == "refine"             # 老实例也看得到同一步骤


def test_advance_outside_a_runnable_state_is_kernel_misuse(kernel) -> None:
    stub, repo = kernel
    with pytest.raises(KernelError) as exc:
        stub.advance()                                  # 还在 PENDING
    assert exc.value.code == ErrorCode.INVALID_TRANSITION.value

    stub.run_to_completion()
    with pytest.raises(KernelError):
        stub.advance()                                  # 已经在 COMPLETED


def test_advance_after_six_steps_refuses_to_invent_a_seventh(kernel) -> None:
    """六步跑完但还没 complete 时，`advance()` 必须拒绝 —— 不许凭空开工。"""
    stub, repo = kernel
    stub.start()
    for _ in range(len(PIPELINE_STEPS)):
        stub.advance()
    assert stub.state == ExecutionState.RUNNING.value

    with pytest.raises(KernelError) as exc:
        stub.advance()
    assert exc.value.code == ErrorCode.INVALID_TRANSITION.value


# ---------------------------------------------------------------------------
# 产物与事件形态
# ---------------------------------------------------------------------------

def test_step_payloads_are_deterministic_and_schema_valid(kernel) -> None:
    """每步的 `payload_ref` / `payload_hash` 确定性落库，且事件过 schema。"""
    stub, repo = kernel
    seen = []
    stub.start()
    for _ in range(len(PIPELINE_STEPS)):
        seen.append(stub.advance())
    stub.complete()

    events = repo.list_events(stub.execution_id)
    by_step = {o.step_ended: o for o in seen}
    for event in events:
        EVENT_VALIDATOR.validate(event.to_document())   # 直接喂 schema

    for outcome in seen:
        assert outcome.payload_ref == f"artifacts/{stub.execution_id}/{outcome.step_ended}.json"
        assert len(outcome.payload_hash) == 64          # 裸 hex，不带 sha256: 前缀
        int(outcome.payload_hash, 16)                   # 必须全是十六进制

    # 确定性：同一个 (execution_id, step) 两次算出来一样
    assert by_step["generate"].payload_hash == stub._payload_hash("generate")


def test_step_field_lands_only_on_step_boundary_events(kernel) -> None:
    """`step` 只在 `engine_step_ended` 上意味着"刚收尾的步骤"——游标推导只认它。"""
    stub, repo = kernel
    stub.run_to_completion()

    for event in repo.list_events(stub.execution_id):
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value:
            assert event.step in PIPELINE_STEPS
        else:
            assert event.step is None, f"{event.type} 不该带 step"


def test_failed_execution_records_the_code_and_no_delivery(kernel) -> None:
    stub, repo = kernel
    stub.start()
    stub.advance()
    assert stub.fail(ErrorCode.BUDGET_EXCEEDED.value, message="provider said no") == (
        ExecutionState.FAILED.value
    )

    record = repo.get(stub.execution_id)
    assert record.error_code == ErrorCode.BUDGET_EXCEEDED.value
    assert record.output_ref is None
    repo.verify_consistency(stub.execution_id)


def test_run_to_completion_stops_at_aborted_when_abort_is_pending(kernel) -> None:
    """调度器遇到 ABORT_PENDING 要收尾后停下，不能再开新步骤。"""
    stub, repo = kernel
    _drive_to(stub, "reference")
    stub.abort()

    assert stub.run_to_completion() == ExecutionState.ABORTED.value
    ended = [
        event.step
        for event in repo.list_events(stub.execution_id)
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value
    ]
    assert ended == ["understand", "reference"]         # 没有往下开 generate
