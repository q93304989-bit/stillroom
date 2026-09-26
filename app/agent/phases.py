"""六个固定阶段：每阶段只暴露该阶段允许的工具。

「有流程顺序」在这里落地：阶段之间按顺序推进，阶段内部由代码根据判断结果决定细节。
每阶段带一份工具白名单，由 `PhaseGate` 中间件在注册表层强制执行——
所以「生成阶段够不到图床」是物理上做不到，而不是靠提示词叮嘱。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 一次运行里最多回修几次（防止无限重做，也控制成本）
MAX_REFINE_ROUNDS = 2


@dataclass(frozen=True)
class Phase:
    key: str
    title: str
    goal: str
    tools: tuple[str, ...]


PHASES: tuple[Phase, ...] = (
    Phase(
        key="understand",
        title="理解需求",
        goal="把一句话变成结构化需求；含糊就先追问，别急着生成",
        tools=("judge.ask",),
    ),
    Phase(
        key="prepare",
        title="找参考",
        goal="从历史作品、用户知识库、联网里找可用的参考（没有也要能继续）",
        tools=("rag.search", "kb.search", "web.search", "web.fetch_image"),
    ),
    Phase(
        key="generate",
        title="生成",
        goal="按需求出图；受平台限额与预算约束",
        tools=("image.generate", "video.submit"),
    ),
    Phase(
        key="evaluate",
        title="评估",
        goal="先让视觉模型描述，再让 Jev 判断是否达标；不确定就请示用户",
        tools=("vision.describe", "judge.ask"),
    ),
    Phase(
        key="refine",
        title="精修",
        goal="按判断结果改写提示词后重新生成（最多两轮）",
        tools=("llm.chat", "image.generate", "video.submit"),
    ),
    Phase(
        key="deliver",
        title="交付",
        goal="结果落库并带上评估，交由用户处置",
        tools=(),
    ),
)

PHASE_BY_KEY: dict[str, Phase] = {phase.key: phase for phase in PHASES}


def tools_of(phase_key: str) -> tuple[str, ...]:
    phase = PHASE_BY_KEY.get(phase_key)
    return phase.tools if phase else ()
