"""六步流水线的**权威顺序**（P1 协议 v1.0）。

放在 `validator/` 而不住在 `workflow_validator.py`，是因为它有两个消费者：

- `workflow_validator` —— 校验 workflow definition 的 `pipeline` 声明；
- `runtime/stub_kernel` —— 推导「当前跑第几步」的游标。

两边共用一个元组，顺序才不会各说各话。顺序本身是协议的一部分，
和 `schemas/workflow.schema.json` 的 `$defs.step` 枚举必须逐字一致
（`tests/test_pipeline.py` 锁死这条一致性）。
"""

from __future__ import annotations

PIPELINE_STEPS: tuple[str, ...] = (
    "understand",
    "reference",
    "generate",
    "evaluate",
    "refine",
    "deliver",
)

_STEP_INDEX = {name: index for index, name in enumerate(PIPELINE_STEPS)}


def is_step(name: str) -> bool:
    return name in _STEP_INDEX


def step_index(name: str) -> int:
    """第几步（0 起）。未知步骤名是编程错误，直接 `ValueError`。"""
    try:
        return _STEP_INDEX[name]
    except KeyError as exc:
        raise ValueError(f"unknown pipeline step: {name!r}") from exc


def next_step(name: str) -> str | None:
    """下一步；已经是最后一步则返回 `None`（调用方据此决定"该交付了"）。"""
    index = step_index(name)
    if index + 1 >= len(PIPELINE_STEPS):
        return None
    return PIPELINE_STEPS[index + 1]


__all__ = ["PIPELINE_STEPS", "is_step", "step_index", "next_step"]
