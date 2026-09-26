"""工具执行前后的闸门与验收（middleware）。

为什么放在能力注册表这一层：**全项目只有 `registry.invoke()` 是工具调用的必经之路**。
闸门放在这里，手动界面、六步流水线、将来的 agent 三条路径都自动受同一套规则约束；
放在服务层里则只要有人绕过服务层就直接失效（限流最初就写在服务层，属于隐患）。

两段钩子：

- `before`：拦住不该发生的调用（参数不合规、超预算、需要审批、在打转、被限流）
- `after`：拿到结果先验收（URL 是否有效、字段是否齐全），不合格就抛错让上层重试或降级

三条约定：

1. 中间件只做「放行 / 拦截 / 降级」，**不隐式改写请求**；
2. 拦截一律抛类型化异常（`NeedsApproval` / `BudgetExceeded` / `LoopDetected` / `ValidationError`），
   上层据此分三种反应：排队等确认、停下问用户、直接报错；
3. 顺序固定且「先便宜后昂贵」：参数校验 → 预算 → 审批 → 循环检测 → 限流 → 真调用 → 验收。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from app.capabilities.registry import SIDE_UPLOAD, ToolSpec
from app.net.errors import (
    BudgetExceeded,
    LoopDetected,
    NeedsApproval,
    ResponseFormatError,
    ToolNotAllowed,
    ValidationError,
)

#: 运行上下文里用到的键（一次运行内共享，调用方负责传递）
CONTEXT_USAGE = "usage"          # {工具名: 已调用次数}
CONTEXT_HISTORY = "history"      # [(工具名, 参数指纹)]
CONTEXT_APPROVED = "approved"    # {已获批准的工具名}
CONTEXT_ALLOWED_TOOLS = "allowed_tools"   # 当前阶段允许的工具集合（工作流用）
#: **下一次**调用不计额度的工具名集合（一次性；重试专用，理由见 `BudgetGate.before`）
CONTEXT_BUDGET_FREE = "budget_free"


@dataclass
class ToolCall:
    """一次工具调用的上下文（闸门据此判断，验收据此校验）。"""

    tool: str
    params: dict
    spec: ToolSpec
    context: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        """工具 + 参数的指纹，用于循环检测（参数顺序无关）。"""
        try:
            payload = json.dumps(self.params, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(sorted(self.params.items()))
        return f"{self.tool}|{payload[:500]}"


class Middleware:
    """中间件基类：两个钩子都可选，默认放行。"""

    name = "middleware"

    async def before(self, call: ToolCall) -> None:
        return None

    async def after(self, call: ToolCall, result: Any) -> Any:
        return result


# --------------------------------------------------------------------------- before 闸门


class ParamValidation(Middleware):
    """按能力自带的 JSON Schema 校验参数。

    最便宜的一道闸，放在最前面：参数不合规直接挡回，不浪费一次网络调用，也不烧额度。
    只实现用到的那几种关键字（type / required / enum / maxItems / items），够用且没有依赖。
    """

    name = "param-validation"
    _SUPPORTED = {"object", "string", "array", "integer", "number", "boolean"}

    async def before(self, call: ToolCall) -> None:
        self._check(call.spec.params or {}, call.params, path=call.tool)

    def _check(self, schema: Mapping[str, Any], value: Any, *, path: str) -> None:
        expected = schema.get("type")
        if expected in self._SUPPORTED:
            self._check_type(expected, value, path)
        if expected == "object":
            for key in schema.get("required", []):
                if key not in (value or {}) or (value or {}).get(key) in (None, ""):
                    raise ValidationError(f"缺少必填参数：{path}.{key}")
            for key, sub_schema in (schema.get("properties") or {}).items():
                if isinstance(value, Mapping) and value.get(key) is not None:
                    self._check(sub_schema, value[key], path=f"{path}.{key}")
        elif expected == "array" and isinstance(value, (list, tuple, set)):
            # 注意：我们自己的代码常把 images 传成 tuple，JSON Schema 里叫 array——
            # 两者都接受，否则闸门会把自己的调用拦下来（这个坑实测踩过）
            value = list(value)
            max_items = schema.get("maxItems")
            if isinstance(max_items, int) and len(value) > max_items:
                raise ValidationError(f"{path} 最多 {max_items} 项（当前 {len(value)} 项）")
            item_schema = schema.get("items")
            if isinstance(item_schema, Mapping):
                for index, item in enumerate(value):
                    self._check(item_schema, item, path=f"{path}[{index}]")
        options = schema.get("enum")
        if isinstance(options, list) and options and value not in options:
            raise ValidationError(f"{path} 只能是 {options} 之一（收到 {value!r}）")

    @staticmethod
    def _check_type(expected: str, value: Any, path: str) -> None:
        if expected == "object" and not isinstance(value, Mapping):
            raise ValidationError(f"{path} 需要是对象，收到 {type(value).__name__}")
        if expected == "string" and not isinstance(value, str):
            raise ValidationError(f"{path} 需要是字符串，收到 {type(value).__name__}")
        if expected == "array" and not isinstance(value, (list, tuple, set)):
            raise ValidationError(f"{path} 需要是数组，收到 {type(value).__name__}")
        if expected == "integer" and not isinstance(value, int):
            raise ValidationError(f"{path} 需要是整数，收到 {type(value).__name__}")
        if expected == "number" and not isinstance(value, (int, float)):
            raise ValidationError(f"{path} 需要是数字，收到 {type(value).__name__}")
        if expected == "boolean" and not isinstance(value, bool):
            raise ValidationError(f"{path} 需要是布尔值，收到 {type(value).__name__}")


class BudgetGate(Middleware):
    """预算闸门：一次运行里某个工具最多调用几次。超了就停下问人。

    这是「agent 把一次出图变成三十次调用」的唯一有效刹车——放在注册表层，任何入口都拦得住。

    **重试不算额度**（`CONTEXT_BUDGET_FREE`）：平台 503 / 队列满时，代码会自动退避重试，
    那不是我方的额外消耗，而是同一个动作的重复。把重试也算进额度，会让用户撞上
    「平台在排队 → 额度先被自己用完 → 报『额度已耗尽』」这种驴唇不对马嘴的失败
    （实测：video.submit 上限 2，平台 503 重试两次就把额度吃光，用户看到的是额度问题）。
    豁免是**一次性**的：每次重试前重新标记一次，用完即清，不会把额度变成无限。
    """

    name = "budget"

    def __init__(self, limits: Mapping[str, int] | None = None) -> None:
        self.limits = dict(limits or {})

    async def before(self, call: ToolCall) -> None:
        limit = self.limits.get(call.tool)
        if not limit:
            return
        free = call.context.get(CONTEXT_BUDGET_FREE)
        if isinstance(free, set) and call.tool in free:
            free.discard(call.tool)      # 一次性：这次放行，下次照常计数
            return
        usage = call.context.setdefault(CONTEXT_USAGE, {})
        used = int(usage.get(call.tool, 0))
        if used >= limit:
            raise BudgetExceeded(
                f"本次运行已用完 {call.tool} 的额度（上限 {limit} 次），已停下等你确认",
                tool=call.tool,
                used=used,
                limit=limit,
            )
        usage[call.tool] = used + 1

    def snapshot(self, context: Mapping[str, Any]) -> dict:
        """给界面看「这次用了多少」（也便于写进运行记录）。"""
        usage = dict((context or {}).get(CONTEXT_USAGE) or {})
        return {tool: {"used": count, "limit": self.limits.get(tool)} for tool, count in usage.items()}


class ApprovalGate(Middleware):
    """审批闸门：有副作用的动作先排队等用户确认。

    判定依据来自能力元数据（`side_effects` 里的 `upload`），不是硬编码工具名——
    将来新增一个会上传的能力，只要声明了副作用就自动被拦。
    """

    name = "approval"

    def __init__(self, always_ask: Iterable[str] = ()) -> None:
        self.always_ask = set(always_ask)

    async def before(self, call: ToolCall) -> None:
        approved = call.context.setdefault(CONTEXT_APPROVED, set())
        if call.tool in approved:
            return
        risky = call.spec.has_side_effect(SIDE_UPLOAD) or call.tool in self.always_ask
        if risky:
            raise NeedsApproval(
                f"{call.tool} 会把内容送到外部且不可回滚，需要你确认后才会执行",
                tool=call.tool,
                params=dict(call.params),
            )


class LoopBreaker(Middleware):
    """循环检测：同一工具 + 相同参数连续出现 N 次就打断（多半是模型在打转）。"""

    name = "loop-breaker"

    def __init__(self, max_repeats: int = 3) -> None:
        self.max_repeats = max(1, max_repeats)

    async def before(self, call: ToolCall) -> None:
        history = call.context.setdefault(CONTEXT_HISTORY, [])
        fingerprint = call.fingerprint()
        repeats = sum(1 for item in history if item == fingerprint)
        if repeats >= self.max_repeats:
            raise LoopDetected(
                f"{call.tool} 已经用相同参数试了 {repeats} 次，先停下重新规划（或换个参数）",
                tool=call.tool,
                repeats=repeats,
            )
        history.append(fingerprint)


class PhaseGate(Middleware):
    """阶段白名单：当前阶段只能调用允许的工具。

    流水线在每个阶段开始前把 `context["allowed_tools"]` 设成本阶段的白名单；
    没设白名单（手动流程）就等同于不限制。
    """

    name = "phase-scope"

    async def before(self, call: ToolCall) -> None:
        allowed = call.context.get(CONTEXT_ALLOWED_TOOLS)
        if allowed is None:
            return
        allowed = tuple(allowed)
        if call.tool not in allowed:
            raise ToolNotAllowed(
                f"当前阶段不能调用 {call.tool}（本阶段允许：{', '.join(allowed) or '无'}）",
                tool=call.tool,
                allowed=allowed,
            )
# --------------------------------------------------------------------------- after 验收


def _expect_url(call: ToolCall, result: Any) -> Any:
    if not isinstance(result, str) or not result.strip():
        raise ResponseFormatError(f"{call.tool} 应当返回图片地址，实际拿到 {type(result).__name__}")
    return result


def _expect_bytes(call: ToolCall, result: Any) -> Any:
    if not isinstance(result, (bytes, bytearray)) or not result:
        raise ResponseFormatError(f"{call.tool} 应当返回非空字节，实际拿到 {type(result).__name__}")
    return result


def _expect_answers(call: ToolCall, result: Any) -> Any:
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict) or not result["answers"]:
        raise ResponseFormatError(f"{call.tool} 没有返回有效判断：{str(result)[:120]}")
    return result


def _expect_description(call: ToolCall, result: Any) -> Any:
    if not isinstance(result, dict):
        raise ResponseFormatError(f"{call.tool} 应当返回结构化描述，实际拿到 {type(result).__name__}")
    if not any(result.get(key) for key in ("subject", "style", "composition", "lighting")):
        raise ResponseFormatError("视觉描述四个字段全空，等于没描述出内容")
    return result


def _expect_search(call: ToolCall, result: Any) -> Any:
    """联网搜索的验收：允许「搜不到」「搜不了」，但不允许形状不对。

    `ok=false` 带着 reason 回来是**合法结果**（没配 key 也是要交代给用户的信息），
    所以这里只查字段齐不齐——真正的判断留给上层。
    """
    if not isinstance(result, dict) or "items" not in result or "ok" not in result:
        raise ResponseFormatError(f"{call.tool} 应当返回 {{ok, items, reason}}，实际拿到 {type(result).__name__}")
    if not isinstance(result.get("items"), list):
        raise ResponseFormatError(f"{call.tool} 的 items 应当是列表")
    return result


def _expect_image_saved(call: ToolCall, result: Any) -> Any:
    """配图下载的验收：必须真的落盘（path 存在且非空），否则算这一步失败。"""
    path = (result or {}).get("path") if isinstance(result, dict) else None
    if not path or not isinstance(path, str):
        raise ResponseFormatError(f"{call.tool} 应当返回落盘路径 path，实际拿到 {str(result)[:120]}")
    return result


class ResultValidation(Middleware):
    """验收：结果先按能力约定的形状过一遍，不合格就抛错让上层重试或降级。

    已有约定自动生效；新增能力可以 `register(tool, 校验函数)` 补一条。
    """

    name = "result-validation"

    def __init__(self) -> None:
        self._validators: dict[str, Callable[[ToolCall, Any], Any]] = {
            "image.generate": _expect_url,
            "media.fetch": _expect_bytes,
            "judge.ask": _expect_answers,
            "vision.describe": _expect_description,
            "web.search": _expect_search,
            "web.fetch_image": _expect_image_saved,
        }

    def register(self, tool: str, validator: Callable[[ToolCall, Any], Any]) -> "ResultValidation":
        self._validators[tool] = validator
        return self

    async def after(self, call: ToolCall, result: Any) -> Any:
        validator = self._validators.get(call.tool)
        return validator(call, result) if validator else result


def default_middlewares(
    *, budget_limits: Mapping[str, int] | None = None, max_repeats: int = 3
) -> list[Middleware]:
    """默认闸门组合（顺序即执行顺序：先便宜后昂贵）。"""
    return [
        ParamValidation(),
        BudgetGate(budget_limits if budget_limits is not None else DEFAULT_BUDGET_LIMITS),
        ApprovalGate(),
        LoopBreaker(max_repeats),
        PhaseGate(),
        ResultValidation(),
    ]


#: 一次运行里各类动作的默认上限（保守取值：够用，且不至于「一次出三十张」）
DEFAULT_BUDGET_LIMITS: dict[str, int] = {
    "image.generate": 6,        # 一次任务最多出 6 张候选
    "video.submit": 2,          # 视频贵且每分钟限 1 个
    "vision.describe": 10,      # 每次生成最多评估 10 张
    "judge.ask": 30,
    "llm.chat": 20,
    "image_host.upload": 5,
    "kb.search": 8,             # 知识库检索（含一次 Jev 重排）
    "web.search": 3,            # 联网搜索：一次运行最多搜 3 次（方案 6.1）
    "web.fetch_image": 4,       # 联网配图：一次运行最多下 4 张（方案 6.2）
}
