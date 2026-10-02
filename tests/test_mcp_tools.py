"""MCP 工具层：11 个工具 + 统一 binder。

测试重点不是"每个工具都调通了"（那是 e2e 的活），而是**三条边界**：

1. **信封只有一处产生** —— 成功与失败的壳都由 `_bind` 出，工具只返回 `data`。
   任何"工具自己拼 `{"ok": …}`"或"工具自己 `try/except`"都会被反向断言抓住。
2. **`creator_context` 不可由 `params` 注入** —— 伪造尝试必须**报错**，不是被忽略。
3. **异常边界只有 `StillroomRuntimeError`** —— 编程错误必须穿透到 `serve()`
   变成 `-32603`，不能被伪装成业务失败。

每条都配反证（"另一种看起来合理的写法"下必须有测试变红）。
"""

from __future__ import annotations

import ast
import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest

from mcp_server.dispatch import make_dispatch
from mcp_server.jsonrpc import serve
from mcp_server import tools as tools_module
from mcp_server.tools import (
    CONTRACT_VERSION,
    ServerContext,
    TOOLS,
    build_toolset,
)
from runtime import (
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_PENDING_ACTIVATION,
    STATUS_REGISTERED,
    ExecutionRepository,
    KernelError,
    RepositoryError,
    WorkflowRegistry,
)
from runtime.stub_kernel import StubKernel
from validator.errors import ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "workflow"

CONTRACT_TOOLS = [
    "get_capabilities",
    "list_skills",
    "create_skill",
    "list_workflows",
    "match_workflow",
    "create_workflow",
    "execute_workflow",
    "retry_execution",
    "get_execution_status",
    "abort_execution",
    "resume_execution",
]

_CREATOR_T2: dict[str, Any] = {
    "identity": "agent",
    "is_system": False,
    "allowed_capabilities": ["llm.call", "file.read", "file.write"],
    "max_trust_level": "T2",
    "can_auto_activate": False,
}


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _definition(**overrides: Any) -> dict[str, Any]:
    definition = copy.deepcopy(_fixture("valid_minimal.json"))
    definition.update(overrides)
    return definition


@pytest.fixture
def ctx(tmp_path: Path) -> ServerContext:
    db = tmp_path / "protocol.db"
    return ServerContext(
        repo=ExecutionRepository(db),
        registry=WorkflowRegistry(db),
        creator_context=copy.deepcopy(_CREATOR_T2),
    )


@pytest.fixture
def toolset(ctx: ServerContext) -> dict[str, Any]:
    return build_toolset(ctx)


def _call(toolset: dict[str, Any], name: str, params: dict[str, Any]) -> dict[str, Any]:
    return toolset[name](params)


def _data(toolset: dict[str, Any], name: str, params: dict[str, Any]) -> dict[str, Any]:
    envelope = _call(toolset, name, params)
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def _error(toolset: dict[str, Any], name: str, params: dict[str, Any]) -> dict[str, Any]:
    envelope = _call(toolset, name, params)
    assert envelope["ok"] is False, envelope
    return envelope["error"]


def _register_active(toolset: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    definition = _definition(**overrides)
    return _data(toolset, "create_workflow", {"workflow": definition, "activate": True})


def _skill(**overrides: Any) -> dict[str, Any]:
    skill = {
        "skill_id": "summarize",
        "version": 1,
        "description": "把长文压成要点",
        "required_capabilities": ["llm.call"],
        "io_contract": {"input_schema_ref": "schemas/summarize_in.json"},
    }
    skill.update(overrides)
    return skill


# ---------------------------------------------------------------------------
# 一、工具清单与契约一致
# ---------------------------------------------------------------------------

def test_tool_names_match_the_frozen_contract() -> None:
    """`TOOLS` 是"11 个工具"的**唯一**来源，必须与契约 §二 逐字一致。

    这条不只是防漂移 —— 它还防"某次重构漏掉一个工具没人发现"。
    """
    assert sorted(TOOLS) == sorted(CONTRACT_TOOLS)
    assert len(TOOLS) == 11


def test_get_capabilities_reports_exactly_the_tools_that_exist(toolset) -> None:
    """工具清单从 `TOOLS` 现读，不另存常量 —— 否则"清单"和"实现"能各自漂移。"""
    data = _data(toolset, "get_capabilities", {})
    assert data["tools"] == sorted(TOOLS)


def test_get_capabilities_reports_identity_from_the_context(toolset) -> None:
    data = _data(toolset, "get_capabilities", {})
    assert data["identity"] == _CREATOR_T2["identity"]
    assert data["max_trust_level"] == _CREATOR_T2["max_trust_level"]
    assert data["can_auto_activate"] is False
    assert data["protocol_version"] == CONTRACT_VERSION
    assert set(data["capabilities"]) == set(_data_capabilities())


def _data_capabilities() -> set[str]:
    from validator.workflow_validator import KNOWN_CAPABILITIES
    return set(KNOWN_CAPABILITIES)


def test_get_capabilities_takes_no_parameters(toolset) -> None:
    error = _error(toolset, "get_capabilities", {"anything": 1})
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "anything"


# ---------------------------------------------------------------------------
# 二、信封：只有一处产生
# ---------------------------------------------------------------------------

def test_a_success_envelope_has_exactly_ok_and_data(toolset) -> None:
    """**强写法：键集精确相等。** 工具自己多拼一个键就会被抓住。"""
    envelope = _call(toolset, "get_capabilities", {})
    assert set(envelope) == {"ok", "data"}


def test_a_business_failure_envelope_has_exactly_ok_and_error(toolset) -> None:
    envelope = _call(toolset, "get_execution_status", {"execution_id": "exec_missing"})
    assert set(envelope) == {"ok", "error"}
    assert set(envelope["error"]) >= {"code", "message"}


def test_a_business_error_carries_the_code_from_the_shared_enum(toolset) -> None:
    error = _error(toolset, "get_execution_status", {"execution_id": "exec_missing"})
    assert error["code"] == ErrorCode.EXECUTION_NOT_FOUND.value
    assert error["code"] in {c.value for c in ErrorCode}


def test_a_kernel_error_is_also_normalized(toolset, ctx) -> None:
    """**这条专打"只归一化 `RepositoryError`"的窄边界。**

    `KernelError` 是 `RepositoryError` 的兄弟，不是子类。窄边界会让
    `INVALID_TRANSITION`（契约里列出的合法业务错误码）变成 `-32603`，
    客户端读成"服务端内部炸了"，而实际是"你这个状态下不能这么做"。

    造法：把一条 `engine_step_ended` 的 `step` 抹成 `NULL`，
    内核就**推导不出当前步骤**（`_derive_step` 报事件流不可重放），
    于是 `get_execution_status` 里的 `current_step()` 抛 `KernelError`。

    注意必须先 `advance()` 一次：没有 `engine_step_ended` 行时
    `_derive_step` 走的是"还没跑过第一步"的正常分支，**不报错** ——
    只抹 `NULL` 而不先推进，这条用例会假绿。
    """
    execution_id = _start_execution(toolset)
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    kernel.advance()

    ctx.repo._conn.execute(
        "UPDATE execution_events SET step = NULL WHERE execution_id = ?", (execution_id,)
    )
    ctx.repo._conn.commit()

    error = _error(toolset, "get_execution_status", {"execution_id": execution_id})
    assert error["code"] == ErrorCode.INVALID_TRANSITION.value


def test_a_programming_error_is_never_disguised_as_a_business_failure(monkeypatch, ctx) -> None:
    """**编程错误必须穿透 binder。** 用 `TypeError` 代表一整类。

    一个"顺手 `except Exception`"的实现会把它包成 `{"ok": false}` ——
    于是"工具内部写错了"看起来像"业务上做不到"，真缺陷从日志里消失。
    """
    def exploding(_ctx: ServerContext, _params: dict[str, Any]) -> dict[str, Any]:
        raise TypeError("wiring is wrong")

    monkeypatch.setitem(tools_module.TOOLS, "get_capabilities", exploding)
    with pytest.raises(TypeError):
        build_toolset(ctx)["get_capabilities"]({})


def test_a_programming_error_becomes_internal_error_on_the_wire(monkeypatch, ctx) -> None:
    """配套：穿透到 `serve()` 之后确实变成 `-32603`，细节只进 stderr。"""
    def exploding(_ctx: ServerContext, _params: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("secret_key")

    monkeypatch.setitem(tools_module.TOOLS, "get_capabilities", exploding)
    dispatch = make_dispatch(build_toolset(ctx))
    reader = io.StringIO(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"}
    ) + "\n")
    writer = io.StringIO()
    logs: list[str] = []
    serve(reader, writer, dispatch, log=logs.append)

    frame = json.loads(writer.getvalue().strip())
    assert frame["error"]["code"] == -32603
    assert "secret_key" not in json.dumps(frame)
    assert any("secret_key" in entry for entry in logs)


def test_every_entry_of_the_built_toolset_is_wrapped(ctx, monkeypatch) -> None:
    """**binder 覆盖全部条目**，不靠 11 个装饰器各自记得。

    造法：把 `TOOLS` 整张表换成 11 个必抛业务异常的函数，rebuild，
    断言**每一个**名字都返回结构化错误信封。漏包一个就有一条变红。
    """
    def exploding(_ctx: ServerContext, _params: dict[str, Any]) -> dict[str, Any]:
        raise RepositoryError(ErrorCode.EXECUTION_NOT_FOUND, "boom")

    monkeypatch.setattr(
        tools_module, "TOOLS", {name: exploding for name in CONTRACT_TOOLS}
    )
    built = build_toolset(ctx)

    assert sorted(built) == sorted(CONTRACT_TOOLS)
    for name in CONTRACT_TOOLS:
        assert built[name]({}) == {
            "ok": False,
            "error": {"code": ErrorCode.EXECUTION_NOT_FOUND.value, "message": "boom"},
        }, name


def test_the_binder_does_not_catch_broad_exceptions() -> None:
    """源码扫描：`tools.py` 里不许有 `except Exception` / 裸 `except`。

    行为测试能抓住"当前有没有伪装"，但抓不住"下次有人顺手放宽"。
    边界是靠重复维护的，所以直接钉在源码上。

    断言分两半，因为**扫描器只看"宽不宽"，不看"有几处"**：
    宽处理器必须为 0；而整个文件允许的 `except` 子句**恰好 2 条**
    （`_bind` 的归一化 + `_max_tokens_of` 的局部 catch），且类型都是
    `StillroomRuntimeError`。于是"顺手再加一条静默 catch"会被抓到 ——
    只断言前者的话，新加一条 `except ValueError: pass` 是能溜过去的。
    """
    tree = ast.parse((REPO_ROOT / "mcp_server" / "tools.py").read_text(encoding="utf-8"))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    offenders = [
        "bare except" if h.type is None
        else f"except {h.type.id}"
        for h in handlers
        if h.type is None or (isinstance(h.type, ast.Name) and h.type.id in {"Exception", "BaseException"})
    ]

    assert offenders == [], f"tools.py 里出现了过宽的 except：{offenders}"
    assert len(handlers) == 2, (
        f"tools.py 的 except 子句应恰好 2 条（`_bind` + `_max_tokens_of`），"
        f"实际 {len(handlers)} 条：{[ast.unparse(h) for h in handlers]}"
    )
    assert [h.type.id for h in handlers] == ["StillroomRuntimeError"] * 2, (
        f"两条 catch 都必须只接 `StillroomRuntimeError`，实际：{[ast.unparse(h.type) for h in handlers]}"
    )


# ---------------------------------------------------------------------------
# 三、入参纪律
# ---------------------------------------------------------------------------

def test_a_bool_is_not_accepted_where_an_int_is_required(toolset) -> None:
    """`isinstance(True, int)` 为真 —— 不显式挡的话 `{"version": true}` 会被当成 `1`。

    语义错、且不炸，所以只能靠断言抓。`version` 与 `top_k` 两处入参各打一遍。
    """
    for name, params, path in [
        ("execute_workflow", {"workflow_id": "w", "version": True, "request_id": "r", "input": {}}, "version"),
        ("match_workflow", {"intent": "x", "top_k": True}, "top_k"),
    ]:
        error = _error(toolset, name, params)
        assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value, name
        assert error["path"] == path


def test_a_float_version_is_rejected_too(toolset) -> None:
    error = _error(toolset, "execute_workflow", {
        "workflow_id": "w", "version": 3.0, "request_id": "r", "input": {},
    })
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "version"


def test_unknown_parameters_are_rejected_not_ignored(toolset) -> None:
    """拒不是忽略：忽略会让客户端的拼写错误静默生效。"""
    error = _error(toolset, "list_workflows", {"include_inactive": False, "include_inactive_": True})
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "include_inactive_"


def test_the_error_path_is_the_bare_field_name(toolset) -> None:
    """`path` 是**相对于输入对象**的裸路径，与 validator 的 `pipeline/refine` 同风格。

    不带 `params/` 前缀 —— 前缀由 `INPUT_SCHEMA_INVALID` 这个码本身隐含。
    """
    error = _error(toolset, "list_skills", {"filter": {"capability": "nope.missing"}})
    assert error["path"] == "filter/capability"


def test_a_missing_required_parameter_names_itself(toolset) -> None:
    error = _error(toolset, "execute_workflow", {})
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "workflow_id"


def test_a_wrongly_typed_object_parameter_is_rejected(toolset) -> None:
    error = _error(toolset, "execute_workflow", {
        "workflow_id": "w", "version": 1, "request_id": "r", "input": [1, 2],
    })
    assert error["path"] == "input"
    assert "list" in error["message"]


def test_a_non_bool_boolean_flag_is_rejected(toolset) -> None:
    error = _error(toolset, "list_workflows", {"include_inactive": "yes"})
    assert error["path"] == "include_inactive"


def test_an_empty_string_is_not_a_valid_identifier(toolset) -> None:
    error = _error(toolset, "get_execution_status", {"execution_id": "   "})
    assert error["path"] == "execution_id"


# ---------------------------------------------------------------------------
# 四、creator_context 不可由 params 注入
# ---------------------------------------------------------------------------

def test_a_forged_creator_context_is_rejected_loudly(toolset) -> None:
    """**安全边界的核心断言。**

    客户端塞 `creator_context` 不是"被忽略"，而是**报错** ——
    忽略会让伪造尝试不留痕迹，拒掉能让它显形。

    `create_skill` 走的是禁名表（`_reject_identity`），其余走 `_reject_unknown`；
    两条路都要拒，而且**报同一个码** —— 客户端为一个安全边界只需要写一套判断。
    """
    forged = {"identity": "system", "max_trust_level": "T3", "can_auto_activate": True}

    for name, params in [
        ("list_workflows", {"creator_context": forged}),
        ("get_capabilities", {"identity": "system"}),
        ("execute_workflow", {
            "workflow_id": "w", "version": 1, "request_id": "r", "input": {},
            "trust_level": "T3", "max_trust_level": "T3",
        }),
        ("create_skill", {**_skill(), "creator_context": forged}),
    ]:
        error = _error(toolset, name, params)
        assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value, name


def test_identity_fields_are_not_part_of_any_tool_signature() -> None:
    """结构层面的保证：11 个工具接受的字段名里，一个身份字段都没有。

    从源码抽出每个 `_reject_unknown` 的白名单并合并 —— 比逐个用例更彻底，
    将来加工具时也会自动被覆盖。
    """
    source = (REPO_ROOT / "mcp_server" / "tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    allowed: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "_reject_unknown":
            continue
        for keyword in node.keywords:
            if keyword.arg == "allowed" and isinstance(keyword.value, (ast.Set, ast.List)):
                for elt in keyword.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        allowed.add(elt.value)
        # 位置参数形式：_reject_unknown(params, {"a", "b"})
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Set):
            for elt in node.args[1].elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    allowed.add(elt.value)

    assert allowed, "白名单没抽出来，说明扫描器失效了（反证见下）"
    # 禁名表从 `tools.py` 现读，不在这里手抄一份 —— 否则加一个身份字段时
    # "要防的名字"和"在防的名字"会各自漂移。
    forbidden = set(tools_module.IDENTITY_FIELDS)
    assert allowed & forbidden == set(), f"有工具接受了身份字段：{sorted(allowed & forbidden)}"


def test_the_signature_scanner_actually_sees_the_whitelists() -> None:
    """反证：扫描器不是空转 —— 上一条依赖它真的抽到了东西。

    同时验证它认得出**故意违规**的写法（加一个身份字段进白名单）。
    """
    source = (
        "def f(params):\n"
        "    _reject_unknown(params, allowed={'workflow_id'})\n"
        "    _reject_unknown(params, {'top_k'})\n"
        "    _reject_unknown(params, allowed={'creator_context'})\n"
    )
    tree = ast.parse(source)
    allowed: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "_reject_unknown":
            continue
        for keyword in node.keywords:
            if keyword.arg == "allowed" and isinstance(keyword.value, ast.Set):
                allowed.update(e.value for e in keyword.value.elts)
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Set):
            allowed.update(e.value for e in node.args[1].elts)

    assert allowed == {"workflow_id", "top_k", "creator_context"}


#: 契约 §一.1 那条禁令清单的**本地副本**。它的作用不是"权威"，而是**绊线**：
#: 下面那条测试真正读的是 `contracts/mcp-tools.md`，这份字面量只用来
#: 在"契约被重构、解析器抽出了另一堆词"时把测试钉响 —— 而不是悄悄换成别的集合。
CONTRACT_FORBIDDEN_IDENTITY_FIELDS = frozenset({
    "creator_context",
    "identity",
    "is_system",
    "allowed_capabilities",
    "max_trust_level",
    "can_auto_activate",
    "trust_level",
})


def _contract_forbidden_identity_fields() -> frozenset[str]:
    """从契约 §一.1 的代码块里读禁令清单。"""
    text = (REPO_ROOT / "contracts" / "mcp-tools.md").read_text(encoding="utf-8")
    marker = "### 一.1"
    assert marker in text, "契约里找不到 §一.1 —— 扫描器失效了（反证见下）"
    block = text.split(marker, 1)[1].split("```")[1]
    return frozenset(block.split())


def test_identity_fields_match_the_contract() -> None:
    """**禁名表与契约 §一.1 逐字一致。**

    这条要的是"两边都不会自己漂"：契约新增一个身份字段而实现不改，该字段就被静默放行；
    实现多禁一个名字而契约不改，契约里的合法字段就被误拒。**两个方向都不会自己响**。

    实现方式上比"两份字面量互比"再进一步：这里**真的去读契约文件**，
    于是"契约加了字段、测试忘了改"这条路径也不存在了
    （绊线 `CONTRACT_FORBIDDEN_IDENTITY_FIELDS` 挡住的是另一种失效：
    契约被重构后解析器抽出了另一堆词）。
    """
    from_contract = _contract_forbidden_identity_fields()

    assert from_contract == CONTRACT_FORBIDDEN_IDENTITY_FIELDS, "契约与本地副本不一致"
    assert tools_module.IDENTITY_FIELDS == from_contract


def test_the_contract_scan_actually_sees_the_forbidden_names() -> None:
    """反证：解析器不是返回空集。"""
    assert "creator_context" in _contract_forbidden_identity_fields()
    assert "trust_level" in _contract_forbidden_identity_fields()


def _unguarded_tools(source: str, tool_names: list[str]) -> list[str]:
    """返回"没把**顶层** `params` 交给 `_reject_unknown` / `_reject_identity`"的工具名。

    只看 `ast.FunctionDef`（工具是顶层普通函数），只看第一个实参**就是** `params`
    这个名字 —— `_reject_unknown(params["filter"], …)` 那种"只挡了嵌套对象"不算过关。
    """
    functions = {
        node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
    }
    unguarded: list[str] = []
    for name in tool_names:
        node = functions.get(name)
        if node is None:
            unguarded.append(name)
            continue
        guarded = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in {"_reject_unknown", "_reject_identity"}
            and call.args
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "params"
            for call in ast.walk(node)
        )
        if not guarded:
            unguarded.append(name)
    return unguarded


def test_every_tool_guards_its_own_params_envelope() -> None:
    """结构不变式：11 个工具**每一个**都挡了顶层 `params`。

    `create_skill` 的白名单是"定义文档的字段"，所以它挡的是禁名表
    （`_reject_identity`）而不是白名单 —— 两者都算过关。

    这条防的是"将来加第 12 个工具时忘了挡"：逐个行为用例抓不住不存在的东西。
    """
    source = (REPO_ROOT / "mcp_server" / "tools.py").read_text(encoding="utf-8")
    assert sorted(TOOLS)  # 工具表是这一刻的权威清单，不是手抄的
    assert _unguarded_tools(source, sorted(TOOLS)) == []


def test_the_envelope_guard_scanner_detects_a_forgotten_guard() -> None:
    """反证：上一条的扫描器真会**报**，不是永远返回空集。

    四个诱饵各打一种失效方式：正常白名单 / 禁名表 / 完全没挡 /
    只挡了嵌套对象（`params["inner"]` 不算挡了信封）。
    """
    source = (
        "def guarded(params):\n"
        "    _reject_unknown(params, allowed={'a'})\n"
        "def identity_guarded(params):\n"
        "    _reject_identity(params)\n"
        "def forgot(params):\n"
        "    return params\n"
        "def nested_only(params):\n"
        "    _reject_unknown(params['inner'], allowed={'a'})\n"
    )
    assert _unguarded_tools(source, ["guarded", "identity_guarded"]) == []
    assert _unguarded_tools(source, ["forgot"]) == ["forgot"]
    assert _unguarded_tools(source, ["nested_only"]) == ["nested_only"]
    assert _unguarded_tools(source, ["not_even_defined"]) == ["not_even_defined"]


def test_the_context_is_deeply_read_only(ctx) -> None:
    """冻结不是礼仪，是结构：改 `ctx.creator_context` 必须 `TypeError`。

    浅冻结不够 —— `allowed_capabilities` 是个 list，改它才是最容易的那条路。
    """
    with pytest.raises(TypeError):
        ctx.creator_context["identity"] = "system"          # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.creator_context["allowed_capabilities"] += ("git.ops",)   # type: ignore[index]
    assert isinstance(ctx.creator_context["allowed_capabilities"], tuple)


def test_the_context_snapshot_is_independent_of_the_caller(tmp_path: Path) -> None:
    """构造后改原来那份 dict，不影响已建好的 ctx。"""
    creator = {"identity": "agent", "allowed_capabilities": ["llm.call"],
               "max_trust_level": "T2", "can_auto_activate": False}
    db = tmp_path / "protocol.db"
    ctx = ServerContext(
        repo=ExecutionRepository(db), registry=WorkflowRegistry(db), creator_context=creator
    )
    creator["allowed_capabilities"].append("git.ops")

    assert ctx.creator_context["allowed_capabilities"] == ("llm.call",)


def test_the_creator_identity_still_drives_layer3(toolset) -> None:
    """冻结归冻结，值仍然真的参与 L3 判定 —— 否则"安全边界"只是摆设。

    T2 creator 声明 T3 → `INSUFFICIENT_TRUST`（由 ctx 决定，不由 params 决定）。
    """
    definition = _definition(permissions={
        "trust_level": "T3",
        "activation": "human_required",
        "required_capabilities": ["llm.call"],
    })
    error = _error(toolset, "create_workflow", {"workflow": definition})
    assert error["code"] == ErrorCode.INSUFFICIENT_TRUST.value


# ---------------------------------------------------------------------------
# 五、技能
# ---------------------------------------------------------------------------

def _start_execution(toolset: dict[str, Any], request_id: str = "req_1") -> str:
    data = _register_active(toolset)
    execution = _data(toolset, "execute_workflow", {
        "workflow_id": data["workflow_id"],
        "version": data["version"],
        "request_id": request_id,
        "input": {"topic": "x"},
    })
    return execution["execution_id"]


def test_create_skill_returns_the_contract_shape(toolset) -> None:
    data = _data(toolset, "create_skill", _skill())
    assert data == {"skill_id": "summarize", "version": 1, "status": STATUS_REGISTERED}


def test_create_skill_is_idempotent_for_identical_content(toolset) -> None:
    first = _data(toolset, "create_skill", _skill())
    second = _data(toolset, "create_skill", _skill())
    assert first == second


def test_create_skill_rejects_a_different_body_for_the_same_version(toolset) -> None:
    _data(toolset, "create_skill", _skill())
    error = _error(toolset, "create_skill", _skill(description="换了描述"))
    assert error["code"] == ErrorCode.WORKFLOW_VERSION_IMMUTABLE.value
    assert error["path"] == "version"


def test_create_skill_denies_a_capability_the_creator_lacks(toolset) -> None:
    error = _error(toolset, "create_skill", _skill(required_capabilities=["git.ops"]))
    assert error["code"] == ErrorCode.CAPABILITY_DENIED.value
    assert error["path"] == "required_capabilities"


def test_create_skill_rejects_an_unknown_field(toolset) -> None:
    """非身份类的意外字段，形状校验**只有一处**（`validate_skill`），工具不再叠加一遍。

    与下一条并存，两条一起把"两个码的分工"钉住：
    身份字段 → `INPUT_SCHEMA_INVALID`（契约 §一，进定义校验**之前**就被拒），
    其它意外字段 → `SCHEMA_INVALID`（定义文档自身的问题）。
    """
    error = _error(toolset, "create_skill", _skill(bogus=1))
    assert error["code"] == ErrorCode.SCHEMA_INVALID.value


def test_create_skill_rejects_identity_fields_with_the_input_code(toolset) -> None:
    """`create_skill` 的 `params` 就是定义文档，**但这不改变身份字段的码**。

    它走的是 `_reject_identity`（禁名表），不是定义校验 ——
    否则同一个伪造尝试在 10 个工具上报 `INPUT_SCHEMA_INVALID`、
    在 `create_skill` 上报 `SCHEMA_INVALID`，客户端就得为一个安全边界写两套判断。
    """
    for field in sorted(tools_module.IDENTITY_FIELDS):
        error = _error(toolset, "create_skill", _skill(**{field: "spoofed"}))
        assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value, field
        assert error["path"] == field, field


def test_list_skills_returns_the_contract_shape(toolset) -> None:
    _data(toolset, "create_skill", _skill())
    data = _data(toolset, "list_skills", {})

    assert data == {"skills": [{
        "skill_id": "summarize",
        "version": 1,
        "description": "把长文压成要点",
        "required_capabilities": ["llm.call"],
    }]}


def test_list_skills_filters_by_capability(toolset) -> None:
    _data(toolset, "create_skill", _skill())
    _data(toolset, "create_skill", _skill(
        skill_id="read_file", description="读文件", required_capabilities=["file.read"],
    ))

    only_llm = _data(toolset, "list_skills", {"filter": {"capability": "llm.call"}})
    assert [s["skill_id"] for s in only_llm["skills"]] == ["summarize"]

    none = _data(toolset, "list_skills", {"filter": {"capability": "git.ops"}})
    assert none == {"skills": []}


def test_list_skills_accepts_an_empty_filter(toolset) -> None:
    _data(toolset, "create_skill", _skill())
    assert len(_data(toolset, "list_skills", {"filter": {}})["skills"]) == 1


def test_an_unknown_capability_in_the_filter_is_rejected(toolset) -> None:
    error = _error(toolset, "list_skills", {"filter": {"capability": "not.a.capability"}})
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "filter/capability"


def test_the_filter_object_rejects_unknown_keys(toolset) -> None:
    error = _error(toolset, "list_skills", {"filter": {"caps": "llm.call"}})
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "filter/caps"


# ---------------------------------------------------------------------------
# 六、工作流
# ---------------------------------------------------------------------------

def test_create_workflow_returns_the_contract_shape(toolset) -> None:
    data = _data(toolset, "create_workflow", {"workflow": _definition()})
    assert data == {
        "workflow_id": "minimal_job",
        "version": 1,
        "status": STATUS_PENDING_ACTIVATION,
        "trust_level": "T2",
    }


def test_create_workflow_can_activate_in_the_same_call(toolset) -> None:
    data = _data(toolset, "create_workflow", {"workflow": _definition(), "activate": True})
    assert data["status"] == STATUS_ACTIVE


def test_create_workflow_leaves_it_pending_when_not_asked_to_activate(toolset) -> None:
    data = _data(toolset, "create_workflow", {"workflow": _definition(), "activate": False})
    assert data["status"] == STATUS_PENDING_ACTIVATION


def test_create_workflow_surfaces_the_validator_code(toolset) -> None:
    """L1–L4 的码要原样透给客户端，不要被改写成通用的 `SCHEMA_INVALID`。"""
    definition = _definition(metadata={"permissions": {"trust_level": "T3"}})
    error = _error(toolset, "create_workflow", {"workflow": definition})
    assert error["code"] == ErrorCode.METADATA_FORBIDDEN.value
    assert error["path"].startswith("metadata/")


def test_create_workflow_requires_the_workflow_object(toolset) -> None:
    error = _error(toolset, "create_workflow", {"workflow": "not an object"})
    assert error["path"] == "workflow"


def test_list_workflows_hides_inactive_ones_by_default(toolset) -> None:
    _data(toolset, "create_workflow", {"workflow": _definition(), "activate": True})
    _data(toolset, "create_workflow", {"workflow": _definition(
        workflow_id="pending_job", version=1)}, )

    visible = _data(toolset, "list_workflows", {})["workflows"]
    assert [w["workflow_id"] for w in visible] == ["minimal_job"]

    everything = _data(toolset, "list_workflows", {"include_inactive": True})["workflows"]
    assert sorted(w["workflow_id"] for w in everything) == ["minimal_job", "pending_job"]


def test_list_workflows_returns_the_contract_shape(toolset) -> None:
    _data(toolset, "create_workflow", {"workflow": _definition(), "activate": True})
    entry = _data(toolset, "list_workflows", {})["workflows"][0]

    assert set(entry) == {"workflow_id", "version", "display_name", "trust_level", "active"}
    assert entry["display_name"] == "minimal_job", "定义没写 display_name 时回落到 ID"


def test_list_workflows_uses_display_name_from_the_definition(toolset) -> None:
    _data(toolset, "create_workflow", {
        "workflow": _definition(display_name="极简任务"), "activate": True,
    })
    assert _data(toolset, "list_workflows", {})["workflows"][0]["display_name"] == "极简任务"


def test_list_workflows_still_lists_a_deprecated_definition(toolset, ctx) -> None:
    """下架不删定义：`include_inactive` 时要能看到它，`display_name` 仍读得出来。"""
    _data(toolset, "create_workflow", {
        "workflow": _definition(display_name="旧版"), "activate": True,
    })
    ctx.registry.deprecate("minimal_job", 1)

    entry = _data(toolset, "list_workflows", {"include_inactive": True})["workflows"][0]
    assert entry["display_name"] == "旧版" and entry["active"] is False


# ---------------------------------------------------------------------------
# 七、match_workflow（P1 桩）
# ---------------------------------------------------------------------------

def test_match_workflow_returns_the_contractual_none_exit(toolset) -> None:
    """P1 恒 `none`。**这是契约里的合法出口，不是占位符** —— 断言形状而不只是"有返回"。"""
    data = _data(toolset, "match_workflow", {"intent": "写一篇关于猫的文章"})
    assert data == {"match": "none", "candidates": []}


def test_match_workflow_accepts_an_omitted_top_k(toolset) -> None:
    assert _data(toolset, "match_workflow", {"intent": "x"})["match"] == "none"


def test_match_workflow_requires_an_intent(toolset) -> None:
    assert _error(toolset, "match_workflow", {})["path"] == "intent"


def test_the_injected_router_replaces_the_stub(tmp_path: Path) -> None:
    """注入点真的接通了：换掉 `router` 就换掉行为，`tools.py` 一行不用改。"""
    calls: list[tuple[str, int]] = []

    def router(intent: str, top_k: int) -> dict[str, Any]:
        calls.append((intent, top_k))
        return {"match": "candidates", "candidates": [
            {"workflow_id": "minimal_job", "version": 1, "confidence": 0.9, "reason": "像"}
        ]}

    db = tmp_path / "protocol.db"
    ctx = ServerContext(
        repo=ExecutionRepository(db),
        registry=WorkflowRegistry(db),
        creator_context=dict(_CREATOR_T2),
        router=router,
    )
    data = _data(build_toolset(ctx), "match_workflow", {"intent": "写文章", "top_k": 5})

    assert calls == [("写文章", 5)]
    assert data["match"] == "candidates"


# ---------------------------------------------------------------------------
# 八、execute_workflow
# ---------------------------------------------------------------------------

def test_execute_returns_the_contract_shape(toolset) -> None:
    execution_id = _start_execution(toolset)
    assert execution_id.startswith("exec_")


def test_the_same_request_id_returns_the_very_same_execution(toolset) -> None:
    """幂等不变式：`request_id` 永久绑定首次 execution。"""
    _register_active(toolset)
    params = {"workflow_id": "minimal_job", "version": 1, "request_id": "req_a", "input": {"x": 1}}
    first = _data(toolset, "execute_workflow", params)
    second = _data(toolset, "execute_workflow", params)

    assert first["execution_id"] == second["execution_id"]
    assert first["is_duplicate"] is False
    assert second["is_duplicate"] is True


def test_a_duplicate_reports_the_current_status_not_pending(toolset, ctx) -> None:
    """**幂等命中时必须报当前状态。** 报 `PENDING` 会让客户端以为"刚起了个新的"。

    造法：跑到 `COMPLETED` 再重发同一个 `request_id`。
    """
    _register_active(toolset)
    params = {"workflow_id": "minimal_job", "version": 1, "request_id": "req_b", "input": {}}
    execution_id = _data(toolset, "execute_workflow", params)["execution_id"]
    assert StubKernel(ctx.repo, execution_id).run_to_completion() == "COMPLETED"

    again = _data(toolset, "execute_workflow", params)
    assert again["execution_id"] == execution_id
    assert again["status"] == "COMPLETED", "报了 PENDING 就是把既有执行伪装成新的"


def test_a_reused_request_id_with_different_input_is_flagged(toolset, ctx) -> None:
    """不该静默：`BindResult.input_mismatch` 是刻意设计来暴露客户端 bug 的。"""
    _register_active(toolset)
    _data(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "req_c", "input": {"x": 1},
    })
    again = _data(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "req_c", "input": {"x": 2},
    })

    assert again["is_duplicate"] is True
    assert again["input_mismatch"] is True


def test_a_fresh_execution_does_not_carry_the_mismatch_flag(toolset) -> None:
    """反方向：别把标志位永远钉上 —— 那样它就没有信息量了。"""
    _register_active(toolset)
    data = _data(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "req_d", "input": {},
    })
    assert "input_mismatch" not in data


def test_execute_on_an_unregistered_workflow_is_not_found(toolset) -> None:
    error = _error(toolset, "execute_workflow", {
        "workflow_id": "nope", "version": 1, "request_id": "r", "input": {},
    })
    assert error["code"] == ErrorCode.WORKFLOW_NOT_FOUND.value


def test_execute_on_a_pending_workflow_is_not_active(toolset) -> None:
    _data(toolset, "create_workflow", {"workflow": _definition()})
    error = _error(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "r", "input": {},
    })
    assert error["code"] == ErrorCode.WORKFLOW_NOT_ACTIVE.value


def test_execute_pins_the_definition_fingerprint(toolset, ctx) -> None:
    """执行记录必须钉住**当版**定义的指纹与地址（不是"当前 active 那版"）。"""
    data = _register_active(toolset)
    execution_id = _start_execution(toolset, request_id="req_e")
    record = ctx.repo.get(execution_id)
    registered = ctx.registry.get(data["workflow_id"], data["version"])

    assert record.workflow_definition_hash == registered.definition_hash
    assert record.workflow_definition_ref == registered.definition_ref


def test_execute_records_the_creators_trust_level_snapshot(toolset, ctx) -> None:
    execution_id = _start_execution(toolset, request_id="req_f")
    assert ctx.repo.get(execution_id).trust_level_at_creation == _CREATOR_T2["max_trust_level"]


def test_execute_rejects_forbidden_metadata_tool(toolset) -> None:
    """L4 是**每次调用**都要成立的性质，不是只在登记定义时成立。

    定义干净、调用时塞 `permissions` —— 只扫定义的话这条会漏。
    """
    _register_active(toolset)
    error = _error(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "r", "input": {},
        "metadata": {"note": "ok", "permissions": {"trust_level": "T3"}},
    })
    assert error["code"] == ErrorCode.METADATA_FORBIDDEN.value
    assert error["path"] == "metadata/permissions"


def test_execute_accepts_harmless_metadata(toolset) -> None:
    _register_active(toolset)
    data = _data(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "req_g", "input": {},
        "metadata": {"trace": {"note": "ok"}, "tags": ["a", "b"]},
    })
    assert data["is_duplicate"] is False


def test_execute_rejects_a_non_object_metadata(toolset) -> None:
    _register_active(toolset)
    error = _error(toolset, "execute_workflow", {
        "workflow_id": "minimal_job", "version": 1, "request_id": "r", "input": {},
        "metadata": "nope",
    })
    assert error["path"] == "metadata"


# ---------------------------------------------------------------------------
# 九、retry_execution
# ---------------------------------------------------------------------------

def test_retry_creates_a_new_execution_with_a_parent(toolset, ctx) -> None:
    """重试要求原执行在 **FAILED**（不是 `COMPLETED`，后者是终态里的成功出口）。

    这里刻意用 `start()` + `fail()` 而不是 `run_to_completion()`：
    后者推到 `COMPLETED`，再补 `engine_failed` 会被状态机挡成 `ALREADY_TERMINAL` ——
    那测的就不是 `retry_execution` 了。
    """
    execution_id = _start_execution(toolset, request_id="req_r1")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    kernel.fail("SEMANTIC_INVALID")

    data = _data(toolset, "retry_execution", {
        "execution_id": execution_id, "request_id": "req_r1",
    })

    assert data["execution_id"] != execution_id
    assert data["parent_execution_id"] == execution_id
    assert data["status"] == "PENDING"


def test_retry_rejects_a_request_id_from_another_family(toolset, ctx) -> None:
    """`request_id` 是**声明**，不是装饰：与 execution 实际所属不符就拒。

    不拒的话，客户端会以为自己在重试 A，实际重试的是 B。
    """
    execution_id = _start_execution(toolset, request_id="req_r2")
    error = _error(toolset, "retry_execution", {
        "execution_id": execution_id, "request_id": "some_other_request",
    })
    assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value
    assert error["path"] == "request_id"


def test_retry_has_no_override_knobs(toolset) -> None:
    """契约 §二.8：入参里**没有** override 字段。多传就拒。"""
    for field in ("input", "max_retries", "budget_raised", "override_input"):
        error = _error(toolset, "retry_execution", {
            "execution_id": "exec_x", "request_id": "r", field: 1,
        })
        assert error["code"] == ErrorCode.INPUT_SCHEMA_INVALID.value, field


def test_retry_of_a_missing_execution_is_not_found(toolset) -> None:
    error = _error(toolset, "retry_execution", {
        "execution_id": "exec_missing", "request_id": "r",
    })
    assert error["code"] == ErrorCode.EXECUTION_NOT_FOUND.value


# ---------------------------------------------------------------------------
# 十、get_execution_status / abort / resume
# ---------------------------------------------------------------------------

def test_status_returns_the_contract_shape(toolset) -> None:
    execution_id = _start_execution(toolset, request_id="req_s1")
    data = _data(toolset, "get_execution_status", {"execution_id": execution_id})

    assert set(data) == {"status", "current_step", "steps", "budget", "artifacts", "error"}
    assert data["status"] == "PENDING"
    assert data["current_step"] is None
    assert data["budget"]["tokens_used"] == 0
    assert data["artifacts"] == []
    assert data["error"] is None


def test_status_reports_max_tokens_from_the_definition(toolset) -> None:
    _register_active(toolset)
    execution_id = _start_execution(toolset, request_id="req_s2")
    data = _data(toolset, "get_execution_status", {"execution_id": execution_id})

    expected = _definition()["resource_policy"]["max_tokens"]
    assert data["budget"]["max_tokens"] == expected


def test_status_tracks_the_running_step(toolset, ctx) -> None:
    execution_id = _start_execution(toolset, request_id="req_s3")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    kernel.advance()

    data = _data(toolset, "get_execution_status", {"execution_id": execution_id})
    assert data["status"] == "RUNNING"
    assert data["current_step"] == "reference"
    assert data["steps"][0] == {"step": "understand", "status": "done"}
    assert data["steps"][1] == {"step": "reference", "status": "running"}
    assert data["steps"][-1]["status"] == "pending"


def test_status_reports_the_error_code_on_failure(toolset, ctx) -> None:
    execution_id = _start_execution(toolset, request_id="req_s4")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    kernel.fail("SEMANTIC_INVALID")

    data = _data(toolset, "get_execution_status", {"execution_id": execution_id})
    assert data["status"] == "FAILED"
    assert data["error"] == "SEMANTIC_INVALID"


def test_status_of_a_missing_execution_is_not_found(toolset) -> None:
    error = _error(toolset, "get_execution_status", {"execution_id": "exec_missing"})
    assert error["code"] == ErrorCode.EXECUTION_NOT_FOUND.value


def test_aborting_a_pending_execution_lands_aborted(toolset) -> None:
    execution_id = _start_execution(toolset, request_id="req_a1")
    data = _data(toolset, "abort_execution", {"execution_id": execution_id, "reason": "不要了"})
    assert data == {"status": "ABORTED"}


def test_aborting_a_running_execution_lands_abort_pending(toolset, ctx) -> None:
    """**中止不掐断步骤** —— 这正是两段式存在的理由。"""
    execution_id = _start_execution(toolset, request_id="req_a2")
    StubKernel(ctx.repo, execution_id).start()

    data = _data(toolset, "abort_execution", {"execution_id": execution_id})
    assert data == {"status": "ABORT_PENDING"}


def test_aborting_a_terminal_execution_is_already_terminal(toolset, ctx) -> None:
    execution_id = _start_execution(toolset, request_id="req_a3")
    StubKernel(ctx.repo, execution_id).run_to_completion()

    error = _error(toolset, "abort_execution", {"execution_id": execution_id})
    assert error["code"] == ErrorCode.ALREADY_TERMINAL.value


def test_the_abort_reason_is_recorded_on_the_event(toolset, ctx) -> None:
    execution_id = _start_execution(toolset, request_id="req_a4")
    _data(toolset, "abort_execution", {"execution_id": execution_id, "reason": "预算不够"})

    events = ctx.repo.list_events(execution_id)
    assert events[-1].type == "abort_requested"
    assert events[-1].message == "预算不够"


def test_abort_is_optional_reason(toolset) -> None:
    execution_id = _start_execution(toolset, request_id="req_a5")
    assert _data(toolset, "abort_execution", {"execution_id": execution_id})["status"] == "ABORTED"


def test_resume_outside_waiting_input_is_not_waiting_input(toolset) -> None:
    """契约 §二.11 要的是 `NOT_WAITING_INPUT`，不是泛泛的 `INVALID_TRANSITION`。

    后者会让客户端以为"状态机坏了"，而实际是"你调错了时机"。

    **注意这条不能证明 `resume_execution` 里有前置检查** ——
    这个码由 `state_machine.py` 的专门分支给出，检查在不在都一样。
    守那个检查的是下一条（副作用）。
    """
    execution_id = _start_execution(toolset, request_id="req_u1")
    error = _error(toolset, "resume_execution", {"execution_id": execution_id, "input": {}})
    assert error["code"] == ErrorCode.NOT_WAITING_INPUT.value


def _store_files(ctx) -> set[Path]:
    """store 里实际落盘的文件（内容寻址，一次写入一个文件）。

    用**文件数**而不是"算一遍期望的 sha256 再 `has()`"：后者要在测试里
    复制一份规范化编码的规则，格式一变断言就空转通过（假绿）。
    """
    return {p for p in (ctx.artifacts.root / "sha256").rglob("*") if p.is_file()}


def test_a_rejected_resume_leaves_no_artifact_behind(toolset, ctx) -> None:
    """**这条才是前置检查的守卫。**

    被拒绝的调用不许留下副作用：`resume_execution` 先判状态再写 store，
    所以非 `WAITING_INPUT` 的调用**一个文件都不该多**。

    反证：把 `tools.py` 里那句 `if record.state != WAITING_INPUT` 换成 `pass`
    （`tools/prove_mcp_server.py` 的场景 11），错误码仍然对 ——
    但如果只断言错误码，这条变异会溜过去；断言文件数就会红。
    """
    execution_id = _start_execution(toolset, request_id="req_u5")
    before = _store_files(ctx)

    error = _error(toolset, "resume_execution", {
        "execution_id": execution_id, "input": {"不该被落盘": True},
    })

    assert error["code"] == ErrorCode.NOT_WAITING_INPUT.value
    assert _store_files(ctx) == before, "被拒绝的 resume 往 store 里写了东西 —— 前置检查被绕过了"


def test_resume_from_waiting_input_returns_running(toolset, ctx) -> None:
    execution_id = _start_execution(toolset, request_id="req_u2")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    ctx.repo.append_event(execution_id, "input_required")

    data = _data(toolset, "resume_execution", {
        "execution_id": execution_id, "input": {"需要补充的": "内容"},
    })
    assert data == {"status": "RUNNING"}


def test_the_resume_payload_is_stored_content_addressed(toolset, ctx) -> None:
    """补上来的输入进 store，事件只带 ref/hash —— 原快照仍然不可变。"""
    execution_id = _start_execution(toolset, request_id="req_u3")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    ctx.repo.append_event(execution_id, "input_required")
    before = ctx.repo.get(execution_id).input_snapshot

    _data(toolset, "resume_execution", {
        "execution_id": execution_id, "input": {"补": 1},
    })

    event = ctx.repo.list_events(execution_id)[-1]
    assert event.type == "resume_requested"
    assert event.payload_ref and event.payload_ref.startswith("artifact:sha256:")
    assert ctx.artifacts.get_json(ctx.artifacts.parse_ref(event.payload_ref)) == {"补": 1}
    assert ctx.repo.get(execution_id).input_snapshot == before, "原快照必须逐字节不变"


def test_the_resume_payload_does_not_become_the_delivery_pointer(toolset, ctx) -> None:
    """`output_ref` 只认进入 `COMPLETED` 的事件 —— 恢复载荷不能污染它。"""
    execution_id = _start_execution(toolset, request_id="req_u4")
    kernel = StubKernel(ctx.repo, execution_id)
    kernel.start()
    ctx.repo.append_event(execution_id, "input_required")
    _data(toolset, "resume_execution", {"execution_id": execution_id, "input": {"补": 1}})
    kernel.run_to_completion()

    record = ctx.repo.verify_consistency(execution_id)
    assert record.state == "COMPLETED"
    assert record.output_ref is not None
    assert "resume" not in record.output_ref


# ---------------------------------------------------------------------------
# 十一、端到端：走一次真正的协议流
# ---------------------------------------------------------------------------

def test_a_full_session_over_the_wire(ctx) -> None:
    """一次握手 → 建工作流 → 起执行 → 查状态，全程走 stdout 上的协议帧。

    这是把三层（jsonrpc → dispatch → tools）接起来的唯一一条腿：
    少了它，三层各自的测试全绿也可能拼不起来。
    """
    dispatch = make_dispatch(build_toolset(ctx))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"},
        {"jsonrpc": "2.0", "id": 2, "method": "create_workflow",
         "params": {"workflow": _definition(), "activate": True}},
        {"jsonrpc": "2.0", "id": 3, "method": "execute_workflow",
         "params": {"workflow_id": "minimal_job", "version": 1,
                    "request_id": "req_e2e", "input": {}}},
    ]
    reader = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    writer = io.StringIO()
    logs: list[str] = []

    code = serve(reader, writer, dispatch, log=logs.append)
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]

    assert code == 0
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert all(f["result"]["ok"] is True for f in frames), frames

    execution_id = frames[2]["result"]["data"]["execution_id"]
    assert frames[0]["result"]["data"]["tools"] == sorted(TOOLS)

    # 再走一次：查状态
    reader = io.StringIO(json.dumps(
        {"jsonrpc": "2.0", "id": 4, "method": "get_execution_status",
         "params": {"execution_id": execution_id}}
    ) + "\n")
    writer = io.StringIO()
    serve(reader, writer, dispatch, log=logs.append)
    frame = json.loads(writer.getvalue().strip())

    assert frame["result"]["ok"] is True
    assert frame["result"]["data"]["status"] == "PENDING"


def test_a_business_failure_over_the_wire_stays_in_result(ctx) -> None:
    """**跨层的分工断言**：业务失败在 `result` 里，`error` 字段留给传输问题。

    这一条与 `test_mcp_jsonrpc.py` 的同名性质互为独立验证 ——
    那边用假 dispatch 验证传输层，这里用真工具验证整条链没有把它写歪。
    """
    dispatch = make_dispatch(build_toolset(ctx))
    reader = io.StringIO("".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "get_execution_status",
                    "params": {"execution_id": "exec_missing"}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "no_such_tool"}) + "\n",
    ]))
    writer = io.StringIO()
    serve(reader, writer, dispatch, log=lambda _m: None)
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]

    assert "error" not in frames[0]
    assert frames[0]["result"]["ok"] is False
    assert frames[0]["result"]["error"]["code"] == ErrorCode.EXECUTION_NOT_FOUND.value

    assert "result" not in frames[1]
    assert frames[1]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# 十二、分层
# ---------------------------------------------------------------------------

def test_tools_never_import_the_gui_layer() -> None:
    """无头层不许 import PySide6 / `app.*` —— P1 的验收硬条件。"""
    tree = ast.parse((REPO_ROOT / "mcp_server" / "tools.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))

    forbidden = {m for m in imported if m.lstrip(".").split(".")[0] in {"app", "PySide6"}}
    assert forbidden == set()
