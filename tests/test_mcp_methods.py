"""MCP 方法层（`mcp.py`）：wire 上是**三个方法**，不是十一个工具名。

这个文件锁三件事：

1. **方法命名空间** —— `initialize` / `tools/list` / `tools/call`，且工具名
   **不再是**方法名（发 `{"method":"get_capabilities"}` 必须拿 `-32601`）。
   这条不守，标准 MCP 客户端一个都连不上，而 P1 的阶段目标恰恰是"能被 MCP 客户端连上"。
2. **清单的单一来源** —— `tools/list` 的名字取自工具表本身、描述取自
   `TOOL_SUMMARIES`，两者与 `tools.TOOLS` 的键集**机器比对**（读契约 + 字面量绊线）。
3. **错误码分工的边界** —— 未知工具 / `arguments` 不是对象是**协议**错误（`-32602`）；
   工具自己说 `{"ok": false}` 是**工具执行失败**（`result` 里 `isError: true`）。
   混起来客户端就分不清"调用写错了"与"工具拒绝了"。

与方法学有关的两处刻意写法：

- `isError` 的判据是 `{"ok": True}`（`is True`），所以专门用 `{"ok": 1}` 试探 ——
  `1 == True` 在 Python 里成立，用真值判断的实现在这条上会静默放行。
- 通知那条同时断言**恰好一条**响应：只断言"没有响应"在整条流都没响应时也是真的，
  那是假通过。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from mcp_server.dispatch import make_dispatch
from mcp_server.jsonrpc import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JsonRpcError,
    serve,
)
from mcp_server.mcp import (
    INVALID_PARAMS_REASONS,
    MCP_VERSION,
    NOTIFICATION_METHODS,
    REASON_INVALID_ARGUMENTS,
    REASON_INVALID_NAME,
    REASON_UNEXPECTED_PARAM,
    REASON_UNKNOWN_TOOL,
    SERVER_NAME,
    SERVER_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOL_SUMMARIES,
    make_method_table,
)
from mcp_server.tools import TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "contracts" / "mcp-tools.md"

METHOD_NAMESPACE = ("initialize", "tools/list", "tools/call")
"""契约 §五.7 的三个方法。字面量绊线：契约里那张表被删/改写时这里要一起动。"""


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

def _tool(result: Any, seen: list[dict[str, Any]] | None = None) -> Any:
    """最简工具：回固定结果（`Exception` 则抛出），可选记录收到的 params。"""
    def call(params: dict[str, Any]) -> Any:
        if seen is not None:
            seen.append(params)
        if isinstance(result, BaseException):
            raise result
        return result
    return call


def _table(toolset: dict[str, Any] | None = None) -> dict[str, Any]:
    """建一张方法表。缺省工具集只有一个叫 `a` 的假工具 —— 路由测试只需要它。"""
    return make_method_table(
        toolset if toolset is not None else {"a": _tool({"ok": True, "data": 1})},
        log=lambda _m: None,
    )


def _call(method: str, params: dict[str, Any], toolset: dict[str, Any] | None = None) -> Any:
    return _table(toolset)[method](params)


def _run(stream: str, toolset: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """跑一遍真正的 `serve()`（用假工具表），返回响应信封列表。"""
    reader = io.StringIO(stream)
    writer = io.StringIO()
    dispatch = make_dispatch(_table(toolset), log=lambda _m: None)
    serve(reader, writer, dispatch, log=lambda _m: None)
    raw = writer.getvalue()
    return [json.loads(line) for line in raw.splitlines() if line]


def _request(method: str, *, request_id: Any = 1, params: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body, ensure_ascii=False) + "\n"


def _notification(method: str, params: dict[str, Any] | None = None) -> str:
    """通知：**没有** `id` 字段。故意不叫 `_request` —— 差别就在这个字段。"""
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body, ensure_ascii=False) + "\n"


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    """把 `tools/call` 的 MCP content 还原成内部信封。"""
    return json.loads(response["result"]["content"][0]["text"])


def _contract_section(heading: str) -> str:
    """取契约里 `### <heading>` 到下一个二级标题之间的正文。"""
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.index("\n## ", start)
    return text[start:end]


def _tables(text: str) -> list[list[str]]:
    """把正文里一段段连续的 `|` 行切成一张张表（每张表是行的列表）。"""
    tables: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("|"):
            current.append(line)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _first_cell(line: str) -> str:
    """一行表格的第一格内容（去掉空白与反引号）：`| \\`initialize\\` | …` → `initialize`。"""
    return line.split("|")[1].strip().strip("`").strip()


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_/]*")


def _contract_wire_methods() -> list[str]:
    """契约 §五.7 **第一张表**的第一列 = 三个方法名。

    取"第一张表"而不是"所有以 ``| ` `` 开头的行"：§五.7 后面还有 `data.reason` 表，
    它每行也是 ``| `unknown_tool` | …`` —— 一起扫进来会多出四个"方法名"，
    于是"恰好三个"那条断言变成**必然假红**。这不是假想：
    加 `reason` 表的那次改动就踩了一遍。
    """
    table = _tables(_contract_section("### 五.7"))[0]
    return [cell for cell in map(_first_cell, table) if _IDENTIFIER.fullmatch(cell)]


def _contract_reasons() -> set[str]:
    """契约 §五.7 里 `data.reason` 那张表的取值（按表头认表，不按位置）。"""
    for table in _tables(_contract_section("### 五.7")):
        if "reason" in _first_cell(table[0]):
            return {
                cell for cell in map(_first_cell, table[1:]) if _IDENTIFIER.fullmatch(cell)
            }
    raise AssertionError("契约 §五.7 里找不到 `data.reason` 那张表")


# ---------------------------------------------------------------------------
# 一、方法命名空间：三个，不多不少
# ---------------------------------------------------------------------------

def test_the_wire_exposes_exactly_the_three_contract_methods() -> None:
    """方法名**逐个**与契约 §五.7 的表比对 —— 读契约文件，不是再抄一份字面量。"""
    assert _contract_wire_methods() == list(METHOD_NAMESPACE)

    table = _table()
    declared = [name for name in table if name not in NOTIFICATION_METHODS]
    assert sorted(declared) == sorted(METHOD_NAMESPACE)


def test_the_contract_namespace_scan_actually_sees_the_table() -> None:
    """反证：契约扫描器不是空转（它永远返回 `[]` 的话上面那条也是假绿）。"""
    names = _contract_wire_methods()
    assert names, "契约 §五.7 里没扫到方法表 —— 要么表没了，要么扫描器坏了"
    assert "initialize" in names


def test_a_tool_name_is_no_longer_a_method() -> None:
    """**这次改动的核心断言**：11 个工具名不再是方法名。

    不锁这条的话，"换了一张表"可以退化成"又加了一张表"，而标准 MCP 客户端
    依然连不上（它只会发 `tools/call`）。
    """
    toolset = {"get_capabilities": _tool({"ok": True, "data": {}}), "execute_workflow": _tool({})}
    dispatch = make_dispatch(_table(toolset), log=lambda _m: None)

    for tool_name in toolset:
        outcome = dispatch(tool_name, {})
        assert isinstance(outcome, JsonRpcError), f"{tool_name} 不该再是方法名"
        assert outcome.code == METHOD_NOT_FOUND


def test_the_unknown_method_error_lists_the_methods_not_the_tools() -> None:
    """拼错方法名时回的是**方法表**（3 个方法 + 4 个通知），不是工具清单（11 个）。"""
    dispatch = make_dispatch(_table(), log=lambda _m: None)
    outcome = dispatch("tools/calll", {})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == METHOD_NOT_FOUND
    assert outcome.data is not None
    assert "tools/call" in outcome.data["available"]
    assert "get_capabilities" not in outcome.data["available"]


def test_all_three_methods_are_reachable_through_dispatch() -> None:
    dispatch = make_dispatch(_table(), log=lambda _m: None)

    for method in METHOD_NAMESPACE:
        assert not isinstance(dispatch(method, {"name": "a"} if method == "tools/call" else {}),
                              JsonRpcError), f"{method} 应当可达"


# ---------------------------------------------------------------------------
# 二、initialize
# ---------------------------------------------------------------------------

def test_initialize_reports_version_capabilities_and_server_info() -> None:
    result = _call("initialize", {})

    assert result["protocolVersion"] == MCP_VERSION
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"] == {"name": SERVER_NAME, "version": SERVER_VERSION}


def test_initialize_echoes_a_supported_client_version() -> None:
    """MCP 的对齐规则：客户端请求的版本受支持就**原样回**，不是一律回自己的。"""
    assert _call("initialize", {"protocolVersion": MCP_VERSION})["protocolVersion"] == MCP_VERSION


def test_initialize_answers_its_own_version_when_the_client_asks_for_another() -> None:
    """不受支持的版本 → 回本服务的版本（MCP 规定），且**记 stderr**（不静默）。"""
    lines: list[str] = []
    table = make_method_table({"a": _tool({})}, log=lines.append)

    result = table["initialize"]({"protocolVersion": "1999-01-01"})

    assert result["protocolVersion"] == MCP_VERSION
    assert any("1999-01-01" in line for line in lines)


def test_initialize_does_not_police_the_clients_params() -> None:
    """握手方法不挑字段：MCP 各版本发的字段不同，白名单只会把新版客户端拒在门外。"""
    result = _call("initialize", {
        "protocolVersion": MCP_VERSION,
        "capabilities": {"roots": {"listChanged": True}},
        "clientInfo": {"name": "claude-ai", "version": "0.1.0"},
    })

    assert result["serverInfo"]["name"] == SERVER_NAME


def test_the_declared_protocol_version_matches_the_contract() -> None:
    """契约 §五.7 写明了版本号；代码改它、契约没改（或反过来）都要红。"""
    section = _contract_section("### 五.7")
    dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", section))

    assert MCP_VERSION in dates, f"契约 §五.7 里没写 {MCP_VERSION}"
    assert SUPPORTED_PROTOCOL_VERSIONS == (MCP_VERSION,)


# ---------------------------------------------------------------------------
# 三、tools/list
# ---------------------------------------------------------------------------

def test_tools_list_reports_exactly_the_tools_in_the_table() -> None:
    """清单取自工具表本身 —— 两边**同一个来源**，所以不可能只多一个或少一个。"""
    toolset = {"zeta": _tool({}), "alpha": _tool({}), "mid": _tool({})}

    tools = _call("tools/list", {}, toolset)["tools"]

    assert [item["name"] for item in tools] == sorted(toolset) == ["alpha", "mid", "zeta"]


def test_every_tool_carries_a_description_and_an_object_schema() -> None:
    tools = _call("tools/list", {}, {name: _tool({}) for name in TOOLS})["tools"]

    assert len(tools) == len(TOOLS)
    for item in tools:
        assert set(item) == {"name", "description", "inputSchema"}
        assert item["description"] == TOOL_SUMMARIES[item["name"]]
        assert item["inputSchema"]["type"] == "object"


def test_the_listed_names_are_the_frozen_eleven() -> None:
    """与 `get_capabilities` 一样，11 个名字有契约级的绊线。"""
    tools = _call("tools/list", {}, {name: _tool({}) for name in TOOLS})["tools"]
    assert sorted(item["name"] for item in tools) == sorted(TOOLS)


def test_tools_list_hands_out_fresh_copies() -> None:
    """调用方改了拿到的条目，不能影响下一次（否则一次误改会污染整个进程）。"""
    table = _table({"a": _tool({})})

    table["tools/list"]({})["tools"][0]["description"] = "tampered"

    assert table["tools/list"]({})["tools"][0]["description"] == TOOL_SUMMARIES.get("a", "")


def test_tools_list_accepts_a_cursor_and_rejects_anything_else() -> None:
    """P1 不分页（从不返回 `nextCursor`），但客户端带 `cursor` 上来不该是错误。"""
    assert not isinstance(_call("tools/list", {"cursor": "abc"}), JsonRpcError)

    outcome = _call("tools/list", {"curzor": "abc"})
    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == INVALID_PARAMS
    assert outcome.data is not None
    assert outcome.data["unexpected"] == ["curzor"]


# ---------------------------------------------------------------------------
# 四、tools/call —— 成功路径
# ---------------------------------------------------------------------------

def test_a_successful_call_is_wrapped_in_mcp_content() -> None:
    envelope = {"ok": True, "data": {"execution_id": "exec-1"}}
    outcome = _call("tools/call", {"name": "a", "arguments": {}}, {"a": _tool(envelope)})

    assert outcome["isError"] is False
    block = outcome["content"][0]
    assert block["type"] == "text"
    assert json.loads(block["text"]) == envelope


def test_the_content_text_is_deterministic() -> None:
    """键序固定（`sort_keys`）：同一份结果两次调用必须字节相同，否则测试只能退化。"""
    envelope = {"ok": True, "data": {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}}
    toolset = {"a": _tool(envelope)}

    first = _call("tools/call", {"name": "a"}, toolset)["content"][0]["text"]
    second = _call("tools/call", {"name": "a"}, toolset)["content"][0]["text"]

    assert first == second
    assert '"a":2,"b":1' in first


def test_arguments_default_to_an_empty_object() -> None:
    """`arguments` 可缺省（MCP 里它是可选字段），工具收到的必须是 `{}` 而不是 `None`。"""
    seen: list[dict[str, Any]] = []
    _call("tools/call", {"name": "a"}, {"a": _tool({"ok": True}, seen)})

    assert seen == [{}]


def test_meta_is_accepted_and_ignored() -> None:
    """MCP 的 `_meta` 是保留键，收到它既不是错误、也不该传给工具。"""
    seen: list[dict[str, Any]] = []
    outcome = _call("tools/call", {"name": "a", "_meta": {"trace": "x"}}, {"a": _tool({"ok": True}, seen)})

    assert not isinstance(outcome, JsonRpcError)
    assert seen == [{}]


# ---------------------------------------------------------------------------
# 五、tools/call —— 失败路径（协议错误 vs 工具执行失败）
# ---------------------------------------------------------------------------

def test_a_tool_saying_not_ok_is_a_result_not_a_transport_error() -> None:
    """工具执行失败走 `result` 里的 `isError`，**不走** `error` 字段（契约 §五.1）。"""
    envelope = {"ok": False, "error": {"code": "EXECUTION_NOT_FOUND", "message": "no such execution"}}
    outcome = _call("tools/call", {"name": "a"}, {"a": _tool(envelope)})

    assert not isinstance(outcome, JsonRpcError)
    assert outcome["isError"] is True
    assert json.loads(outcome["content"][0]["text"]) == envelope


def test_a_truthy_but_wrong_ok_field_is_not_a_success() -> None:
    """`1 == True` 在 Python 里成立 —— 判据必须是 `is True`，否则写错的信封会被放过。"""
    outcome = _call("tools/call", {"name": "a"}, {"a": _tool({"ok": 1, "data": {}})})

    assert outcome["isError"] is True


_MISSING = object()
"""哨兵：把"没有 `ok` 这个键"和"`ok` 是 `None`"分开 —— 两者都要算失败，但成因不同。"""


@pytest.mark.parametrize(("ok_value", "expected_is_error"), [
    (True, False),          # 唯一的成功形态
    (1, True),              # 整数 1：`1 == True` 成立，但 `is True` 不成立
    (0, True),
    ("true", True),         # 字符串 —— 最容易被"顺手改成 not result.get('ok')"放过
    ("", True),
    (None, True),
    ("missing", True),      # 连键都没有
])
def test_is_error_accepts_exactly_boolean_true(ok_value: Any, expected_is_error: bool) -> None:
    """`isError` 的判据是 `{"ok": True}`（**身份**断言，不是真值断言）。

    为什么值得专门参数化：这几个值里 `"true"` 和 `"missing"` 是将来被"顺手改坏"的入口 ——
    有人把 `result.get("ok") is not True` 改成 `not result.get("ok")`，
    `1` 那格会红（现有那条测试能抓到），但**真正危险的是语义反转**：
    写错信封的产物从"失败"变成"成功"，而失败是没有告警的。
    """
    envelope: dict[str, Any] = {"data": {}}
    if ok_value is not _MISSING:
        envelope["ok"] = ok_value

    outcome = _call("tools/call", {"name": "a"}, {"a": _tool(envelope)})

    assert outcome["isError"] is expected_is_error, envelope


def test_the_is_error_rule_is_a_identity_test_not_a_truthiness_test() -> None:
    """把上一条的判据本身写出来：`is True` 与真值判断在 `1` 上分道扬镳。

    这条守的是**判据写法**，不是某个取值 —— 上面那组参数化是它的实例。
    """
    for value in (1, "true", [1]):
        assert (value is True) is False
        assert bool(value) is True          # 真值判断会把它当成功
    assert (True is True) is True


def test_an_unknown_tool_is_invalid_params_not_method_not_found() -> None:
    """`tools/call` 这个方法**在**，错的是参数 —— 所以是 `-32602`。"""
    outcome = _call("tools/call", {"name": "nope"}, {"a": _tool({})})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == INVALID_PARAMS
    assert outcome.data is not None
    assert outcome.data["available"] == ["a"]


@pytest.mark.parametrize("params", [
    {},                                   # 没有 name
    {"name": ""},                          # 空名字
    {"name": 7},                           # 不是字符串
])
def test_a_missing_or_wrong_name_is_invalid_params(params: dict[str, Any]) -> None:
    outcome = _call("tools/call", params, {"a": _tool({})})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == INVALID_PARAMS


def test_arguments_must_be_an_object() -> None:
    outcome = _call("tools/call", {"name": "a", "arguments": [1, 2]}, {"a": _tool({})})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == INVALID_PARAMS
    assert "list" in outcome.message


def test_unexpected_params_are_rejected_not_ignored() -> None:
    """`argumetns` 这种拼错必须报错 —— 静默忽略等于让客户端以为参数送到了。"""
    outcome = _call("tools/call", {"name": "a", "argumetns": {}}, {"a": _tool({})})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == INVALID_PARAMS
    assert outcome.data is not None
    assert outcome.data["unexpected"] == ["argumetns"]


# ---------------------------------------------------------------------------
# 五之二、`-32602` 的 `data.reason`：一个码扛四种情形，客户端要能分支
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("params", "reason", "extra"), [
    ({}, REASON_INVALID_NAME, None),
    ({"name": ""}, REASON_INVALID_NAME, None),
    ({"name": 7}, REASON_INVALID_NAME, None),
    ({"name": "nope"}, REASON_UNKNOWN_TOOL, {"name": "nope", "available": ["a"]}),
    ({"name": "a", "arguments": [1]}, REASON_INVALID_ARGUMENTS, None),
    ({"name": "a", "argumetns": {}}, REASON_UNEXPECTED_PARAM, {"unexpected": ["argumetns"]}),
])
def test_every_invalid_params_carries_a_stable_reason(
    params: dict[str, Any], reason: str, extra: dict[str, Any] | None
) -> None:
    """四种情形各出一个 `reason` —— 客户端据此分支，而不是去匹配 message 字符串。

    `message` 是给人看的，改一个字就变；`reason` 是给程序看的稳定契约。
    """
    outcome = _call("tools/call", params, {"a": _tool({})})

    assert isinstance(outcome, JsonRpcError)
    assert outcome.code == INVALID_PARAMS
    assert outcome.data is not None
    assert outcome.data["reason"] == reason
    if extra is not None:
        for key, value in extra.items():
            assert outcome.data[key] == value


def test_the_reason_is_exactly_the_enum_and_nothing_else() -> None:
    """反证：上面那组参数化**穷尽**了 `-32602` 的出口。

    实现里所有 `-32602` 都从 `_invalid_params` 出，它只收枚举内的值；
    这条再从"实际产生的 reason 集合"回看一遍 —— 两边相等才说明没有第五种情形。
    """
    produced = set()
    for params in ({}, {"name": ""}, {"name": "nope"}, {"name": "a", "arguments": []},
                   {"name": "a", "x": 1}):
        outcome = _call("tools/call", params, {"a": _tool({})})
        assert isinstance(outcome, JsonRpcError)
        assert outcome.data is not None
        produced.add(outcome.data["reason"])

    assert produced == set(INVALID_PARAMS_REASONS)


def test_the_reason_enum_matches_the_contract_table() -> None:
    """读契约 §五.7 的 `reason` 表比对取值 —— 少一个多一个都红（机器比对，不是抄字面量）。"""
    reasons_in_table = _contract_reasons()

    assert reasons_in_table == set(INVALID_PARAMS_REASONS), (
        f"契约 §五.7 与代码不一致：契约={sorted(reasons_in_table)} "
        f"代码={sorted(INVALID_PARAMS_REASONS)}"
    )


def test_the_reason_enum_check_would_catch_drift() -> None:
    """反证：上面那条不是恒真 —— 契约里确实扫到了那些值，且多一个就会不等。"""
    reasons_in_table = _contract_reasons()

    assert "unknown_tool" in reasons_in_table
    assert reasons_in_table != set(INVALID_PARAMS_REASONS) | {"no_such_reason"}


def test_every_reason_matches_the_message_it_rides_with() -> None:
    """`reason` 与 `message` 说的是同一件事 —— 顺手一致性检查。

    没有这条的话，把一处 `reason` 复制粘贴错（比如 `invalid_name` 配
    "arguments must be an object"）不会有任何测试喊出来：两边各自都"合法"。
    """
    cases = {
        REASON_UNKNOWN_TOOL: ("nope",),
        REASON_INVALID_ARGUMENTS: ("object",),
        REASON_INVALID_NAME: ("name",),
        REASON_UNEXPECTED_PARAM: ("unexpected",),
    }
    assert set(cases) == set(INVALID_PARAMS_REASONS)

    for reason, needles in cases.items():
        params = {
            REASON_UNKNOWN_TOOL: {"name": "nope"},
            REASON_INVALID_ARGUMENTS: {"name": "a", "arguments": []},
            REASON_INVALID_NAME: {},
            REASON_UNEXPECTED_PARAM: {"name": "a", "x": 1},
        }[reason]
        outcome = _call("tools/call", params, {"a": _tool({})})
        assert isinstance(outcome, JsonRpcError)
        assert outcome.data is not None
        assert outcome.data["reason"] == reason
        assert any(needle in outcome.message for needle in needles), outcome.message


def test_a_bad_arguments_field_is_not_invalid_params(tmp_path: Path) -> None:
    """★ **`arguments` 里的字段问题不走 `-32602`** —— 这条把两层分开钉死。

    客户端要处理两类"参数不行"，它们**不是同一层**：

    | 情形 | 走哪 | 客户端反应 |
    |---|---|---|
    | 调用形状写错（`arguments` 不是对象） | `-32602` + `reason` | 改代码 |
    | 工具入参不合法（`arguments` 里缺字段） | `result` 的 `isError: true` + `INPUT_SCHEMA_INVALID` | 补字段 / 提示用户 |

    混起来会把"服务器拒绝执行"说成"你把 JSON-RPC 调用写错了"。

    用**真工具**（不是假表）验第二种：假表只能证明"我没拦"，
    证明不了"真工具会拦、且拦在业务层"。
    """
    from mcp_server import stdio
    from mcp_server.tools import build_toolset

    ctx = stdio.build_server_context(
        db_path=tmp_path / "protocol.db", identity="agent", log=lambda _m: None
    )
    try:
        table = make_method_table(build_toolset(ctx), log=lambda _m: None)
        # 工具名对，`arguments` 是个**对象**，只是里面缺了必填字段
        outcome = table["tools/call"]({"name": "execute_workflow", "arguments": {}})
    finally:
        stdio.close_quietly(ctx, log=lambda _m: None)

    assert not isinstance(outcome, JsonRpcError), "这是业务错误，不该走 -32602"
    assert outcome["isError"] is True
    body = json.loads(outcome["content"][0]["text"])
    assert body["ok"] is False
    assert body["error"]["code"] == "INPUT_SCHEMA_INVALID"


def test_a_programming_error_inside_a_tool_still_propagates() -> None:
    """方法层**不 catch** 工具抛的异常：冒泡给 `serve()` 翻成 `-32603`。

    在这里 catch 会把真缺陷伪装成业务失败（"工具出错了" ≠ "工具有 bug"）。
    """
    def exploding(_params: dict[str, Any]) -> Any:
        raise KeyError("this is a bug, not a business failure")

    with pytest.raises(KeyError):
        _call("tools/call", {"name": "a"}, {"a": exploding})


def test_a_tool_returning_a_non_dict_is_a_programming_error() -> None:
    """工具函数按契约必须回 dict；回别的形状是编程错误 → 让它抛（`serve` 会兜成 `-32603`）。"""
    with pytest.raises(TypeError):
        _call("tools/call", {"name": "a"}, {"a": _tool("not a dict")})


# ---------------------------------------------------------------------------
# 六、通知：登记了，但**不响应**
# ---------------------------------------------------------------------------

def test_every_notification_is_registered() -> None:
    table = _table()
    for name in NOTIFICATION_METHODS:
        assert name in table
        assert table[name]({}) == {}


def test_a_notification_gets_no_response_and_does_not_stop_the_loop() -> None:
    """通知（无 `id`）执行但**不响应**；同一流里后面那条请求照常有响应。

    断言"恰好一条"而不是"没有响应" —— 后者在整条流都没响应时也成立，是假通过。
    """
    stream = _notification("notifications/initialized") + _request("tools/list")
    responses = _run(stream)

    assert len(responses) == 1
    assert responses[0]["id"] == 1
    assert "tools" in responses[0]["result"]


def test_the_three_methods_answer_their_own_ids() -> None:
    stream = (
        _request("initialize", request_id=1)
        + _request("tools/list", request_id=2)
        + _request("tools/call", request_id=3, params={"name": "a"})
    )
    responses = _run(stream, {"a": _tool({"ok": True, "data": {}})})

    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert _payload(responses[2]) == {"ok": True, "data": {}}


# ---------------------------------------------------------------------------
# 七、清单的单一来源：描述表的同步测试（读契约 + 字面量绊线）
# ---------------------------------------------------------------------------

CONTRACT_TOOL_NAMES = (
    "get_capabilities", "list_skills", "create_skill", "list_workflows", "match_workflow",
    "create_workflow", "execute_workflow", "retry_execution", "get_execution_status",
    "abort_execution", "resume_execution",
)
"""字面量绊线：契约 §二 的 11 个工具。改了工具集这条要一起动（故意的摩擦）。"""


def test_the_summaries_cover_exactly_the_frozen_eleven_tools() -> None:
    """`TOOL_SUMMARIES` 与 `TOOLS` 的键集必须**完全一致** —— 少一个就有工具没描述，
    多一个就有描述指向不存在的工具，两种都是"看起来没事"的漂移。
    """
    assert set(TOOL_SUMMARIES) == set(TOOLS)
    assert set(TOOLS) == set(CONTRACT_TOOL_NAMES)
    assert len(TOOL_SUMMARIES) == 11


def test_the_summary_sync_check_would_catch_drift() -> None:
    """反证：上面那条判据对**故意漂移**的表必须变红（否则它只是恒真的空转）。"""
    missing = {name: text for name, text in TOOL_SUMMARIES.items() if name != "resume_execution"}
    extra = {**TOOL_SUMMARIES, "no_such_tool": "把名字写错的一行"}

    assert set(missing) != set(TOOLS)
    assert set(extra) != set(TOOLS)


def test_every_summary_is_a_non_empty_single_line() -> None:
    """描述会原样进 `tools/list` 给客户端的模型看 —— 多行 / 空串都会让清单变脏。"""
    for name, text in TOOL_SUMMARIES.items():
        assert text == text.strip(), name
        assert text, name
        assert "\n" not in text, name


# ---------------------------------------------------------------------------
# 八、真实装配：`stdio.run` 走的是方法层
# ---------------------------------------------------------------------------

def test_the_real_assembly_speaks_the_three_methods(tmp_path: Path) -> None:
    """端到端锁住"装配换了那张表"：真实 `ServerContext` 下，
    `tools/call` 通、`get_capabilities`（旧形状）不通。
    """
    from mcp_server import stdio

    ctx = stdio.build_server_context(
        db_path=tmp_path / "protocol.db", identity="agent", log=lambda _m: None
    )
    writer = io.StringIO()
    try:
        reader = io.StringIO(
            _request("initialize", request_id=1)
            + _request("tools/list", request_id=2)
            + _request("tools/call", request_id=3, params={"name": "get_capabilities", "arguments": {}})
            + _request("get_capabilities", request_id=4)
        )
        code = stdio.run(ctx, reader=reader, writer=writer, log=lambda _m: None)
    finally:
        stdio.close_quietly(ctx, log=lambda _m: None)

    assert code == 0
    responses = [json.loads(line) for line in writer.getvalue().split("\n") if line]
    assert [r["id"] for r in responses] == [1, 2, 3, 4]

    assert [item["name"] for item in responses[1]["result"]["tools"]] == sorted(TOOLS)
    assert _payload(responses[2])["data"]["identity"] == "agent"

    assert responses[3]["error"]["code"] == METHOD_NOT_FOUND, "工具名不该还能当方法名"
