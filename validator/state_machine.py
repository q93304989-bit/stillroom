"""执行状态机（P1 协议 v1.0）。

只做一件事：把「当前状态 + 事件」映射成新状态，并给出稳定错误码。
不碰数据库、不碰时间、不碰线程 —— 纯函数，便于无头单测。

不变式（由 tests/test_state_machine.py 锁死）：

1. `ABORT_PENDING` 是正式状态，**唯一出口**是 `engine_step_ended` → `ABORTED`。
2. 终态固定为 COMPLETED / FAILED / TIMEOUT / ABORTED / BUDGET_EXCEEDED；
   进入终态后任何事件都返回 `ALREADY_TERMINAL`，不抛异常。
3. retry 无 override：COMPLETED 一律拒绝；BUDGET_EXCEEDED 默认拒绝
   （除非调用方显式抬高预算）。
4. `WAITING_INPUT` 只能由 `input_required` 进入，由 `input_provided` / `resume_requested` 离开。

状态转换只能由三类触发者发起：Engine（`engine_*` / `budget_exceeded`）、
`abort_execution`（`abort_requested`）、`resume_execution`（`resume_requested`）。
Agent 不能直接改状态 —— 它只能通过工具调用产生事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import ErrorCode


class ExecutionState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    ABORT_PENDING = "ABORT_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    ABORTED = "ABORTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


TERMINAL_STATES = frozenset({
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.TIMEOUT,
    ExecutionState.ABORTED,
    ExecutionState.BUDGET_EXCEEDED,
})


class ExecutionEvent(str, Enum):
    ENGINE_STARTED = "engine_started"
    ENGINE_STEP_ENDED = "engine_step_ended"
    ENGINE_COMPLETED = "engine_completed"
    ENGINE_FAILED = "engine_failed"
    ENGINE_TIMEOUT = "engine_timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    INPUT_REQUIRED = "input_required"
    INPUT_PROVIDED = "input_provided"
    ABORT_REQUESTED = "abort_requested"
    RESUME_REQUESTED = "resume_requested"


# 唯一的状态转移真相来源。表里没有的行 = 该状态不存在有效出口
# （终态在 apply() 里先被拦掉，所以不需要在这里出现）。
_TRANSITIONS: dict[ExecutionState, dict[ExecutionEvent, ExecutionState]] = {
    ExecutionState.PENDING: {
        ExecutionEvent.ENGINE_STARTED: ExecutionState.RUNNING,
        ExecutionEvent.ABORT_REQUESTED: ExecutionState.ABORTED,
        ExecutionEvent.ENGINE_TIMEOUT: ExecutionState.TIMEOUT,
        ExecutionEvent.BUDGET_EXCEEDED: ExecutionState.BUDGET_EXCEEDED,
    },
    ExecutionState.RUNNING: {
        ExecutionEvent.ENGINE_STEP_ENDED: ExecutionState.RUNNING,
        ExecutionEvent.ENGINE_COMPLETED: ExecutionState.COMPLETED,
        ExecutionEvent.ENGINE_FAILED: ExecutionState.FAILED,
        ExecutionEvent.ENGINE_TIMEOUT: ExecutionState.TIMEOUT,
        ExecutionEvent.BUDGET_EXCEEDED: ExecutionState.BUDGET_EXCEEDED,
        ExecutionEvent.INPUT_REQUIRED: ExecutionState.WAITING_INPUT,
        ExecutionEvent.ABORT_REQUESTED: ExecutionState.ABORT_PENDING,
    },
    ExecutionState.WAITING_INPUT: {
        ExecutionEvent.INPUT_PROVIDED: ExecutionState.RUNNING,
        ExecutionEvent.RESUME_REQUESTED: ExecutionState.RUNNING,
        ExecutionEvent.ABORT_REQUESTED: ExecutionState.ABORTED,
        ExecutionEvent.ENGINE_TIMEOUT: ExecutionState.TIMEOUT,
        ExecutionEvent.BUDGET_EXCEEDED: ExecutionState.BUDGET_EXCEEDED,
    },
    # 不变式 1：ABORT_PENDING 只有这一条出口。
    ExecutionState.ABORT_PENDING: {
        ExecutionEvent.ENGINE_STEP_ENDED: ExecutionState.ABORTED,
    },
}


@dataclass(frozen=True)
class TransitionResult:
    ok: bool
    state: ExecutionState
    error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class RetryDecision:
    ok: bool
    error_code: str | None = None
    message: str = ""


def coerce_state(value: ExecutionState | str) -> ExecutionState:
    if isinstance(value, ExecutionState):
        return value
    try:
        return ExecutionState(str(value).upper())
    except ValueError as exc:  # 未知状态名是编程错误，直接抛
        raise ValueError(f"unknown execution state: {value!r}") from exc


def coerce_event(value: ExecutionEvent | str) -> ExecutionEvent:
    if isinstance(value, ExecutionEvent):
        return value
    try:
        return ExecutionEvent(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown execution event: {value!r}") from exc


def is_terminal(state: ExecutionState | str) -> bool:
    return coerce_state(state) in TERMINAL_STATES


def allowed_events(state: ExecutionState | str) -> tuple[ExecutionEvent, ...]:
    """该状态允许的事件（终态返回空元组）。"""
    current = coerce_state(state)
    if current in TERMINAL_STATES:
        return ()
    return tuple(_TRANSITIONS.get(current, {}))


def apply(state: ExecutionState | str, event: ExecutionEvent | str) -> TransitionResult:
    """推进状态。失败时不改变 state，只给错误码。"""
    current = coerce_state(state)
    incoming = coerce_event(event)

    # 不变式 2：终态优先，任何事件都只得到 ALREADY_TERMINAL。
    if current in TERMINAL_STATES:
        return TransitionResult(
            ok=False, state=current, error_code=ErrorCode.ALREADY_TERMINAL.value,
            message=f"{current.value} is terminal",
        )

    # resume 只对 WAITING_INPUT 有意义 —— 单独给码，便于 MCP 侧原样透出。
    if incoming is ExecutionEvent.RESUME_REQUESTED and current is not ExecutionState.WAITING_INPUT:
        return TransitionResult(
            ok=False, state=current, error_code=ErrorCode.NOT_WAITING_INPUT.value,
            message=f"cannot resume: execution is {current.value}, not WAITING_INPUT",
        )

    target = _TRANSITIONS.get(current, {}).get(incoming)
    if target is None:
        allowed = ", ".join(e.value for e in allowed_events(current)) or "<none>"
        return TransitionResult(
            ok=False, state=current, error_code=ErrorCode.INVALID_TRANSITION.value,
            message=f"{current.value} + {incoming.value} is not allowed (allowed: {allowed})",
        )

    return TransitionResult(ok=True, state=target)


# ---------------------------------------------------------------------------
# retry 策略：无 override，永远复用原 input_snapshot
# ---------------------------------------------------------------------------

# 允许发起 retry 的终态。COMPLETED 与 BUDGET_EXCEEDED 单独判定，故不在此列。
_RETRYABLE_STATES = frozenset({
    ExecutionState.FAILED,
    ExecutionState.TIMEOUT,
    ExecutionState.ABORTED,
})


def can_retry(
    state: ExecutionState | str,
    *,
    retries_used: int,
    max_retries: int,
    budget_raised: bool = False,
) -> RetryDecision:
    """retry_execution 的门槛判定。

    判定顺序（先给最可行动的原因）：
    非终态 → COMPLETED → BUDGET_EXCEEDED 未抬预算 → 未归类终态 → 次数耗尽 → 允许。
    """
    current = coerce_state(state)

    if current not in TERMINAL_STATES:
        return RetryDecision(
            False, ErrorCode.NOT_TERMINAL.value,
            f"execution is {current.value}, retry requires a terminal state",
        )
    if current is ExecutionState.COMPLETED:
        return RetryDecision(
            False, ErrorCode.RETRY_NOT_ALLOWED_FOR_COMPLETED.value,
            "completed execution cannot be retried",
        )
    if current is ExecutionState.BUDGET_EXCEEDED and not budget_raised:
        return RetryDecision(
            False, ErrorCode.RETRY_BUDGET_NOT_RAISED.value,
            "budget exceeded: raise the budget before retrying",
        )
    if current not in _RETRYABLE_STATES and current is not ExecutionState.BUDGET_EXCEEDED:
        # 兜底：将来新增终态若未归类，默认不允许 retry（宁严勿松）。
        return RetryDecision(
            False, ErrorCode.NOT_TERMINAL.value,
            f"{current.value} is not retryable",
        )
    if retries_used >= max_retries:
        return RetryDecision(
            False, ErrorCode.RETRY_EXHAUSTED.value,
            f"retries used {retries_used} >= max {max_retries}",
        )
    return RetryDecision(True)
