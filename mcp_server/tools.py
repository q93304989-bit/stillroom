"""11 个 MCP 工具的业务实现。

## 铁律：**工具只做业务，不管协议**

| 情形 | 谁负责 | 出口 |
|---|---|---|
| 方法名不存在 | `dispatch.py` | `-32601`（`error` 字段） |
| 入参缺字段 / 类型不对 / 有未知字段 | **本模块**（`_require_*` / `_reject_unknown`） | `{"ok": false, "error": {"code": "INPUT_SCHEMA_INVALID", …}}` |
| 业务失败（`StillroomRuntimeError`） | 本模块**抛出**，由 `build_toolset` 的统一 binder 兜 | `{"ok": false, "error": {...}}` |
| 编程错误（`TypeError` / `KeyError` …） | **不接** | 一路抛到 `jsonrpc.serve()` → `-32603` |

推论（不守住就会长出 11 份协议逻辑）：

- 工具函数**永不构造 JSON-RPC 错误对象**，也**永不自己拼 `{"ok": …, …}` 信封** ——
  成功与失败的壳都由 binder 一处产生，工具只返回 `data` 本身；
- 工具**不 `try/except` 自己的编程错误**。`try/except Exception` 会把
  "工具内部炸了"伪装成"业务上做不到"，真缺陷就此从日志里消失；
- 唯一的例外是**刻意标注**的局部 catch（见 `get_execution_status` 对定义读取的处理）。

## 为什么工具签名是 `(ctx, params)` 而不是 `(params)` + 闭包

11 个顶层普通函数可以**直接单测**：`execute_workflow(ctx, {...})`，
不必先 `build_toolset` 再按名字查。用例更短，失败信息直接指到函数名。

`ctx` 承载"这次连接是谁"与所有后端依赖（repo / registry / router）。
把它做成参数而不是闭包捕获，还有一个好处：**签名本身就说明了
工具的权限来源不可能是 `params`** —— 见下。

## `creator_context` 不可能来自 `params`

它只存在于 `ctx` 里，而 `ctx` 由 `stdio.py` 在**进程启动时**用启动参数构造。
客户端若在 `params` 里塞 `creator_context` / `identity` / `max_trust_level`，
`_reject_unknown`（按白名单）与 `_reject_identity`（按禁名表）会**直接拒掉**
（`INPUT_SCHEMA_INVALID`），不是静默忽略。

选"拒"而不是"忽略"：忽略会让**伪造尝试不留痕迹**，拒掉能让它显形。
详见 `contracts/mcp-tools.md` §一。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn

from runtime.artifacts import ArtifactStore
from runtime.errors import StillroomRuntimeError
from runtime.registry import STATUS_PENDING_ACTIVATION, WorkflowRegistry
from runtime.repository import ExecutionRepository
from runtime.router import MatchFn, stub_match
from runtime.stub_kernel import StubKernel
from validator.errors import ErrorCode, ValidationResult
from validator.pipeline import PIPELINE_STEPS
from validator.state_machine import ExecutionEvent, ExecutionState
from validator.workflow_validator import KNOWN_CAPABILITIES, validate_metadata

from .dispatch import ToolFn
from .jsonrpc import LogFn

CONTRACT_VERSION = "1.0"
"""`contracts/mcp-tools.md` 的版本，`get_capabilities` 报给客户端的那个。
**不是** `protocol.db` 的 schema 版本 —— 两者独立演化。"""


class InputError(StillroomRuntimeError):
    """MCP 入参不合法。`INPUT_SCHEMA_INVALID` / `METADATA_FORBIDDEN` 的载体。

    继承 `StillroomRuntimeError` 是为了让 binder 的**同一条** `except` 接住它 ——
    入参错误与业务错误对客户端是同一层（都走 `result` 里的结构化错误）。
    """


# ---------------------------------------------------------------------------
# 连接上下文
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServerContext:
    """一次服务进程的全部依赖。由 `stdio.py` 在启动时组装，之后只读。

    `creator_context` 会被**深度冻结**（dict → 只读映射，list → tuple）：
    "不接受客户端自报"这句话如果只是约定，迟早会有人 `ctx.creator_context[...] = …`。
    冻上之后它就是结构性的，不用靠自觉。
    """

    repo: ExecutionRepository
    registry: WorkflowRegistry
    creator_context: Mapping[str, Any]
    router: MatchFn | None = None
    log: LogFn | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "creator_context", _deep_freeze(self.creator_context))

    @property
    def artifacts(self) -> ArtifactStore:
        """内容寻址 store。`resume_execution` 补的那份输入也写这里。

        委托给 registry 的那个实例，不另开一个 —— 一个库配一个 artifact 目录
        才是完整的备份单元（`WorkflowRegistry.__init__` 的注释）。
        """
        return self.registry.artifacts

    # -- 便捷读取 ---------------------------------------------------------

    @property
    def identity(self) -> str:
        return str(self.creator_context.get("identity", "agent"))

    @property
    def max_trust_level(self) -> str:
        return str(self.creator_context.get("max_trust_level", "T2"))

    @property
    def can_auto_activate(self) -> bool:
        return bool(self.creator_context.get("can_auto_activate", False))

    def match(self, intent: str, *, top_k: int) -> dict[str, Any]:
        """走注入的路由器；P1 默认是恒定 `none` 的桩（见 `runtime/router.py`）。"""
        router = self.router or stub_match
        return router(intent, top_k)

    def emit(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


def _deep_freeze(value: Any) -> Any:
    """把嵌套结构变成只读：dict → `MappingProxyType`，list → tuple。"""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


# ---------------------------------------------------------------------------
# 入参助手（11 个工具共用，抽一次不重复）
# ---------------------------------------------------------------------------

def _fail(path: str, message: str, *, code: ErrorCode = ErrorCode.INPUT_SCHEMA_INVALID) -> NoReturn:
    raise InputError(code, message, path=path)


def _reject_unknown(params: Mapping[str, Any], allowed: set[str], *, prefix: str = "") -> None:
    """未知字段一律拒。

    与 `workflow.schema.json` 的 `additionalProperties: false` 是同一条规矩：
    静默忽略等于把客户端的 bug（拼错的字段名）吃掉。

    `path` 用**相对于输入对象的裸路径**（`creator_context`、`filter/capability`），
    与 `workflow_validator` 报错时的风格（`pipeline/refine` 不带前缀）一致。
    """
    unknown = sorted(set(params) - allowed)
    if unknown:
        first = unknown[0]
        _fail(
            f"{prefix}/{first}" if prefix else first,
            f"unknown parameter(s) {unknown}; accepted: {sorted(allowed)}",
        )


IDENTITY_FIELDS = frozenset({
    "creator_context",
    "identity",
    "is_system",
    "allowed_capabilities",
    "max_trust_level",
    "can_auto_activate",
    "trust_level",
})
"""身份 / 权限字段的**禁名表**。契约 §一：「`params` 里任何身份 / 权限字段都不是
合法入参 —— 不是"忽略"，是直接 `INPUT_SCHEMA_INVALID` 拒掉」。

**这是禁名表，不是白名单**，所以它不与任何定义 schema 争夺"哪些字段合法"的权威；
它只回答"这个字段名会不会被读成身份"。

### 为什么除了 `_reject_unknown` 还要这一条

`_reject_unknown` 靠**白名单**工作，只对"params 是工具信封"的工具成立。
但 `create_skill` 的 params **就是定义文档本身**，它的形状由 `validate_skill` 判，
而 `validate_skill` 是按 `_FIELDS` 白名单判"意外字段"的。于是有一个真实的失守路径：
**哪天 `_FIELDS` 增加了一个与身份同名的字段，`validate_skill` 就会静默放行它**，
"params 不能自报身份"这条安全不变式随一次 schema 演进无声失效。

`_reject_identity` 让这件事**不可能**：身份字段在进定义校验之前就被拒了，
与 `_FIELDS` 怎么演化无关。这是纵深防御，不是重复劳动。
"""


def _reject_identity(params: Mapping[str, Any], *, prefix: str = "") -> None:
    hit = sorted(set(params) & IDENTITY_FIELDS)
    if hit:
        _fail(
            f"{prefix}/{hit[0]}" if prefix else hit[0],
            f"identity/permission field(s) {hit} are not accepted from params; "
            "the creator identity comes from the server's startup configuration",
        )


def _require_str(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        _fail(name, f"{name} must be a non-empty string, got {value!r}")
    return value


def _optional_str(params: Mapping[str, Any], name: str) -> str | None:
    if name not in params:
        return None
    value = params[name]
    if not isinstance(value, str):
        _fail(name, f"{name} must be a string, got {type(value).__name__}")
    return value


def _require_int(params: Mapping[str, Any], name: str, *, minimum: int = 1) -> int:
    """**显式排斥 `bool`**：`isinstance(True, int)` 为真，不挡的话
    `{"version": true}` 会被当成 `1` 一路走到底 —— 语义错，且不炸。
    """
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(name, f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


def _require_dict(params: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = params.get(name)
    if not isinstance(value, dict):
        _fail(name, f"{name} must be an object, got {type(value).__name__}")
    return value


def _optional_bool(params: Mapping[str, Any], name: str, default: bool) -> bool:
    if name not in params:
        return default
    value = params[name]
    if not isinstance(value, bool):
        _fail(name, f"{name} must be a boolean, got {type(value).__name__}")
    return value


def _raise_first(result: ValidationResult) -> None:
    """把校验结果的第一条转成 `InputError`（保留它的 `code` 与 `path`）。"""
    first = result.issues[0]
    raise InputError(first.code, first.message, path=first.path)


# ---------------------------------------------------------------------------
# 1. get_capabilities —— 握手
# ---------------------------------------------------------------------------

def get_capabilities(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """入参：无。

    工具清单取自 `TOOLS` **本身**，不另存一份常量 ——
    否则"契约列了 11 个、实现少了一个"这种事只能靠人眼发现。
    """
    _reject_unknown(params, allowed=set())
    return {
        "tools": sorted(TOOLS),
        "capabilities": sorted(KNOWN_CAPABILITIES),
        "identity": ctx.identity,
        "max_trust_level": ctx.max_trust_level,
        "can_auto_activate": ctx.can_auto_activate,
        "protocol_version": CONTRACT_VERSION,
    }


# ---------------------------------------------------------------------------
# 2/3. 技能
# ---------------------------------------------------------------------------

def list_skills(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(params, allowed={"filter"})

    capability: str | None = None
    if "filter" in params:
        flt = _require_dict(params, "filter")
        _reject_unknown(flt, allowed={"capability"}, prefix="filter")
        if "capability" in flt:
            capability = flt["capability"]
            if capability not in KNOWN_CAPABILITIES:
                _fail("filter/capability", f"unknown capability {capability!r}")

    return {"skills": [s.to_dict() for s in ctx.registry.list_skills(capability=capability)]}


def create_skill(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """入参就是技能定义本身（契约 §二.3），所以**形状**校验交给 `validate_skill`。

    **不在本函数里再 `_reject_unknown` 一遍**：那会让"哪些字段合法"有第二个来源，
    两边迟早不一致。非身份类的意外字段由 `validate_skill` 报 `SCHEMA_INVALID`。

    唯一的例外是 `_reject_identity` —— 见那张禁名表的说明：它不是"合法字段"的
    第二个来源，而是**在定义校验之前**把身份字段钉死，避免 `_FIELDS` 演化后
    出现"定义 schema 里恰好有个叫 `trust_level` 的字段于是静默放行"的失守路径。
    于是 `create_skill` 里身份字段报 `INPUT_SCHEMA_INVALID`（契约 §一），
    其余意外字段报 `SCHEMA_INVALID`（定义文档问题）。
    """
    _reject_identity(params)
    record = ctx.registry.register_skill(dict(params), creator=dict(ctx.creator_context))
    return {"skill_id": record.skill_id, "version": record.version, "status": record.status}


# ---------------------------------------------------------------------------
# 4. list_workflows
# ---------------------------------------------------------------------------

def list_workflows(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(params, allowed={"include_inactive"})
    include_inactive = _optional_bool(params, "include_inactive", False)

    return {
        "workflows": [
            s.to_dict()
            for s in ctx.registry.list_workflow_summaries(include_inactive=include_inactive)
        ]
    }


# ---------------------------------------------------------------------------
# 5. match_workflow —— P1 是契约允许的恒定出口
# ---------------------------------------------------------------------------

def match_workflow(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """P1 恒定返回 `{"match": "none", "candidates": []}`（决策 #7）。

    **不是占位符**：契约 §5 规定未命中就返回 `none`，Agent 收到它走
    "换个说法 / 新建 Workflow"，与"匹配过但低于阈值"的处理完全一致。
    """
    _reject_unknown(params, allowed={"intent", "top_k"})
    intent = _require_str(params, "intent")
    top_k = _require_int(params, "top_k", minimum=1) if "top_k" in params else 3

    return ctx.match(intent, top_k=top_k)


# ---------------------------------------------------------------------------
# 6. create_workflow
# ---------------------------------------------------------------------------

def create_workflow(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(params, allowed={"workflow", "activate"})
    workflow = _require_dict(params, "workflow")
    activate = _optional_bool(params, "activate", False)

    record = ctx.registry.register(dict(workflow), creator=dict(ctx.creator_context))
    if activate and record.status == STATUS_PENDING_ACTIVATION:
        record = ctx.registry.activate(record.workflow_id, record.version)

    return {
        "workflow_id": record.workflow_id,
        "version": record.version,
        "status": record.status,
        "trust_level": record.trust_level,
    }


# ---------------------------------------------------------------------------
# 7. execute_workflow
# ---------------------------------------------------------------------------

def execute_workflow(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """`request_id` 幂等：同一个 `request_id` 永远返回**首次**那个 execution。

    P1 限制（**不假装做了**）：`io_contract.input_schema_ref` 只记录不解析 ——
    P1 没有 schema 解析器，fixture 里的 ref 指向的文件也不存在。
    所以这里只校验"`input` 是个对象"这一层形状。
    假装校验比不校验更糟：那会让一个从未被验证的输入看起来是验证过的。
    """
    _reject_unknown(
        params, allowed={"workflow_id", "version", "request_id", "input", "metadata"}
    )
    workflow_id = _require_str(params, "workflow_id")
    version = _require_int(params, "version")
    request_id = _require_str(params, "request_id")
    input_snapshot = _require_dict(params, "input")

    if "metadata" in params:
        metadata = _require_dict(params, "metadata")
        metadata_result = validate_metadata(metadata)
        if not metadata_result.ok:
            _raise_first(metadata_result)

    # 前置检查：不存在 → WORKFLOW_NOT_FOUND，未激活 → WORKFLOW_NOT_ACTIVE
    record = ctx.registry.require_active(workflow_id, version)

    bound = ctx.repo.bind_request(
        request_id=request_id,
        workflow_id=workflow_id,
        workflow_version=version,
        input_snapshot=input_snapshot,
        trust_level=ctx.max_trust_level,
        workflow_definition_hash=record.definition_hash,
        workflow_definition_ref=record.definition_ref,
    )

    data: dict[str, Any] = {
        "execution_id": bound.execution_id,
        "request_id": bound.record.request_id,
        # **当前**状态，不是 `PENDING`：幂等命中时它可能是 RUNNING 甚至 COMPLETED。
        # 报 `PENDING` 会让客户端以为"刚起了一个新的"。
        "status": bound.record.state,
        "is_duplicate": not bound.created,
    }
    if bound.input_mismatch:
        # 同一个 request_id 配了不同 input —— 客户端的 bug。
        # `BindResult` 刻意不报错（契约要求返回既有 execution），但也不许静默。
        data["input_mismatch"] = True
        ctx.emit(f"execute_workflow: request_id {request_id!r} reused with different input")
    return data


# ---------------------------------------------------------------------------
# 8. retry_execution
# ---------------------------------------------------------------------------

def retry_execution(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """**没有 override 字段**（契约 §二.8）：`max_retries` 取自工作流定义的
    `resource_policy`，`budget_raised` 在 P1 恒为 `False` —— Agent 无法自己抬预算。
    """
    _reject_unknown(params, allowed={"execution_id", "request_id"})
    execution_id = _require_str(params, "execution_id")
    request_id = _require_str(params, "request_id")

    original = ctx.repo.get(execution_id)
    if original.request_id != request_id:
        _fail(
            "request_id",
            f"request_id {request_id!r} does not belong to {execution_id}; "
            f"its request_id is {original.request_id!r}",
        )

    definition = ctx.registry.get_definition(original.workflow_id, original.workflow_version)
    max_retries = definition["resource_policy"]["max_retries"]

    bound = ctx.repo.retry(execution_id, max_retries=int(max_retries))
    return {
        "execution_id": bound.execution_id,
        "parent_execution_id": bound.record.parent_execution_id,
        "status": bound.record.state,
    }


# ---------------------------------------------------------------------------
# 9. get_execution_status
# ---------------------------------------------------------------------------

def get_execution_status(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """出参字段全部保留契约形状，但 P1 有两个字段**如实为空**：

    - `budget.tokens_used` 恒 `0`：stub kernel 真的不消耗 token（没在假装统计）。
      `max_tokens` 取自定义的 `resource_policy`，读不到就 `None`。
    - `artifacts` 恒 `[]`：P1 的内核只产出 `payload_ref` 指针，**没有真的产物**。
      按决策 #6 的先例（不为工作流定义伪造 Artifact Manifest），
      这里也不伪造 `artifact_id` / `kind` / `uri`。
    """
    _reject_unknown(params, allowed={"execution_id"})
    execution_id = _require_str(params, "execution_id")

    record = ctx.repo.get(execution_id)
    events = ctx.repo.list_events(execution_id)
    current_step = StubKernel(ctx.repo, execution_id).current_step()

    ended = [
        event.step
        for event in events
        if event.type == ExecutionEvent.ENGINE_STEP_ENDED.value and event.step
    ]
    steps = [
        {"step": step, "status": _step_status(step, ended, current_step)}
        for step in PIPELINE_STEPS
    ]

    return {
        "status": record.state,
        "current_step": current_step,
        "steps": steps,
        "budget": {
            "tokens_used": 0,
            "max_tokens": _max_tokens_of(ctx, record.workflow_id, record.workflow_version),
        },
        "artifacts": [],
        "error": record.error_code,
    }


def _step_status(step: str, ended: list[str], current_step: str | None) -> str:
    if step in ended:
        return "done"
    if step == current_step:
        return "running"
    return "pending"


def _max_tokens_of(ctx: ServerContext, workflow_id: str, version: int) -> int | None:
    """读定义里的预算上限。**读不到不能影响状态查询** ——

    调用方问的是"这次执行现在怎么样"，而定义的可用性是 registry 域的事。
    因为一个指针读不到就把整条状态查询变成错误，是把两件事混在一起。
    这也是本模块唯一一处刻意标注的局部 catch。
    """
    try:
        definition = ctx.registry.get_definition(workflow_id, version)
    except StillroomRuntimeError as exc:
        ctx.emit(f"get_execution_status: cannot read definition of {workflow_id} v{version}: {exc}")
        return None
    return int(definition["resource_policy"]["max_tokens"])


# ---------------------------------------------------------------------------
# 10/11. abort / resume
# ---------------------------------------------------------------------------

def abort_execution(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """请求中止。落点由状态机决定，不由本函数决定：

    - `PENDING` → `ABORTED`（还没有步骤在跑，即时生效）
    - `RUNNING` → `ABORT_PENDING`（**不掐断**当前步骤，等它自己收尾）
    - `WAITING_INPUT` → `ABORTED`
    - 终态 → `ALREADY_TERMINAL`（`append_event` 里的 `apply()` 给出）
    """
    _reject_unknown(params, allowed={"execution_id", "reason"})
    execution_id = _require_str(params, "execution_id")
    reason = _optional_str(params, "reason")

    event = ctx.repo.append_event(
        execution_id, ExecutionEvent.ABORT_REQUESTED.value, message=reason
    )
    return {"status": event.status_after}


def resume_execution(ctx: ServerContext, params: dict[str, Any]) -> dict[str, Any]:
    """从 `WAITING_INPUT` 恢复。

    **先显式判状态再动手** —— 理由是**副作用**，不是错误码：
    `resume_requested` 在非 `WAITING_INPUT` 下本来就由状态机报
    `NOT_WAITING_INPUT`（`validator/state_machine.py` 里有一个专门分支），
    所以拿掉这个检查，**错误码一模一样**。

    差别在于：补上来的 `input` 会被写进内容寻址 store。
    没有前置检查的话，一次**被拒绝**的调用会先落一份永远没人引用的 artifact
    再去撞状态机 —— 错误码看着没错，副作用已经发生了。
    先判状态就是把这件事挡在门外。

    （这个理由是被反证逼出来的，不是事后编的：`tools/prove_mcp_tools.py` 里
    "拿掉前置检查"那个变异最初**没被抓住** —— 因为当时的断言只看错误码，
    而错误码压根不由这个检查决定。现在守它的是
    `test_a_rejected_resume_leaves_no_artifact_behind`。）

    补上来的输入写进 store，事件只带 `payload_ref` / `payload_hash`：
    `input_snapshot` 是不可变的（幂等与重放都依赖这一点），
    恢复输入只能作为事件载荷追加，不能改写原快照。
    `payload_ref` 也不会污染 `output_ref` —— 折叠规则只认进入 `COMPLETED` 的事件。
    """
    _reject_unknown(params, allowed={"execution_id", "input"})
    execution_id = _require_str(params, "execution_id")
    supplied = _require_dict(params, "input")

    record = ctx.repo.get(execution_id)
    if record.state != ExecutionState.WAITING_INPUT.value:
        _fail(
            "execution_id",
            f"{execution_id} is {record.state}, not WAITING_INPUT",
            code=ErrorCode.NOT_WAITING_INPUT,
        )

    stored = ctx.artifacts.put_json(supplied)
    event = ctx.repo.append_event(
        execution_id,
        ExecutionEvent.RESUME_REQUESTED.value,
        payload_ref=stored.ref,
        payload_hash=stored.sha256,
    )
    return {"status": event.status_after}


# ---------------------------------------------------------------------------
# 工具表：11 个名字的**唯一**来源
# ---------------------------------------------------------------------------

ToolImpl = Callable[[ServerContext, dict[str, Any]], dict[str, Any]]

TOOLS: dict[str, ToolImpl] = {
    "get_capabilities": get_capabilities,
    "list_skills": list_skills,
    "create_skill": create_skill,
    "list_workflows": list_workflows,
    "match_workflow": match_workflow,
    "create_workflow": create_workflow,
    "execute_workflow": execute_workflow,
    "retry_execution": retry_execution,
    "get_execution_status": get_execution_status,
    "abort_execution": abort_execution,
    "resume_execution": resume_execution,
}


def build_toolset(ctx: ServerContext) -> dict[str, ToolFn]:
    """把 11 个工具绑上 `ctx`，并**统一**套上信封 + 异常归一化。

    用**一个 binder** 而不是 11 个 `@tool` 装饰器：装饰器漏一个，
    在代码里是看不出来的；binder 漏不掉 —— 它遍历的就是那张唯一的表。
    """
    return {name: _bind(name, impl, ctx) for name, impl in TOOLS.items()}


def _bind(name: str, impl: ToolImpl, ctx: ServerContext) -> ToolFn:
    def tool(params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, "data": impl(ctx, params)}
        except StillroomRuntimeError as exc:
            # 只接这一层。**不接 `Exception`** —— 编程错误必须继续往上抛，
            # 由 `jsonrpc.serve()` 兜成 `-32603`，否则真缺陷会被伪装成业务失败。
            ctx.emit(f"{name}: {exc.code}: {exc.message} {exc.details or ''}".rstrip())
            return {"ok": False, "error": exc.as_dict()}

    return tool


__all__ = [
    "CONTRACT_VERSION",
    "IDENTITY_FIELDS",
    "InputError",
    "ServerContext",
    "TOOLS",
    "ToolImpl",
    "build_toolset",
    "abort_execution",
    "create_skill",
    "create_workflow",
    "execute_workflow",
    "get_capabilities",
    "get_execution_status",
    "list_skills",
    "list_workflows",
    "match_workflow",
    "resume_execution",
    "retry_execution",
]
