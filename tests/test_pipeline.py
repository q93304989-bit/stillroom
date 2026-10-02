"""P1 六步顺序的权威来源与 schema 的一致性。

`validator/pipeline.py` 的 `PIPELINE_STEPS` 被两处消费（workflow 校验、内核游标推导），
所以它必须与 `schemas/workflow.schema.json` 的 `$defs.step` 逐字一致 ——
否则"能声明的工作流"与"内核愿意跑的步骤"会各说各话。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from validator.pipeline import PIPELINE_STEPS, is_step, next_step, step_index

_SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
_WORKFLOW_SCHEMA = json.loads((_SCHEMAS / "workflow.schema.json").read_text(encoding="utf-8"))
_EVENT_SCHEMA = json.loads(
    (_SCHEMAS / "execution-event.schema.json").read_text(encoding="utf-8")
)


def test_pipeline_order_matches_every_frozen_schema_enum() -> None:
    """六步名在协议里出现**三处**，必须与 `PIPELINE_STEPS` 逐字一致且顺序一致。

    - `workflow.schema.json` 的 `pipeline` 键（可声明哪些步 required/optional/disabled）
    - `workflow.schema.json` 的 `step_overrides` 键（每步的 cancellable/timeout）
    - `execution-event.schema.json` 的 `$defs.step`（事件上 `step` 字段的枚举）

    少校验一处就会出现"能声明但事件记不下"这种半截协议。
    """
    workflow = _WORKFLOW_SCHEMA["properties"]
    assert list(workflow["pipeline"]["properties"]) == list(PIPELINE_STEPS)
    assert list(workflow["step_overrides"]["properties"]) == list(PIPELINE_STEPS)
    assert _EVENT_SCHEMA["$defs"]["step"]["enum"] == list(PIPELINE_STEPS)


def test_pipeline_is_six_steps_in_the_documented_order() -> None:
    assert PIPELINE_STEPS == (
        "understand", "reference", "generate", "evaluate", "refine", "deliver",
    )


def test_next_step_walks_to_the_end_then_stops() -> None:
    walked = [PIPELINE_STEPS[0]]
    while (following := next_step(walked[-1])) is not None:
        walked.append(following)
    assert tuple(walked) == PIPELINE_STEPS
    # 最后一步之后没有下一步 —— 调用方据此决定"该交付了"
    assert next_step(PIPELINE_STEPS[-1]) is None


def test_step_index_is_zero_based() -> None:
    assert step_index("understand") == 0
    assert step_index("deliver") == len(PIPELINE_STEPS) - 1


def test_unknown_step_is_a_programming_error() -> None:
    assert is_step("understand") is True
    assert is_step("prepare") is False        # app/ 里的旧六阶段名，不属于本协议
    for call in (lambda: step_index("prepare"), lambda: next_step("prepare")):
        with pytest.raises(ValueError, match="unknown pipeline step"):
            call()
