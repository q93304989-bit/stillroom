"""任务句柄：长任务的可查询、可取消外壳。

替换旧版的 `_cancel_requested` 布尔标志——那种做法只能「丢弃结果」，任务本身仍在跑，
界面也无从知道它到底处于什么阶段。这里每个任务都有：

    id / tool / params / status / attempts / 时间戳 / 结果 / 错误分类 / 产物

并预留 `parent_job_id`：未来的确定性工作流里，一个 Run 的多个 Step 就是一批有共同父节点的 Job。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED)


def new_job_id() -> str:
    return "job-" + uuid.uuid4().hex[:12]


@dataclass
class Job:
    """一次工具调用的完整状态。"""

    tool: str
    params: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_job_id)
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    progress: float | None = None
    message: str = ""
    result: Any = None
    error: str = ""
    error_kind: str = ""
    artifacts: list[str] = field(default_factory=list)
    #: 一次运行内共享的上下文（预算计数、调用历史、已批准清单），闸门与流水线都用它
    context: dict = field(default_factory=dict)
    idempotency_key: str = ""
    parent_job_id: str | None = None      # 为将来的 Run/Step 预留

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "status": self.status.value,
            "params": dict(self.params),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "attempts": self.attempts,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "error_kind": self.error_kind,
            "artifacts": list(self.artifacts),
            "idempotency_key": self.idempotency_key,
            "parent_job_id": self.parent_job_id,
        }


class JobHandle:
    """任务句柄：`cancel()` 取消，`wait()` 等结果，属性即时反映 Job 状态。"""

    def __init__(self, job: Job, task: asyncio.Task) -> None:
        self._job = job
        self._task = task

    # ---------------------------------------------------------------- 只读视图

    @property
    def job(self) -> Job:
        return self._job

    @property
    def id(self) -> str:
        return self._job.id

    @property
    def status(self) -> JobStatus:
        return self._job.status

    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def result(self) -> Any:
        return self._job.result

    @property
    def error(self) -> str:
        return self._job.error

    def to_dict(self) -> dict[str, Any]:
        return self._job.to_dict()

    # ---------------------------------------------------------------- 控制

    def cancel(self) -> None:
        """请求取消。协作式：正在等待（sleep / 网络）的任务会立刻中断。

        注意：若任务已经提交到服务端（视频），服务端那份仍会跑完——我们能做的是停止
        等待并把状态标为已取消，这与「假装它不存在」有本质区别。
        """
        if not self._task.done():
            self._task.cancel()

    async def wait(self) -> Job:
        """等任务结束并返回 Job；取消异常在这里被消化成 `status = canceled`。"""
        try:
            await self._task
        except asyncio.CancelledError:
            if self._job.status is not JobStatus.CANCELED:
                self._job.status = JobStatus.CANCELED
                self._job.finished_at = self._job.finished_at or time.time()
        return self._job
