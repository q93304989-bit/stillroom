"""工具闸门与验收：参数校验 / 预算 / 审批 / 循环检测 / 结果验收。"""

from __future__ import annotations

import pytest

from app.capabilities.middleware import (
    DEFAULT_BUDGET_LIMITS,
    ApprovalGate,
    BudgetGate,
    CONTEXT_BUDGET_FREE,
    LoopBreaker,
    Middleware,
    ParamValidation,
    ResultValidation,
    ToolCall,
    default_middlewares,
)
from app.capabilities.registry import SIDE_NETWORK, SIDE_UPLOAD, ToolRegistry, ToolSpec
from app.net.errors import (
    BudgetExceeded,
    LoopDetected,
    NeedsApproval,
    ResponseFormatError,
    ValidationError,
)


def spec(name: str, params: dict, **kwargs) -> ToolSpec:
    return ToolSpec(name=name, description="", params=params, **kwargs)


def call(name: str, params: dict, tool_spec: ToolSpec | None = None, context: dict | None = None) -> ToolCall:
    return ToolCall(
        tool=name,
        params=params,
        spec=tool_spec or spec(name, {"type": "object"}),
        context=context if context is not None else {},
    )


IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "images": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "model": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["prompt"],
}


# --------------------------------------------------------------------------- 参数校验

async def test_param_validation_blocks_bad_arguments():
    gate = ParamValidation()
    with pytest.raises(ValidationError) as excinfo:
        await gate.before(call("image.generate", {}, spec("image.generate", IMAGE_SCHEMA)))
    assert "prompt" in str(excinfo.value)

    with pytest.raises(ValidationError):
        await gate.before(call("image.generate", {"prompt": 123}, spec("image.generate", IMAGE_SCHEMA)))
    with pytest.raises(ValidationError):
        await gate.before(call("image.generate", {"prompt": "x", "model": "c"}, spec("image.generate", IMAGE_SCHEMA)))
    with pytest.raises(ValidationError) as excinfo:
        await gate.before(
            call("image.generate", {"prompt": "x", "images": ["1", "2", "3", "4", "5", "6"]}, spec("image.generate", IMAGE_SCHEMA))
        )
    assert "最多 5 项" in str(excinfo.value)


async def test_param_validation_accepts_tuple_and_nested_items():
    """自己的代码常把 images 传成 tuple —— 不能因此把自己的调用拦下。"""
    gate = ParamValidation()
    await gate.before(
        call("image.generate", {"prompt": "x", "images": ("a.png", "b.png")}, spec("image.generate", IMAGE_SCHEMA))
    )
    with pytest.raises(ValidationError):
        await gate.before(
            call("image.generate", {"prompt": "x", "images": [1]}, spec("image.generate", IMAGE_SCHEMA))
        )


# --------------------------------------------------------------------------- 预算

async def test_budget_gate_counts_and_blocks():
    gate = BudgetGate({"image.generate": 2})
    context: dict = {}
    await gate.before(call("image.generate", {}, context=context))
    await gate.before(call("image.generate", {}, context=context))
    with pytest.raises(BudgetExceeded) as excinfo:
        await gate.before(call("image.generate", {}, context=context))

    assert excinfo.value.tool == "image.generate"
    assert excinfo.value.used == 2 and excinfo.value.limit == 2
    assert gate.snapshot(context)["image.generate"] == {"used": 2, "limit": 2}


async def test_budget_gate_ignores_tools_without_limit():
    gate = BudgetGate({"image.generate": 1})
    context: dict = {}
    for _ in range(5):
        await gate.before(call("media.fetch", {}, context=context))
    # 没有限额的工具连计数都不该写进上下文（保持上下文干净）
    assert context.get("usage", {}) == {}


async def test_budget_gate_free_pass_is_one_shot():
    """重试免额度是**一次性**的：放行一次后立刻恢复计数，不会把额度变成无限。

    为什么要有这条：平台 503 / 队列满时我们自动退避重试，那不是我方的额外消耗
    （实测：video.submit 上限 2，平台 503 重试两次就把额度吃光，用户看到的是
    「额度已耗尽」而不是「队列已满」）。所以重试要免额度——但只能免一次。
    """
    gate = BudgetGate({"video.submit": 1})
    context: dict = {}

    await gate.before(call("video.submit", {}, context=context))       # 首次：占 1 格
    with pytest.raises(BudgetExceeded):
        await gate.before(call("video.submit", {}, context=context))   # 用完就拦

    # 模拟 generation._call_with_retry：重试前标记一次免额度
    context.setdefault(CONTEXT_BUDGET_FREE, set()).add("video.submit")
    await gate.before(call("video.submit", {}, context=context))       # 重试放行
    assert context[CONTEXT_BUDGET_FREE] == set(), "豁免标记用掉后必须清掉"
    assert context["usage"]["video.submit"] == 1, "重试不该再加计数"

    with pytest.raises(BudgetExceeded):
        await gate.before(call("video.submit", {}, context=context))   # 再试就被拦


async def test_budget_gate_free_pass_only_affects_the_marked_tool():
    gate = BudgetGate({"video.submit": 1, "image.generate": 1})
    context: dict = {CONTEXT_BUDGET_FREE: {"video.submit"}}
    context["usage"] = {"image.generate": 1}

    await gate.before(call("video.submit", {}, context=context))       # 被豁免
    with pytest.raises(BudgetExceeded):
        await gate.before(call("image.generate", {}, context=context)) # 没豁免，照拦


async def test_default_budget_limits_are_conservative():
    assert DEFAULT_BUDGET_LIMITS["image.generate"] <= 10
    assert DEFAULT_BUDGET_LIMITS["video.submit"] <= 3


def test_prepare_phase_can_reach_both_local_sources():
    """找参考阶段：历史（A-RAG）与知识库都够得到，别的手段照旧够不到。"""
    from app.agent.phases import tools_of

    tools = tools_of("prepare")
    assert "rag.search" in tools and "kb.search" in tools
    assert DEFAULT_BUDGET_LIMITS.get("kb.search", 0) > 0, "kb.search 要有预算额度"


# --------------------------------------------------------------------------- 审批

async def test_approval_gate_blocks_upload_until_approved():
    upload = spec("image_host.upload", {"type": "object"}, side_effects=frozenset({SIDE_NETWORK, SIDE_UPLOAD}))
    gate = ApprovalGate()
    context: dict = {}

    with pytest.raises(NeedsApproval) as excinfo:
        await gate.before(call("image_host.upload", {"path": "a.png"}, upload, context))
    assert excinfo.value.tool == "image_host.upload"
    assert excinfo.value.params == {"path": "a.png"}

    context["approved"] = {"image_host.upload"}          # 用户点过确认
    await gate.before(call("image_host.upload", {"path": "a.png"}, upload, context))


async def test_approval_gate_lets_safe_tools_through():
    gate = ApprovalGate()
    await gate.before(call("image.generate", {}, spec("image.generate", {"type": "object"})))


# --------------------------------------------------------------------------- 循环

async def test_loop_breaker_stops_repeats():
    gate = LoopBreaker(max_repeats=3)
    context: dict = {}
    for _ in range(3):
        await gate.before(call("image.generate", {"prompt": "同样的"}, context=context))
    with pytest.raises(LoopDetected) as excinfo:
        await gate.before(call("image.generate", {"prompt": "同样的"}, context=context))
    assert excinfo.value.repeats == 3

    # 换个参数就不算打转
    await gate.before(call("image.generate", {"prompt": "换个说法"}, context=context))


async def test_loop_breaker_fingerprint_is_order_insensitive():
    gate = LoopBreaker(max_repeats=1)
    context: dict = {}
    await gate.before(call("t", {"a": 1, "b": 2}, context=context))
    with pytest.raises(LoopDetected):
        await gate.before(call("t", {"b": 2, "a": 1}, context=context))


# --------------------------------------------------------------------------- 验收

async def test_result_validation_catches_broken_results():
    gate = ResultValidation()
    with pytest.raises(ResponseFormatError):
        await gate.after(call("image.generate", {}), "")
    with pytest.raises(ResponseFormatError):
        await gate.after(call("judge.ask", {}), {"model": "jev"})
    with pytest.raises(ResponseFormatError):
        await gate.after(call("vision.describe", {}), {"subject": "", "style": ""})
    with pytest.raises(ResponseFormatError):
        await gate.after(call("media.fetch", {}), b"")

    assert await gate.after(call("image.generate", {}), "https://cdn/a.png") == "https://cdn/a.png"
    good_judge = {"answers": {"fits": {"type": "noul", "noul": 0.9}}}
    assert await gate.after(call("judge.ask", {}), good_judge) is good_judge
    good_desc = {"subject": "城市夜景"}
    assert await gate.after(call("vision.describe", {}), good_desc) is good_desc


async def test_result_validation_can_be_extended():
    gate = ResultValidation()

    def must_have_url(_call: ToolCall, result):
        if not isinstance(result, dict) or "url" not in result:
            raise ResponseFormatError("缺 url")
        return result

    gate.register("custom.tool", must_have_url)
    with pytest.raises(ResponseFormatError):
        await gate.after(call("custom.tool", {}), {"link": "x"})
    assert await gate.after(call("custom.tool", {}), {"url": "x"}) == {"url": "x"}


# --------------------------------------------------------------------------- 链本身

async def test_chain_order_and_no_handler_on_reject():
    order: list[str] = []

    class Recorder(Middleware):
        def __init__(self, label: str) -> None:
            self.label = label

        async def before(self, _call: ToolCall) -> None:
            order.append(f"before:{self.label}")

        async def after(self, _call: ToolCall, result):
            order.append(f"after:{self.label}")
            return result

    registry = ToolRegistry()
    called = {"handler": 0}

    async def handler(params):
        called["handler"] += 1
        return "ok"

    registry.register(spec("t", {"type": "object"}), handler)
    registry.use(Recorder("1")).use(Recorder("2"))

    assert await registry.invoke("t", {}) == "ok"
    assert order == ["before:1", "before:2", "after:2", "after:1"]
    assert called["handler"] == 1

    # 闸门拦下时，处理函数一次都不该被调用
    registry2 = ToolRegistry()
    registry2.register(spec("t", {"type": "object", "required": ["x"]}), handler)
    registry2.use(ParamValidation())
    with pytest.raises(ValidationError):
        await registry2.invoke("t", {})
    assert called["handler"] == 1


async def test_invoke_passes_context_between_calls():
    registry = ToolRegistry()

    async def handler(params):
        return "u"

    registry.register(spec("image.generate", {"type": "object"}), handler)
    registry.use(BudgetGate({"image.generate": 1}))

    shared: dict = {}
    assert await registry.invoke("image.generate", {}, context=shared) == "u"
    with pytest.raises(BudgetExceeded):
        await registry.invoke("image.generate", {}, context=shared)

    # 不传 context 就是各自独立：新的一次调用不受上一批影响
    assert await registry.invoke("image.generate", {}) == "u"


def test_default_middlewares_order():
    names = [middleware.name for middleware in default_middlewares()]
    assert names == [
        "param-validation",
        "budget",
        "approval",
        "loop-breaker",
        "phase-scope",
        "result-validation",
    ]


async def test_phase_gate_blocks_tools_outside_the_phase():
    """阶段白名单：不在当前阶段的能力应当物理上够不到。"""
    from app.capabilities.middleware import PhaseGate
    from app.net.errors import ToolNotAllowed

    gate = PhaseGate()
    context = {"allowed_tools": ("image.generate", "video.submit")}

    await gate.before(call("image.generate", {}, context=context))
    with pytest.raises(ToolNotAllowed) as excinfo:
        await gate.before(call("image_host.upload", {"path": "a.png"}, context=context))
    assert excinfo.value.tool == "image_host.upload"
    assert "image.generate" in excinfo.value.allowed

    # 没设白名单 = 不限制（手动流程就是这种状态）
    await gate.before(call("image_host.upload", {}, context={}))
