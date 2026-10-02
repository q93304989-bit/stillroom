"""执行状态机单测：把 P1 v1.0 的四条不变式锁死。

这些测试全部离线、纯函数，不碰 SQLite —— 所以它们能在无 UI 环境下先跑通，
再去接 Repository。
"""

from __future__ import annotations

import pytest

from validator.errors import ErrorCode
from validator.state_machine import (
    TERMINAL_STATES,
    ExecutionEvent,
    ExecutionState,
    allowed_events,
    apply,
    can_retry,
    is_terminal,
)


VALID_TRANSITIONS = [
    ("PENDING", "engine_started", "RUNNING"),
    ("PENDING", "abort_requested", "ABORTED"),
    ("PENDING", "engine_timeout", "TIMEOUT"),
    ("PENDING", "budget_exceeded", "BUDGET_EXCEEDED"),
    ("RUNNING", "engine_step_ended", "RUNNING"),
    ("RUNNING", "engine_completed", "COMPLETED"),
    ("RUNNING", "engine_failed", "FAILED"),
    ("RUNNING", "engine_timeout", "TIMEOUT"),
    ("RUNNING", "budget_exceeded", "BUDGET_EXCEEDED"),
    ("RUNNING", "input_required", "WAITING_INPUT"),
    ("RUNNING", "abort_requested", "ABORT_PENDING"),
    ("WAITING_INPUT", "input_provided", "RUNNING"),
    ("WAITING_INPUT", "resume_requested", "RUNNING"),
    ("WAITING_INPUT", "abort_requested", "ABORTED"),
    ("WAITING_INPUT", "engine_timeout", "TIMEOUT"),
    ("WAITING_INPUT", "budget_exceeded", "BUDGET_EXCEEDED"),
    ("ABORT_PENDING", "engine_step_ended", "ABORTED"),
]


@pytest.mark.parametrize(
    "state,event,expected", VALID_TRANSITIONS,
    ids=[f"{s}+{e}" for s, e, _ in VALID_TRANSITIONS],
)
def test_valid_transition(state: str, event: str, expected: str) -> None:
    result = apply(state, event)
    assert result.ok, result
    assert result.state is ExecutionState[expected]
    assert result.error_code is None


def test_enum_and_string_inputs_are_equivalent() -> None:
    a = apply(ExecutionState.RUNNING, ExecutionEvent.ABORT_REQUESTED)
    b = apply("RUNNING", "abort_requested")
    assert a == b
    assert a.state is ExecutionState.ABORT_PENDING


def test_unknown_state_or_event_raises() -> None:
    with pytest.raises(ValueError):
        apply("NOT_A_STATE", "engine_started")
    with pytest.raises(ValueError):
        apply("RUNNING", "not_an_event")


# ---------------------------------------------------------------------------
# 不变式 1：ABORT_PENDING 只有一个出口
# ---------------------------------------------------------------------------

def test_abort_pending_has_exactly_one_exit() -> None:
    assert allowed_events(ExecutionState.ABORT_PENDING) == (ExecutionEvent.ENGINE_STEP_ENDED,)

    for event in ExecutionEvent:
        result = apply(ExecutionState.ABORT_PENDING, event)
        if event is ExecutionEvent.ENGINE_STEP_ENDED:
            assert result.ok
            assert result.state is ExecutionState.ABORTED
            continue
        assert not result.ok, event
        # resume_requested 单独给更精确的码：它只对 WAITING_INPUT 有意义。
        expected_code = (
            ErrorCode.NOT_WAITING_INPUT.value
            if event is ExecutionEvent.RESUME_REQUESTED
            else ErrorCode.INVALID_TRANSITION.value
        )
        assert result.error_code == expected_code
        # 失败不得改变状态
        assert result.state is ExecutionState.ABORT_PENDING


def test_abort_pending_is_not_terminal() -> None:
    assert not is_terminal("ABORT_PENDING")


# ---------------------------------------------------------------------------
# 不变式 2：终态一律 ALREADY_TERMINAL
# ---------------------------------------------------------------------------

def test_terminal_set_is_exactly_five_states() -> None:
    assert {s.value for s in TERMINAL_STATES} == {
        "COMPLETED", "FAILED", "TIMEOUT", "ABORTED", "BUDGET_EXCEEDED",
    }
    for state in ExecutionState:
        assert is_terminal(state) is (state in TERMINAL_STATES)


@pytest.mark.parametrize("state", sorted(s.value for s in TERMINAL_STATES))
@pytest.mark.parametrize("event", sorted(e.value for e in ExecutionEvent))
def test_terminal_state_rejects_every_event(state: str, event: str) -> None:
    result = apply(state, event)
    assert not result.ok
    assert result.error_code == ErrorCode.ALREADY_TERMINAL.value
    assert result.state is ExecutionState[state]
    assert allowed_events(state) == ()


# ---------------------------------------------------------------------------
# 不变式 3：retry 无 override
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["FAILED", "TIMEOUT", "ABORTED"])
def test_retry_allowed_from_retryable_terminal(state: str) -> None:
    decision = can_retry(state, retries_used=0, max_retries=2)
    assert decision.ok, decision


def test_retry_rejected_for_completed() -> None:
    decision = can_retry("COMPLETED", retries_used=0, max_retries=2)
    assert not decision.ok
    assert decision.error_code == ErrorCode.RETRY_NOT_ALLOWED_FOR_COMPLETED.value


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "WAITING_INPUT", "ABORT_PENDING"])
def test_retry_rejected_for_non_terminal(state: str) -> None:
    decision = can_retry(state, retries_used=0, max_retries=2)
    assert not decision.ok
    assert decision.error_code == ErrorCode.NOT_TERMINAL.value


def test_retry_budget_exceeded_needs_raised_budget() -> None:
    blocked = can_retry("BUDGET_EXCEEDED", retries_used=0, max_retries=2)
    assert not blocked.ok
    assert blocked.error_code == ErrorCode.RETRY_BUDGET_NOT_RAISED.value

    raised = can_retry("BUDGET_EXCEEDED", retries_used=0, max_retries=2, budget_raised=True)
    assert raised.ok


def test_retry_exhausted() -> None:
    decision = can_retry("FAILED", retries_used=2, max_retries=2)
    assert not decision.ok
    assert decision.error_code == ErrorCode.RETRY_EXHAUSTED.value

    # 0 次上限 = 从来不允许 retry
    assert not can_retry("FAILED", retries_used=0, max_retries=0).ok


def test_budget_check_precedes_exhaustion() -> None:
    """预算未抬时，先给更可行动的原因，而不是笼统的次数耗尽。"""
    decision = can_retry("BUDGET_EXCEEDED", retries_used=9, max_retries=1)
    assert decision.error_code == ErrorCode.RETRY_BUDGET_NOT_RAISED.value


# ---------------------------------------------------------------------------
# 不变式 4：WAITING_INPUT 的进出门
# ---------------------------------------------------------------------------

def test_only_input_required_enters_waiting_input() -> None:
    entered = apply(ExecutionState.RUNNING, ExecutionEvent.INPUT_REQUIRED)
    assert entered.ok and entered.state is ExecutionState.WAITING_INPUT

    for state in ExecutionState:
        # RUNNING 是唯一的入口，WAITING_INPUT 已在上面验过，终态一律被拦。
        if state in TERMINAL_STATES or state in (
            ExecutionState.RUNNING, ExecutionState.WAITING_INPUT,
        ):
            continue
        result = apply(state, ExecutionEvent.INPUT_REQUIRED)
        assert not result.ok, state
        assert result.state is state


def test_waiting_input_exits() -> None:
    for event, expected in (
        (ExecutionEvent.INPUT_PROVIDED, ExecutionState.RUNNING),
        (ExecutionEvent.RESUME_REQUESTED, ExecutionState.RUNNING),
        (ExecutionEvent.ABORT_REQUESTED, ExecutionState.ABORTED),
    ):
        result = apply(ExecutionState.WAITING_INPUT, event)
        assert result.ok and result.state is expected, event


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "ABORT_PENDING"])
def test_resume_outside_waiting_input_is_not_waiting_input(state: str) -> None:
    result = apply(state, ExecutionEvent.RESUME_REQUESTED)
    assert not result.ok
    assert result.error_code == ErrorCode.NOT_WAITING_INPUT.value


def test_invalid_transition_keeps_state() -> None:
    result = apply("PENDING", "engine_completed")
    assert not result.ok
    assert result.error_code == ErrorCode.INVALID_TRANSITION.value
    assert result.state is ExecutionState.PENDING
