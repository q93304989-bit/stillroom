"""状态层：单一状态源 + 任务句柄 + 事件流。

契约（Phase 1 定形、这里落地）：

- 任何长任务都有 **Job 句柄**：可查询、可取消、有结果与错误分类；
- 状态变化一律以 **结构化事件** 广播，界面订阅而**不轮询**；
- 服务写入状态，视图读取状态，双方都不反向依赖。

`Job.parent_job_id` 现在就有，为将来的确定性工作流（一次 Run 串起多个 Step）预留。
"""

from app.state.app_state import AppState
from app.state.events import Event, EventBus
from app.state.jobs import Job, JobHandle, JobStatus

__all__ = ["AppState", "Event", "EventBus", "Job", "JobHandle", "JobStatus"]
