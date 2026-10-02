"""确定性 stub 内核（P1）。

真实六步流水线属 **P3**；P1 只要一个**确定**的推进器，让整条链路在无 UI 环境端到端可测。

## 定位：步骤引擎，不是调度器

每次 `advance()` 只让**当前步骤收尾**。谁决定「什么时候收尾」是调度器的事 ——
P1 里是本模块的便利方法 `run_to_completion()`，P3 里换成真实调度器，
而 `advance()` / `current_step()` 这套原语保持不变，测试可以整套复用。

这么切分还解决一个测试问题：`execute_workflow` 与 `abort_execution` 是两个 MCP 调用，
stdio 单线程下不会并发。若内核一次性跑完六步，「中止」只能落在步骤之间，
测试就没法精确指定在哪一步中止，A6 会退化成看运气。

## 两条硬约束

1. **`engine_step_ended` 在两种状态下含义不同**（`validator/state_machine.py` 已定义）：
   `RUNNING` 下是自环（本步跑完，继续下一步），`ABORT_PENDING` 下是**唯一出口**
   （正在跑的步骤收尾 → `ABORTED`）。stub 必须显式区分，不能"每步无脑发一次"。

2. **游标不放内存当权威**。当前步骤由事件流推导（`engine_step_ended` 的 `step`
   记录了"刚收尾的步骤"）。这样进程重启、或换一个 `StubKernel` 实例接着驱动，
   看到的都是同一步骤 —— 也不会出现"内存游标飘了但事件流没飘"的分叉。

## 每步的产物

每步都产出一对**确定性**的 `payload_ref` / `payload_hash`（哈希由
`execution_id + step` 规范化后算出，不看时钟、不看随机数），于是：

- 同一个执行重放两次得到同样的 hash，Replay 可比对；
- 顺带覆盖了事件表这两个字段的落库与 `to_document()` 出口。

注意 `payload_hash` **不上浮**到 `executions.output_ref`：只有进入 `COMPLETED` 的事件
才认交付指针（契约 §四之二），所以中止路径上这些产物只留在事件行里。
"""

from __future__ import annotations

from dataclasses import dataclass

from validator.errors import ErrorCode
from validator.pipeline import PIPELINE_STEPS, is_step, next_step
from validator.state_machine import (
    TERMINAL_STATES,
    ExecutionEvent,
    ExecutionState,
    coerce_state,
)

from .errors import KernelError
from .hashing import canonical_json, sha256_of_text
from .repository import ExecutionRepository

# 能"让当前步骤收尾"的状态。其余状态调 advance() 是内核误用。
_ADVANCEABLE = frozenset({
    ExecutionState.RUNNING,
    ExecutionState.ABORT_PENDING,
})


@dataclass(frozen=True)
class StepOutcome:
    """一次 `advance()` 的结果。"""

    step_ended: str
    """刚刚收尾的步骤。ABORT_PENDING 下它是"被打断的那个步骤"，而不是"下一步"。"""

    state: str
    """收尾后的执行状态：正常推进是 `RUNNING`，中止收尾是 `ABORTED`。"""

    next_step: str | None
    """下一个要跑的步骤；`deliver` 刚收尾、或已在终态时为 `None`。"""

    payload_ref: str
    payload_hash: str

    @property
    def aborted(self) -> bool:
        return self.state == ExecutionState.ABORTED.value


class StubKernel:
    """确定性步骤引擎。构造时只绑定一个 execution，不自己建库。"""

    def __init__(self, repo: ExecutionRepository, execution_id: str) -> None:
        self._repo = repo
        self._execution_id = execution_id

    # -- 查询 -------------------------------------------------------------

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def state(self) -> str:
        return self._repo.get(self._execution_id).state

    def current_step(self) -> str | None:
        """当前（或下一个要跑的）步骤；尚未 start 或已在终态时返回 `None`。

        完全由事件流推导，不读任何内存游标。
        """
        record = self._repo.get(self._execution_id)
        return self._derive_step(record.state, self._repo.list_events(self._execution_id))

    # -- 驱动 -------------------------------------------------------------

    def start(self) -> str:
        """`engine_started`：`PENDING → RUNNING`。返回新状态。"""
        return self._repo.append_event(
            self._execution_id, ExecutionEvent.ENGINE_STARTED.value
        ).status_after

    def advance(self) -> StepOutcome:
        """让**当前步骤**收尾一次。

        - `RUNNING` → 发 `engine_step_ended`，自环回 `RUNNING`，游标推到下一步；
        - `ABORT_PENDING` → 发 `engine_step_ended`，这是 `ABORT_PENDING` 的**唯一出口**，
          推到 `ABORTED`。**不推进游标** —— 没有"下一步"了。

        其余状态是内核误用，抛 `KernelError(INVALID_TRANSITION)`。
        """
        record = self._repo.get(self._execution_id)
        state = coerce_state(record.state)
        events = self._repo.list_events(self._execution_id)

        if state not in _ADVANCEABLE:
            raise KernelError(
                ErrorCode.INVALID_TRANSITION,
                f"cannot end a step while execution is {state.value}",
                execution_id=self._execution_id,
                state=state.value,
            )

        step = self._derive_step(state.value, events)
        if step is None:
            # RUNNING 但推导不出步骤：要么六步跑完了（该 complete 了），
            # 要么事件流缺了 step 字段（数据被改坏）。两种都不该静默开工。
            raise KernelError(
                ErrorCode.INVALID_TRANSITION,
                "no step is in flight: the six-step pipeline already finished, "
                "or the event stream is missing its step records",
                execution_id=self._execution_id,
                state=state.value,
            )

        payload_ref = self._payload_ref(step)
        payload_hash = self._payload_hash(step)
        appended = self._repo.append_event(
            self._execution_id,
            ExecutionEvent.ENGINE_STEP_ENDED.value,
            step=step,
            payload_ref=payload_ref,
            payload_hash=payload_hash,
        )
        new_state = coerce_state(appended.status_after)
        return StepOutcome(
            step_ended=step,
            state=new_state.value,
            next_step=(
                None if new_state is ExecutionState.ABORTED else next_step(step)
            ),
            payload_ref=payload_ref,
            payload_hash=payload_hash,
        )

    def complete(self) -> str:
        """`engine_completed`：`RUNNING → COMPLETED`，本步产物成为交付指针。

        刻意**不校验"六步都跑完了"**：`pipeline` 里可以标 `disabled`
        （见 `schemas/workflow.schema.json`），所以运行期实际步数不一定是六步，
        "该交付了"是定义与调度器的判断。内核只严格执行状态机允许的转移，
        不去发明一条 P3 必须删掉的假不变式。
        """
        return self._repo.append_event(
            self._execution_id,
            ExecutionEvent.ENGINE_COMPLETED.value,
            payload_ref=self._payload_ref(self._last_step()),
            payload_hash=self._payload_hash(self._last_step()),
        ).status_after

    def fail(self, error_code: str = ErrorCode.SEMANTIC_INVALID.value, *, message: str | None = None) -> str:
        """`engine_failed`：`RUNNING → FAILED`。"""
        return self._repo.append_event(
            self._execution_id,
            ExecutionEvent.ENGINE_FAILED.value,
            error_code=error_code,
            message=message,
        ).status_after

    def abort(self) -> str:
        """`abort_requested`。三种落点，全由状态机决定：

        - `PENDING` → `ABORTED`（还没有步骤在跑，即时生效，没有两段式）
        - `RUNNING` → `ABORT_PENDING`（等当前步骤自己收尾，**不掐断**）
        - `WAITING_INPUT` → `ABORTED`
        - 终态 → `RepositoryError(ALREADY_TERMINAL)`
        """
        return self._repo.append_event(
            self._execution_id, ExecutionEvent.ABORT_REQUESTED.value
        ).status_after

    def run_to_completion(self) -> str:
        """便利调度器：一路驱动到终态，返回终态。

        P1 的临时调度器（P3 会被真实调度器替换，`advance()` 原语不变）。
        遇到 `ABORT_PENDING` 会**先让当前步骤收尾再停在 `ABORTED`** ——
        这正是"中止不掐断步骤"的落点。
        """
        state = coerce_state(self.state)
        if state is ExecutionState.PENDING:
            state = coerce_state(self.start())

        # 六步 + 收尾 + start，留足余量；撞上限说明推导逻辑坏了，宁可炸。
        for _ in range(2 * len(PIPELINE_STEPS) + 4):
            if state in TERMINAL_STATES:
                return state.value
            if state is ExecutionState.ABORT_PENDING:
                return self.advance().state
            if state is not ExecutionState.RUNNING:
                return state.value
            if self.current_step() is None:
                return self.complete()
            state = coerce_state(self.advance().state)

        raise KernelError(
            ErrorCode.INVALID_TRANSITION,
            "run_to_completion did not reach a terminal state within the step budget",
            execution_id=self._execution_id,
            state=state.value,
        )

    # -- 内部 -------------------------------------------------------------

    def _derive_step(self, state: str, events) -> str | None:
        """由事件流推导当前步骤。

        规则：`step` 字段只在 `engine_step_ended` 上意味着"**刚收尾的步骤**"，
        所以当前步骤 = 最后一个收尾步骤的下一步。终态一律 `None`。
        """
        current = coerce_state(state)
        if current in TERMINAL_STATES or current is ExecutionState.PENDING:
            return None

        ended = [
            event.step
            for event in events
            if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value
        ]
        if not ended:
            # 已 start，但还没有任何步骤收尾 → 跑第一步
            return PIPELINE_STEPS[0]

        last = ended[-1]
        if last is None or not is_step(last):
            raise KernelError(
                ErrorCode.INVALID_TRANSITION,
                f"last engine_step_ended has no usable step record ({last!r}); "
                "the event stream cannot be replayed",
                execution_id=self._execution_id,
                step=last,
            )
        return next_step(last)

    def _last_step(self) -> str:
        """最后一次收尾的步骤；没有就取第一步（用于算出交付产物的 ref/hash）。"""
        events = self._repo.list_events(self._execution_id)
        ended = [
            event.step
            for event in events
            if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value
        ]
        if not ended:
            return PIPELINE_STEPS[-1]
        last = ended[-1]
        if last is None or not is_step(last):
            raise KernelError(
                ErrorCode.INVALID_TRANSITION,
                f"last engine_step_ended has no usable step record ({last!r})",
                execution_id=self._execution_id,
                step=last,
            )
        return last

    def _payload_ref(self, step: str) -> str:
        return f"artifacts/{self._execution_id}/{step}.json"

    def _payload_hash(self, step: str) -> str:
        """确定性：只依赖 `execution_id` 与步骤名，不看时钟、不看随机数。"""
        return sha256_of_text(
            canonical_json({"execution_id": self._execution_id, "step": step})
        )


__all__ = ["StubKernel", "StepOutcome"]
