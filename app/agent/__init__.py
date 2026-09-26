"""Agent 运行时：六步固定骨架 + 每步的类型化判断。

设计原则（v3）：**代码掌控流程，模型只做窄判断**。

- 结构固定：六个阶段按顺序走，回修有次数上限；
- 阶段内自由：每步要问什么、用什么参数，由代码根据判断结果决定；
- 判断可校验：Jev 返回选项/概率/置信度，代码据此分支（自动通过 / 请用户确认 / 不动手）；
- 越不过闸门：所有对外动作经能力注册表，参数校验、预算、审批、循环、结果验收都在那里。
"""

from app.agent.phases import PHASE_BY_KEY, PHASES, MAX_REFINE_ROUNDS, Phase
from app.agent.runtime import AgentResult, AgentRuntime, StepRecord

__all__ = [
    "AgentResult",
    "AgentRuntime",
    "StepRecord",
    "PHASES",
    "PHASE_BY_KEY",
    "MAX_REFINE_ROUNDS",
    "Phase",
]
