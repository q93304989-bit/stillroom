"""`dispatch.py`：路由。**只有一条判定 —— 方法名在不在表里。**

这个模块薄得可疑，所以测试的重点不是"它做了什么"，而是
**"它没有做别的什么"**：不该拒绝参数、不该碰工具表、不该吞异常、不该做模糊匹配。
每条都用**反向断言**锁（做错时必须有测试变红），否则一个"顺手多做了点事"的
实现照样全绿。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mcp_server.dispatch import ToolFn, make_dispatch
from mcp_server.jsonrpc import METHOD_NOT_FOUND, JsonRpcError, serve

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tool(payload: dict) -> ToolFn:
    """造一个只回固定 payload 的假工具。"""
    return lambda _params: payload


def _echo_tool(record: list) -> ToolFn:
    """造一个把收到的 params 记下来再回显的假工具（用来验透传）。"""
    def tool(params: dict) -> dict:
        record.append(params)
        return {"echo": params}

    return tool


# ---------------------------------------------------------------------------
# 一、happy path：命中就打，返回值原样
# ---------------------------------------------------------------------------

def test_a_known_method_reaches_its_tool_and_returns_its_payload() -> None:
    payload = {"workflows": [{"workflow_id": "a", "version": 1}]}
    dispatch = make_dispatch({"list_workflows": _tool(payload)})

    assert dispatch("list_workflows", {}) == payload


def test_the_tool_payload_is_passed_through_untouched() -> None:
    """**不改写、不加壳、不补字段。** 顺手包一层 `{"ok": true, ...}`
    会让 `tools.py` 与 `dispatch.py` 都以为对方在负责信封 —— 那是两处都漏。"""
    payload = {"ok": False, "error": {"code": "SCHEMA_INVALID", "message": "nope"}}
    dispatch = make_dispatch({"create_workflow": _tool(payload)})

    result = dispatch("create_workflow", {})

    assert result == payload
    assert isinstance(result, dict)
    assert not isinstance(result, JsonRpcError), "业务失败不是传输层错误"


def test_params_are_handed_to_the_tool_as_is() -> None:
    """params 原样进工具：不做补默认值、不做键名重写、不做类型转换。"""
    seen: list[dict] = []
    params = {"workflow_id": "x", "nested": {"a": [1, 2]}, "n": 3}

    make_dispatch({"execute_workflow": _echo_tool(seen)})("execute_workflow", params)

    assert seen == [params]
    assert seen[0] is params, "至少不该在路由这一层复制一份（复制会掩盖下游的原地修改）"


def test_every_registered_method_is_reachable() -> None:
    """表里有什么就能调什么 —— 路由不去猜哪些名字"应该"存在。"""
    names = [f"tool_{i}" for i in range(11)]
    dispatch = make_dispatch({name: _tool({"n": name}) for name in names})

    for name in names:
        assert dispatch(name, {}) == {"n": name}


# ---------------------------------------------------------------------------
# 二、唯一判定：方法名不在表里 → METHOD_NOT_FOUND
# ---------------------------------------------------------------------------

def test_an_unknown_method_is_method_not_found() -> None:
    dispatch = make_dispatch({"get_capabilities": _tool({})})

    outcome = dispatch("no_such_tool", {})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == METHOD_NOT_FOUND
    assert "no_such_tool" in outcome.message


def test_an_unknown_method_never_calls_any_tool() -> None:
    """**反向断言**：只断言"返回了 METHOD_NOT_FOUND"是不够的 ——
    一个"先挨个试、都不匹配才报错"的实现也满足它，而副作用已经发生过了
    （真的建了 workflow、真的起了 execution，撤不回来）。"""
    seen: list[str] = []

    table = {
        name: (lambda params, _n=name: seen.append(_n) or {})
        for name in ("create_workflow", "execute_workflow", "abort_execution")
    }
    dispatch = make_dispatch(table)

    dispatch("execute_worflow", {})          # 少一个 k

    assert seen == [], "拼错的名字绝不能碰到任何工具"


def test_an_empty_toolset_makes_every_method_unknown() -> None:
    dispatch = make_dispatch({})

    for method in ("get_capabilities", "", "init"):
        outcome = dispatch(method, {})
        assert isinstance(outcome, JsonRpcError)
        assert outcome.code == METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "wrong",
    [
        "Get_Capabilities",      # 大小写
        "get_capabilities ",     # 尾空白
        " get_capabilities",     # 首空白
        "get-capabilities",      # 连字符
        "getcapabilities",       # 少下划线
    ],
)
def test_matching_is_exact_and_never_fuzzy(wrong: str) -> None:
    """**精确匹配，不做任何规范化。**

    fuzzy / 归一化匹配的危险不在"多匹配了几个"，而在**静默命中另一个工具**：
    `"execute_workflow"` 拼错成 `"execute-workflow"` 时若被当成合法，
    客户端会以为调用成功，实际拿到的是别的结果。拼错就该报错。
    """
    seen: list[str] = []
    dispatch = make_dispatch(
        {"get_capabilities": lambda _p: seen.append("hit") or {}}
    )

    outcome = dispatch(wrong, {})

    assert isinstance(outcome, JsonRpcError) and outcome.code == METHOD_NOT_FOUND
    assert seen == []


def test_the_error_payload_names_the_method_and_lists_what_exists() -> None:
    """错误里带**有界**的可用清单（工具集由契约固定为 11 个）：
    客户端拼错一个字母时能直接看到正确拼法，不必去翻文档。"""
    dispatch = make_dispatch(
        {"get_capabilities": _tool({}), "abort_execution": _tool({}), "list_skills": _tool({})}
    )

    outcome = dispatch("get_capabilties", {})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.data == {
        "method": "get_capabilties",
        "available": ["abort_execution", "get_capabilities", "list_skills"],
    }


def test_the_available_list_is_sorted_and_stable() -> None:
    """顺序必须确定 —— 否则错误体在两次调用之间会抖，客户端没法比对。"""
    names = ["z_last", "a_first", "m_middle"]
    dispatch = make_dispatch({name: _tool({}) for name in names})

    first = dispatch("nope", {}).data["available"]
    second = dispatch("also_nope", {}).data["available"]

    assert first == sorted(names)
    assert first == second


def test_an_unknown_method_is_logged() -> None:
    """请求路径上 `jsonrpc.serve()` **不会**为 `JsonRpcError` 记日志
    （它把返回值当正常出口），所以拼错方法名这条唯一的线索必须在这里留下。"""
    logs: list[str] = []
    dispatch = make_dispatch({"get_capabilities": _tool({})}, log=logs.append)

    dispatch("typo_here", {})

    assert any("typo_here" in entry for entry in logs)


def test_no_log_sink_means_no_crash() -> None:
    dispatch = make_dispatch({})
    assert isinstance(dispatch("nope", {}), JsonRpcError)


# ---------------------------------------------------------------------------
# 三、工具表在构造时快照
# ---------------------------------------------------------------------------

def test_the_toolset_is_snapshotted_at_construction() -> None:
    """建好之后改调用方那张 dict，**不能**影响已建好的路由。

    否则"服务跑起来之后路由悄悄变了"在并发下无法复现、也无法测试 ——
    这类 bug 只会以"偶尔调错工具"的形式出现。
    """
    table: dict[str, ToolFn] = {"list_skills": _tool({"v": "old"})}
    dispatch = make_dispatch(table)

    table["list_skills"] = _tool({"v": "new"})
    table["brand_new"] = _tool({"v": "surprise"})

    assert dispatch("list_skills", {}) == {"v": "old"}
    assert isinstance(dispatch("brand_new", {}), JsonRpcError)


def test_the_available_list_reflects_the_snapshot_too() -> None:
    """清单与路由必须来自**同一份**快照，否则错误体在骗人。"""
    table: dict[str, ToolFn] = {"a": _tool({})}
    dispatch = make_dispatch(table)
    table["b"] = _tool({})

    assert dispatch("nope", {}).data["available"] == ["a"]


# ---------------------------------------------------------------------------
# 四、非法表项：编程错误，构造时就炸
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_name", ["", 42, None, b"bytes"])
def test_a_bad_method_name_is_rejected_at_construction(bad_name: object) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        make_dispatch({bad_name: _tool({})})          # type: ignore[dict-item]


@pytest.mark.parametrize("bad_value", ["not callable", 3, None, {}, []])
def test_a_non_callable_entry_is_rejected_at_construction(bad_value: object) -> None:
    """fail fast：等第一次调用才发现，定位成本高一个数量级。"""
    with pytest.raises(ValueError, match="not callable"):
        make_dispatch({"tool": bad_value})            # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# 五、异常：这里**不**兜底
# ---------------------------------------------------------------------------

def test_a_tool_exception_propagates_out_of_dispatch() -> None:
    """**这一条是刻意的**：`dispatch` 不 `try/except` 工具。

    - 业务失败（`StillroomRuntimeError`）应由 `tools.py` 的统一包装
      转成结构化 `{"ok": false, ...}` —— 那是业务信封，属工具层；
    - 真编程错误（`TypeError` / `KeyError`）就该往上抛，由 `serve()` 兜成 `-32603`。

    如果这里顺手 catch 了，两种错误都会被伪装成"正常返回"，
    真缺陷在日志里消失 —— 这正是要避免的。
    """
    def exploding(_params: dict) -> dict:
        raise KeyError("workflow_id")

    dispatch = make_dispatch({"execute_workflow": exploding})

    with pytest.raises(KeyError):
        dispatch("execute_workflow", {})


def test_serve_turns_a_tool_exception_into_internal_error() -> None:
    """配套的上半条：抛出去之后，`serve()` 确实把它兜成 `-32603`。"""
    import io
    import json as _json

    def exploding(_params: dict) -> dict:
        raise TypeError("bad wiring")

    dispatch = make_dispatch({"execute_workflow": exploding})
    reader = io.StringIO(
        _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "execute_workflow"}) + "\n"
        + _json.dumps({"jsonrpc": "2.0", "id": 2, "method": "nope"}) + "\n"
    )
    writer = io.StringIO()
    logs: list[str] = []

    code = serve(reader, writer, dispatch, log=logs.append)
    frames = [_json.loads(line) for line in writer.getvalue().splitlines()]

    assert code == 0
    assert frames[0]["error"]["code"] == -32603
    assert "bad wiring" in " ".join(logs)
    assert frames[1]["error"]["code"] == METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# 六、分层：路由不知道业务
# ---------------------------------------------------------------------------

def _imported_modules(path: Path) -> set[str]:
    """收集 import 的模块名，相对导入还原成 `".jsonrpc"` 这种形态。

    两个 AST 细节都得处理，否则有一条走私路径会绕过检查：

    1. `ast.ImportFrom.module` **不含**前导点 —— `from .jsonrpc import X` 的
       `module` 是 `"jsonrpc"`、`level` 是 1。要按 `level` 补回点号，
       否则分不清 `from .jsonrpc` 与 `import jsonrpc`（后者会去第三方包找）。
    2. `from . import tools` 的 `module` 是 **`None`**，导入名全在 `names` 里。
       这时候别名**就是**子模块名，必须一并收进来 ——
       实测这是绕过"不许 import tools"最顺手的一条路。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.module:
                names.add(prefix + node.module)
            elif prefix:
                # `from . import a, b` —— 别名本身是子模块
                names.update(prefix + alias.name for alias in node.names)
    return names


def test_dispatch_does_not_import_the_business_layers() -> None:
    """架构不变量：`dispatch.py` 只依赖 `jsonrpc.py`（拿错误码与类型）。

    一旦它 import 了 `tools` / `runtime` / `validator`，
    就意味着它开始"懂业务"了 —— 要么在多处重复翻译错误，
    要么把业务判断撕成两半。工具表是注入的，这条不变量才有意义。
    """
    imported = _imported_modules(REPO_ROOT / "mcp_server" / "dispatch.py")

    forbidden = {m for m in imported if m.lstrip(".").split(".")[0] in {"runtime", "validator", "app", "tools"}}
    assert forbidden == set(), f"dispatch.py 不该依赖这些：{sorted(forbidden)}"
    assert imported == {".jsonrpc", "__future__", "typing"}, f"意外的 import：{sorted(imported)}"


def test_the_scanner_would_catch_a_business_import(tmp_path: Path) -> None:
    """反证：把扫描器换成正则子串匹配之外的情况 —— 它必须抓得到真的违规。

    没有这条，`forbidden == set()` 可能只是因为它永远返回空集（假绿）。
    """
    path = tmp_path / "leaky.py"
    path.write_text(
        "from __future__ import annotations\n"
        "from typing import Any\n"
        "from runtime.repository import ExecutionRepository\n"
        "from validator.errors import ErrorCode\n"
        "from . import tools\n",
        encoding="utf-8",
    )

    imported = _imported_modules(path)
    forbidden = {m for m in imported if m.lstrip(".").split(".")[0] in {"runtime", "validator", "app", "tools"}}

    assert "runtime.repository" in forbidden
    assert "validator.errors" in forbidden
    assert ".tools" in forbidden
    assert "typing" not in forbidden


def test_a_hit_returns_the_very_object_the_tool_returned() -> None:
    """命中路径上**没有任何再包装**：返回的就是工具返回的那一个对象。

    用 `is`（身份）而不是 `==`（相等）——`dict(result)`、`{"ok": True, **result}`、
    `json.loads(json.dumps(result))` 这类"顺手规整一下"的写法全都过 `==`，
    但会在身份上露馅。这是"dispatch 只有一条判定"最直接的证据。
    """
    payload = {"ok": False, "error": {"code": "INPUT_SCHEMA_INVALID", "message": "x"}}
    tool = _tool(payload)
    dispatch = make_dispatch({"create_workflow": tool})

    assert dispatch("create_workflow", {}) is payload


def test_the_module_stays_small() -> None:
    """薄是**特性**，不是巧合：所有业务判断都在 `tools.py`。

    锁的是"没有偷偷长出第二个判定" —— 判定一多，`tools.py` 就不敢假设
    "params 到我手上时还没被路由挑过毛病"，两个模块的契约就开始互相猜。
    """
    source = (REPO_ROOT / "mcp_server" / "dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try))
    ]
    # 允许的判定：构造期校验(2) + 快照循环(1) + 命中判定(1) + 日志空判(1)
    assert len(branches) <= 5, f"dispatch.py 长出了多余的判定：{len(branches)} 个分支"
