"""A9 · 端到端（`docs/P1-实施计划.md` §二 A9.1–A9.9）。

## 这条链路到底在测什么

把「无头边界」（A1）与「工具可用」（A2–A8）之外**唯一剩下的那件事**测掉：
一个走 stdio 的调用方，能不能按契约把一次执行从头带到尾。

它测的是**接缝**，不是任何单个模块：

| 接缝 | 单模块测试看不到它的原因 |
|---|---|
| wire（`tools/call`）↔ 工具 ↔ runtime | 各层各自绿，拼起来可能是错的 |
| `payload_ref` 落点 ↔ 事件流折叠 | kernel 只写事件，`output_ref` 由仓库折叠决定 —— 两边各测各的 |
| 幂等锚点 ↔ 状态缓存 | 「同一个 request_id」在仓库层与工具层的含义不同 |

## 两处刻意的写法

1. **全程走 `tools/call`**：wire 上没有 11 个方法，工具名只出现在 `params.name` 里。
   直接调 `tools.execute_workflow(...)` 会绕过 `mcp.py` 的 `isError` 归一化，
   而"错误变成 `isError` 而不是 `error`"正是客户端要依赖的东西。
2. **推进六步用 `StubKernel`**：`execute_workflow` 只做绑定（创建 `PENDING`），
   **不推进**。谁决定"什么时候收尾"是调度器的事，P1 的调度器就是
   `StubKernel.run_to_completion()`（见 `runtime/stub_kernel.py` 的模块说明）。
   这不是绕过 MCP，而是 P1 有意切出来的一条边 —— 内核一次性跑完六步的话，
   中止只能落在步骤之间，A6 会退化成看运气。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from mcp_server import stdio
from runtime import KernelError, StubKernel
from runtime.registry import STATUS_ACTIVE
from validator.errors import ErrorCode
from validator.pipeline import PIPELINE_STEPS
from validator.state_machine import (
    TERMINAL_STATES,
    ExecutionEvent,
    ExecutionState,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workflow"
EXIT_OK = 0

#: `executions.execution_id` 的形态（`repository._new_id`）。
_EXECUTION_ID = re.compile(r"^exec_[0-9a-f]{16}$")


# ---------------------------------------------------------------------------
# 会话助手：一条帧进，一条帧出
# ---------------------------------------------------------------------------

def _definition(name: str = "valid_full.json") -> dict[str, Any]:
    """六步全 required 的 fixture —— A9.3 要按 `PIPELINE_STEPS` 走满。

    不用 `valid_minimal.json` 的理由：它把 reference / evaluate / refine 标成
    `disabled`，而 stub 内核**不读 `disabled`**（`StubKernel.complete()` 的说明：
    "刻意不校验六步都跑完了"）。用 minimal 能过，但那会让"六步按序收尾"
    这条断言看起来像是靠 fixture 凑出来的。
    """
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _context(tmp_path: Path, *, identity: str = "human"):
    """`human` 而不是 `agent`：`valid_full` 声明了 `file.read` / `file.write`，
    而 `agent` 预设只有 `llm.call`（最小权限），L3 会拒。

    身份取值本身由 `test_mcp_stdio.py` 与契约机器比对，这里只是挑一个够用的。
    """
    return stdio.build_server_context(
        db_path=tmp_path / "protocol.db", identity=identity, log=lambda _m: None
    )


def _call(ctx, tool: str, arguments: dict[str, Any] | None = None, *, request_id: int = 1) -> dict[str, Any]:
    """经 wire 调一次工具，返回 MCP 的 `result` 对象。

    一次调用一条帧（喂完即 EOF）—— 每次都是完整的 `serve()` 循环，
    拿到的一定是这一条的响应。**不假设服务会保持会话**：
    P1 的 stdio 是"来一条答一条"，靠 `ctx` 而不是靠内存态维持连续性。
    """
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
         "params": {"name": tool, "arguments": {} if arguments is None else arguments}},
        ensure_ascii=False,
    )
    out = io.StringIO()
    code = stdio.run(ctx, reader=io.StringIO(frame + "\n"), writer=out, log=lambda _m: None)
    assert code == EXIT_OK, f"serve 退出码 {code}"

    response = json.loads(out.getvalue().strip())
    assert response["id"] == request_id
    # 业务失败**不许**走 JSON-RPC 的 error 字段（契约 §五.7）——
    # 否则客户端分不清"工具返回了错误"与"方法/参数就不对"。
    assert "error" not in response, response
    return response["result"]


def _ok(ctx, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """成功路径 → 业务 `data`。"""
    result = _call(ctx, tool, arguments)
    assert result["isError"] is False, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True, payload
    return payload["data"]


def _rejected(ctx, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """业务失败路径 → 结构化 `error` 文档，**且不是 JSON-RPC 错误**。"""
    result = _call(ctx, tool, arguments)
    assert result["isError"] is True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False, payload
    return payload["error"]


def _bound_execution(
    tmp_path: Path,
    *,
    identity: str = "human",
    fixture: str = "valid_full.json",
    request_id: str = "req-a9",
    input_snapshot: dict[str, Any] | None = None,
):
    """建定义 → 激活 → 绑定一次执行，返回 `(ctx, definition, execution_id)`。

    到这一步为止执行是 `PENDING`（`execute_workflow` 只绑定，不推进）。
    """
    ctx = _context(tmp_path, identity=identity)
    definition = _definition(fixture)
    created = _ok(ctx, "create_workflow", {"workflow": definition, "activate": True})
    assert created["status"] == STATUS_ACTIVE, created

    started = _ok(ctx, "execute_workflow", {
        "workflow_id": definition["workflow_id"],
        "version": definition["version"],
        "request_id": request_id,
        "input": input_snapshot if input_snapshot is not None else {"topic": "stillroom"},
    })
    return ctx, definition, started["execution_id"]


def _ended_steps(ctx, execution_id: str) -> list[str]:
    return [
        event.step
        for event in ctx.repo.list_events(execution_id)
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value
    ]


def _store_files(root: Path) -> set[str]:
    """store 里所有文件的**相对路径**（内容寻址布局是 `sha256/<前两位>/<hex>`）。"""
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


# ---------------------------------------------------------------------------
# A9.1 一条链走通，execution_id 全程同一个
# ---------------------------------------------------------------------------

def test_a9_1_the_chain_runs_end_to_end_on_one_execution_id(tmp_path: Path) -> None:
    """`create_workflow → match_workflow → execute_workflow → get_execution_status`。

    断言的是"这四个工具**能串起来**"，而不是四个工具各自对不对 ——
    各自的正确性由 `test_mcp_tools.py` 负责。串不起来最常见的形态是
    中间某一步返回的标识与下一步要的标识不是同一个（`workflow_id` 与
    `workflow_version`、`request_id` 与 `execution_id`）。
    """
    ctx = _context(tmp_path)
    definition = _definition()

    created = _ok(ctx, "create_workflow", {"workflow": definition, "activate": True})
    assert created["workflow_id"] == definition["workflow_id"]
    assert created["version"] == definition["version"]
    assert created["status"] == STATUS_ACTIVE

    matched = _ok(ctx, "match_workflow", {"intent": "写一篇图文并茂的短文"})
    assert matched == {"match": "none", "candidates": []}, "P1 的合法出口（决策 #7）"

    started = _ok(ctx, "execute_workflow", {
        "workflow_id": definition["workflow_id"],
        "version": definition["version"],
        "request_id": "req-chain",
        "input": {"topic": "stillroom"},
    })
    assert started["is_duplicate"] is False
    assert started["status"] == ExecutionState.PENDING.value
    execution_id = started["execution_id"]
    assert _EXECUTION_ID.match(execution_id), execution_id

    status = _ok(ctx, "get_execution_status", {"execution_id": execution_id})
    assert status["status"] == ExecutionState.PENDING.value
    # `PENDING` 下**没有**在跑的步骤 —— `current_step` 是 `None`，不是第一步。
    # 报第一步会读成"已经在跑了"，与 `status: PENDING` 自相矛盾。
    assert status["current_step"] is None
    assert status["error"] is None

    # "同一个"这条断言**必须回到库里去证**：光看两次工具返回的字符串，
    # 若两次都就地生成了新 id，也是"相同形态"——但它已经不是同一次执行了。
    assert ctx.repo.get_by_request("req-chain").execution_id == execution_id
    assert ctx.repo.get(execution_id).workflow_id == definition["workflow_id"]
    assert ctx.repo.get(execution_id).workflow_version == definition["version"]


# ---------------------------------------------------------------------------
# A9.2 报出来的状态 ≡ 事件流重放出的状态
# ---------------------------------------------------------------------------

def test_a9_2_every_reported_status_matches_the_replayed_event_stream(tmp_path: Path) -> None:
    """A5 的 e2e 版：**每一次**查询都要与重放一致。

    只在终态比一次是不够的 —— `state` 是缓存列，中间态更容易与事件流分叉
    （终态往往被反复观测，中间态不会）。所以下面在每个拐点都比一次。
    同时比 `verify_consistency()`：它是仓库自己的"缓存列 ⇄ 事件流"校验器，
    与 `replay_state()` 是**两条独立路径**，一起用才排除了"两边一起错"。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    kernel = StubKernel(ctx.repo, execution_id)

    seen: list[str] = []

    def check(label: str) -> None:
        reported = _ok(ctx, "get_execution_status", {"execution_id": execution_id})["status"]
        assert reported == ctx.repo.replay_state(execution_id), f"{label}: 缓存与重放不一致"
        assert ctx.repo.verify_consistency(execution_id).state == reported, label
        seen.append(reported)

    check("绑定后")
    kernel.start()
    check("engine_started 后")
    kernel.advance()
    check("第一步收尾后")
    kernel.run_to_completion()
    check("收尾后")

    assert seen == [
        ExecutionState.PENDING.value,
        ExecutionState.RUNNING.value,
        ExecutionState.RUNNING.value,
        ExecutionState.COMPLETED.value,
    ], seen


# ---------------------------------------------------------------------------
# A9.3 六步按 PIPELINE_STEPS 顺序收尾 → COMPLETED
# ---------------------------------------------------------------------------

def test_a9_3_the_six_steps_end_in_pipeline_order_and_complete(tmp_path: Path) -> None:
    """六步的顺序是**协议的一部分**（`validator/pipeline.py` 与
    `workflow.schema.json` 的枚举逐字一致，`test_pipeline.py` 锁死）。

    断言"恰好这六个、恰好这个顺序"——不是"至少跑了几个"。少一步或多一步
    都说明游标推导坏了，而那时状态可能还是 `COMPLETED`（内核不校验步数）。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    kernel = StubKernel(ctx.repo, execution_id)

    # 还没 `start` 时游标是 `None`（`PENDING` 没有在跑的步骤）——
    # 第一步要等 `engine_started` 之后才出现。
    assert kernel.current_step() is None
    kernel.start()
    assert kernel.current_step() == PIPELINE_STEPS[0]

    final = kernel.run_to_completion()

    assert final == ExecutionState.COMPLETED.value
    assert _ended_steps(ctx, execution_id) == list(PIPELINE_STEPS)

    status = _ok(ctx, "get_execution_status", {"execution_id": execution_id})
    assert status["status"] == ExecutionState.COMPLETED.value
    assert status["current_step"] is None
    assert [step["status"] for step in status["steps"]] == ["done"] * len(PIPELINE_STEPS)


# ---------------------------------------------------------------------------
# A9.4 交付指针 = 最后一步的 payload_ref
# ---------------------------------------------------------------------------

def test_a9_4_the_delivery_pointer_is_the_last_steps_payload(tmp_path: Path) -> None:
    """`output_ref` 只认**进入 `COMPLETED`** 的那个事件的 `payload_ref`。

    这条同时钉住两件事：**取的是最后一步**（`deliver`），以及**不上浮中间产物** ——
    中间五步的 `payload_ref` 都在事件行里，一个都不许漏进 `output_ref`。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    StubKernel(ctx.repo, execution_id).run_to_completion()

    record = ctx.repo.get(execution_id)
    last = PIPELINE_STEPS[-1]
    assert record.output_ref == f"artifacts/{execution_id}/{last}.json"
    assert record.output_ref == ctx.repo.replay(execution_id).output_ref

    completed = [
        event for event in ctx.repo.list_events(execution_id)
        if event.type == ExecutionEvent.ENGINE_COMPLETED.value
    ]
    assert len(completed) == 1
    assert completed[0].payload_ref == record.output_ref
    assert completed[0].payload_hash is not None, "内容指纹必须一起落库，否则重放没法比对"

    # 中间步骤的产物**只**留在事件行里。
    intermediate = {
        event.payload_ref
        for event in ctx.repo.list_events(execution_id)
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value
        and event.step != last
    }
    assert intermediate, "中间步骤一个产物都没有 —— 这条断言会变成空转"
    assert record.output_ref not in intermediate


# ---------------------------------------------------------------------------
# A9.5 不伪造 manifest（负向）
# ---------------------------------------------------------------------------

def test_a9_5_no_artifact_manifest_is_fabricated(tmp_path: Path) -> None:
    """**这条是负向断言，也是本阶段最容易"好心办坏事"的地方。**

    `schemas/artifact-manifest.schema.json` 的 `required` 里有 `execution_id`
    与 `step`，而那是**执行产物**的清单。P1 的内核只产 `payload_ref` 指针
    （`artifacts/<execution_id>/<step>.json`）—— 那些路径**没有对应的真实文件**，
    它们是"内容地址的占位"，由将来的 P3 流水线填充。

    如果为了"看起来完整"而往 store 里写一份 manifest，或让
    `get_execution_status().artifacts` 报几条编出来的条目，就等于对一个
    从未被验证的产物清单背书。决策 #6 已经为工作流定义做过同样的选择
    （不为定义伪造 manifest），这里沿用。
    """
    ctx, definition, execution_id = _bound_execution(tmp_path)
    StubKernel(ctx.repo, execution_id).run_to_completion()

    status = _ok(ctx, "get_execution_status", {"execution_id": execution_id})
    assert status["artifacts"] == [], "P1 没有真的产物，不许报假条目"

    # store 里**只该有**工作流定义那一个 blob —— 由 registry 的 definition_ref 指认，
    # 不是"数一下有几个文件"（那数不出"多出来的是不是 manifest"）。
    record = ctx.registry.require_active(definition["workflow_id"], definition["version"])
    assert record.definition_ref is not None
    expected = {ctx.artifacts.path_for(ctx.artifacts.parse_ref(record.definition_ref))
                .relative_to(ctx.artifacts.root).as_posix()}

    files = _store_files(ctx.artifacts.root)
    assert files == expected, f"store 里多出了不该有的东西：{files - expected}"
    assert not any("manifest" in path for path in files)


# ---------------------------------------------------------------------------
# A9.6 幂等
# ---------------------------------------------------------------------------

def test_a9_6_repeating_the_request_id_reuses_one_execution(tmp_path: Path) -> None:
    """在**执行已到终态之后**再发一次同样的 `request_id`。

    挑这个时点是有意的：`PENDING` 时重复调用，"返回同一个 id"可能只是因为
    两次都还没跑；跑完之后再发，才真正考验"绑定是永久的、且返回的是**当前**状态"。
    """
    ctx, definition, execution_id = _bound_execution(tmp_path, request_id="req-idem")
    StubKernel(ctx.repo, execution_id).run_to_completion()

    again = _ok(ctx, "execute_workflow", {
        "workflow_id": definition["workflow_id"],
        "version": definition["version"],
        "request_id": "req-idem",
        "input": {"topic": "stillroom"},
    })

    assert again["execution_id"] == execution_id
    assert again["is_duplicate"] is True
    # 报的是**当前**状态而不是 `PENDING` —— 报 `PENDING` 会让客户端
    # 以为"刚起了一个新的"，于是一路等一个永远不会到的推进。
    assert again["status"] == ExecutionState.COMPLETED.value
    assert "input_mismatch" not in again, "同样的 input 不该被标成不匹配"

    rows = ctx.repo._conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    assert rows == 1, f"同一 request_id 落了 {rows} 行"


def test_a9_6b_the_same_request_id_with_a_different_input_is_flagged(tmp_path: Path) -> None:
    """同一个 `request_id` 配不同 `input`：**仍然返回既有的 execution**（契约要求），
    但必须把"不匹配"这个事实带出来 —— 静默复用会让调用方以为新输入生效了。
    """
    ctx, definition, execution_id = _bound_execution(
        tmp_path, request_id="req-idem-2", input_snapshot={"topic": "first"}
    )

    again = _ok(ctx, "execute_workflow", {
        "workflow_id": definition["workflow_id"],
        "version": definition["version"],
        "request_id": "req-idem-2",
        "input": {"topic": "second"},
    })

    assert again["execution_id"] == execution_id
    assert again["is_duplicate"] is True
    assert again["input_mismatch"] is True
    assert ctx.repo.get(execution_id).input_snapshot == {"topic": "first"}, "原快照不可变"


# ---------------------------------------------------------------------------
# A9.7 中止两段式
# ---------------------------------------------------------------------------

def test_a9_7_abort_is_two_phase(tmp_path: Path) -> None:
    """`RUNNING` 下中止 → `ABORT_PENDING`（**不是** `ABORTED`），
    等当前步骤自己收尾 → `ABORTED`。

    两段式的意义在"不掐断当前步骤"：一个已经发出去的模型调用没法撤回，
    硬把它标记成"已中止"只会让产物与状态对不上。所以先挂起、再在
    步骤边界收敛 —— 这正是 `ABORT_PENDING` **只有一条出口**的理由。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    kernel = StubKernel(ctx.repo, execution_id)

    kernel.start()
    assert kernel.current_step() == PIPELINE_STEPS[0]

    requested = _ok(ctx, "abort_execution", {"execution_id": execution_id, "reason": "用户取消"})
    assert requested["status"] == ExecutionState.ABORT_PENDING.value
    assert kernel.state == ExecutionState.ABORT_PENDING.value, "中止请求不许就地掐断"
    assert _ok(ctx, "get_execution_status", {"execution_id": execution_id})["status"] == \
        ExecutionState.ABORT_PENDING.value

    outcome = kernel.advance()
    assert outcome.state == ExecutionState.ABORTED.value
    assert outcome.step_ended == PIPELINE_STEPS[0], "收尾的是**被打断的那一步**"
    assert outcome.next_step is None, "ABORTED 没有下一步"

    status = _ok(ctx, "get_execution_status", {"execution_id": execution_id})
    assert status["status"] == ExecutionState.ABORTED.value
    assert status["status"] in TERMINAL_STATES


def test_a9_7b_abort_pending_has_exactly_one_exit(tmp_path: Path) -> None:
    """`ABORT_PENDING` 没有第二条出口 —— A6 的核心不变式。

    第二段收尾之后（已 `ABORTED`）再 `advance()` 必须炸，而不是"再收尾一次"
    或"悄悄推到下一步"。`ABORT_PENDING` 一旦有第二条出路，
    "中止"就变成了"看哪条路先到"。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    _ok(ctx, "abort_execution", {"execution_id": execution_id})
    kernel.advance()

    with pytest.raises(KernelError) as caught:
        kernel.advance()
    assert caught.value.code == ErrorCode.INVALID_TRANSITION.value
    assert kernel.state == ExecutionState.ABORTED.value


def test_a9_7c_abort_before_start_is_immediate(tmp_path: Path) -> None:
    """`PENDING` 下中止**没有**两段式：还没有步骤在跑，即时 `ABORTED`。

    与 A9.7 互为反证：如果两段式被写成"中止一律先 `ABORT_PENDING`"，
    这条会红 —— 而那时一个从未启动的执行会挂在一个永远等不到收尾的状态上。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)

    requested = _ok(ctx, "abort_execution", {"execution_id": execution_id})

    assert requested["status"] == ExecutionState.ABORTED.value
    assert ctx.repo.get(execution_id).state == ExecutionState.ABORTED.value


# ---------------------------------------------------------------------------
# A9.8 retry：新 execution 带 parent、复用同 request_id、无 override
# ---------------------------------------------------------------------------

def test_a9_8_retry_creates_a_linked_execution_without_override(tmp_path: Path) -> None:
    """`FAILED` → `retry_execution` → 新 `execution_id`。

    三条一起断，少一条都会被"看着像重试"蒙过去：
    新 id 必须带 `parent_execution_id`；`request_id` 必须**复用**（否则幂等锚点断了）；
    原绑定必须仍然指向首次那个执行（`get_by_request`）。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path, request_id="req-retry")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    assert kernel.fail(ErrorCode.SEMANTIC_INVALID.value, message="stub failure") == \
        ExecutionState.FAILED.value

    retried = _ok(ctx, "retry_execution", {"execution_id": execution_id, "request_id": "req-retry"})

    assert retried["execution_id"] != execution_id
    assert retried["parent_execution_id"] == execution_id
    assert retried["status"] == ExecutionState.PENDING.value

    fresh = ctx.repo.get(retried["execution_id"])
    assert fresh.request_id == "req-retry", "重试必须复用 request_id"
    assert fresh.attempt == 1
    assert fresh.root_execution_id == ctx.repo.get(execution_id).root_execution_id
    assert fresh.input_snapshot == ctx.repo.get(execution_id).input_snapshot, "输入不可 override"

    # 幂等锚点没被重试改写 —— 仍然指向第一次那个。
    assert ctx.repo.get_by_request("req-retry").execution_id == execution_id
    assert len(ctx.repo.list_attempts("req-retry")) == 2


def test_a9_8b_retry_refuses_a_foreign_request_id(tmp_path: Path) -> None:
    """拿别人的 `request_id` 去重试 → 拒。

    允许的话，重试就能把一次执行挂到**另一个**幂等锚点上，
    于是那个锚点的"永远同一个 execution"随之失效。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path, request_id="req-owner")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    kernel.fail(ErrorCode.SEMANTIC_INVALID.value)

    error = _rejected(ctx, "retry_execution", {
        "execution_id": execution_id, "request_id": "req-someone-else",
    })

    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert len(ctx.repo.list_attempts("req-owner")) == 1, "被拒的 retry 不许留下 attempt"


# ---------------------------------------------------------------------------
# A9.9 resume：补输入离开 WAITING_INPUT；被拒的 resume 不留副作用
# ---------------------------------------------------------------------------

def _waiting_input(ctx, execution_id: str) -> StubKernel:
    """把执行推到 `WAITING_INPUT`：start → `input_required`。

    `input_required` 不是 `StubKernel` 的能力（内核是步骤引擎，不是调度器），
    所以这一步用仓库的事件入口直接发 —— 和 `tools/prove_mcp_server.py`
    造边界状态的手法一致：`WAITING_INPUT` 只能由它进入。
    """
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    ctx.repo.append_event(execution_id, ExecutionEvent.INPUT_REQUIRED.value, step=PIPELINE_STEPS[1])
    assert ctx.repo.get(execution_id).state == ExecutionState.WAITING_INPUT.value
    return kernel


def test_a9_9_resume_leaves_waiting_input_with_the_supplied_input(tmp_path: Path) -> None:
    """补上输入后离开 `WAITING_INPUT`，且**这份输入真的落了盘**。

    补上来的输入走内容寻址 store，事件只带 `payload_ref` / `payload_hash` ——
    `input_snapshot` 是不可变的（幂等与重放都依赖），恢复输入只能作为
    事件载荷**追加**。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    _waiting_input(ctx, execution_id)

    before = _store_files(ctx.artifacts.root)
    supplied = {"answer": "补充的材料"}

    resumed = _ok(ctx, "resume_execution", {"execution_id": execution_id, "input": supplied})

    assert resumed["status"] == ExecutionState.RUNNING.value
    assert ctx.repo.get(execution_id).state == ExecutionState.RUNNING.value

    after = _store_files(ctx.artifacts.root)
    assert len(after - before) == 1, "补上来的输入该落一份新 blob"

    event = ctx.repo.list_events(execution_id)[-1]
    assert event.type == ExecutionEvent.RESUME_REQUESTED.value
    assert event.payload_ref is not None
    assert ctx.artifacts.get_json(ctx.artifacts.parse_ref(event.payload_ref)) == supplied

    # 恢复输入**不污染交付指针**：折叠规则只认进入 `COMPLETED` 的事件。
    assert ctx.repo.get(execution_id).output_ref is None
    assert ctx.repo.verify_consistency(execution_id).state == ExecutionState.RUNNING.value


def test_a9_9b_a_rejected_resume_leaves_no_artifact_behind(tmp_path: Path) -> None:
    """**这条断言的是副作用，不是错误码** —— 因为错误码压根不由那个检查决定。

    `resume_requested` 在非 `WAITING_INPUT` 下本来就由状态机报
    `NOT_WAITING_INPUT`（`validator/state_machine.py` 有个专门分支），
    所以把 `resume_execution` 里的前置状态检查拿掉，**错误码一模一样**。
    差别只在：没有前置检查时，一次**被拒绝**的调用会先往 store 落一份
    永远没人引用的 artifact，再去撞状态机。

    断言错误码是**假通过**（它恒成立）；只有数 store 里的文件才抓得住这个失守。
    """
    ctx, _definition_doc, execution_id = _bound_execution(tmp_path)
    StubKernel(ctx.repo, execution_id).start()          # RUNNING，不是 WAITING_INPUT
    assert ctx.repo.get(execution_id).state == ExecutionState.RUNNING.value

    before = _store_files(ctx.artifacts.root)
    error = _rejected(ctx, "resume_execution", {
        "execution_id": execution_id, "input": {"answer": "没人要的输入"},
    })

    assert error["code"] == ErrorCode.NOT_WAITING_INPUT.value
    assert _store_files(ctx.artifacts.root) == before, "被拒的调用留下了没人引用的 artifact"
    assert ctx.repo.get(execution_id).state == ExecutionState.RUNNING.value, "被拒不许改状态"


# ---------------------------------------------------------------------------
# 收尾：整条链跑完后，库仍然自洽
# ---------------------------------------------------------------------------

def test_a9_the_whole_database_is_still_consistent_after_the_chain(tmp_path: Path) -> None:
    """把 A9.1–A9.9 的几段拼起来跑一遍，最后让仓库自己对账。

    单条用例各自留一个干净的库；这条要的是**长会话**：建定义、跑完一次、
    重试一次、再来一次中止 —— 然后 `verify_consistency()` 必须对每一次执行都通过。
    缓存列与事件流分叉往往正是"跑多了"才出现（单次难复现）。
    """
    ctx, definition, execution_id = _bound_execution(tmp_path, request_id="req-long")
    StubKernel(ctx.repo, execution_id).run_to_completion()

    second = _ok(ctx, "execute_workflow", {
        "workflow_id": definition["workflow_id"],
        "version": definition["version"],
        "request_id": "req-long-2",
        "input": {"topic": "second"},
    })["execution_id"]
    kernel = StubKernel(ctx.repo, second)
    kernel.start()
    _ok(ctx, "abort_execution", {"execution_id": second})
    kernel.advance()

    for record in (ctx.repo.get(execution_id), ctx.repo.get(second)):
        assert record.state in TERMINAL_STATES, record.state
        assert ctx.repo.verify_consistency(record.execution_id).state == record.state
        assert ctx.repo.replay_state(record.execution_id) == record.state
