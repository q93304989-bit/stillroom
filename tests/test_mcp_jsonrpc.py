"""MCP 传输层：分帧、`id` 语义、错误码分工、坏消息隔离、串行。

十一条边界逐条都被两件事锁住：**正向断言**（该发生的发生了）+
**反向断言**（不该发生的没发生）。只做前者会漏掉一半 —— 例如"通知不响应"
只断言"没收到响应"是**假通过**，因为整条流一个响应都没有也满足它。

| # | 边界 | 落点 |
|---|---|---|
| 1 | 换行分隔 JSON | 有界读 + 超长整行丢弃 |
| 2 | `id` 原样回显 | `has_id` 用 `in` 判定；类型断言 |
| 3 | 错误码分工 | 业务错误走 `result`，只有 `JsonRpcError` 走 `error` |
| 4 | `jsonrpc:"2.0"` 必检 | 信封坏了也回（`id` 可用就用它） |
| 5 | stdout 只走协议 | stdout **精确等于**协议帧；诊断全在 stderr |
| 6 | EOF / 坏消息 / 串行 | 单条失败不终止主循环 |
| 7 | 批量请求显式拒绝 | 顶层数组 → **一条** `-32600`，且不流到 `dispatch` |
| 8 | `NaN`/`Infinity` 入口拒 | `parse_constant` 钩子 → `-32700` |
| 9 | `dispatch` 抛异常 → `-32603` | 堆栈只进 stderr，不回显 |
| 10 | 空行只认真空行 | `"   \\n"` 是畸形数据 → `-32700` |
| 11 | `log=None` 兜底 stderr | 不静默丢弃；注入则覆盖 |

**源码扫描拆成两条规则，判据是"标准流"，不是"碰 `sys`"** ——
后者是个过宽的代理指标，会把边界 #11 要求的 stderr 兜底一起禁掉。

| 规则 | 内容 | 适用范围 |
|---|---|---|
| A | 不**写** stdout（含裸 `print()`） | 包内**所有**模块 |
| B | 不**引用** `sys.stdin` / `sys.stdout` | 除入口模块 `__main__.py` 外 |

入口模块必须引用这两个流（把它们交给 `serve(reader=…, writer=…)`），
但它同样不得直接往 stdout 写 —— 它记日志走 `stderr_log`。
"""

from __future__ import annotations

import inspect
import io
import json
import sys
from pathlib import Path

import pytest

from mcp_server import ENTRY_MODULE, jsonrpc as jsonrpc_module
from mcp_server.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcError,
    serve,
)

from source_scan import package_modules, process_stream_references, stdout_write_offenders

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------



def _echo(method: str, params: dict) -> dict:
    """最简 dispatch：把 method/params 回显，用来验证信封与透传。"""
    return {"echo_method": method, "echo_params": params}


def _run(stream: str, dispatch=_echo, **kwargs) -> tuple[int, list[dict], list[str]]:
    """跑一遍主循环。返回 `(退出码, 响应列表, 日志列表)`。

    顺便断言"每帧都以换行结尾" —— 分帧的契约之一，省得每个用例重复写。
    """
    reader = io.StringIO(stream)
    writer = io.StringIO()
    logs: list[str] = []
    code = serve(reader, writer, dispatch, log=logs.append, **kwargs)

    raw = writer.getvalue()
    if raw:
        assert raw.endswith("\n"), "每一帧都必须以换行结尾"
        lines = raw.split("\n")[:-1]
        assert all(line and "\n" not in line for line in lines), "一帧就是一行"
    else:
        lines = []
    return code, [json.loads(line) for line in lines], logs


def _msg(**fields) -> str:
    """拼一条请求（不自动补字段，方便测缺失字段）。"""
    return json.dumps(fields, ensure_ascii=False) + "\n"


def _request(method: str = "ping", *, request_id=1, params=None, **extra) -> str:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    body.update(extra)
    return json.dumps(body, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# 一、分帧：换行分隔
# ---------------------------------------------------------------------------

def test_several_messages_on_separate_lines_all_get_answers() -> None:
    code, responses, _ = _run(_request("a", request_id=1) + _request("b", request_id=2))

    assert code == 0
    assert [r["id"] for r in responses] == [1, 2]
    assert [r["result"]["echo_method"] for r in responses] == ["a", "b"]


def test_true_blank_lines_are_skipped() -> None:
    """客户端偶发多写一个换行不该炸、也不该产生响应。

    注意这里只有**真空行**（`"\\n"`）。纯空白行是另一回事，
    见 `test_a_whitespace_only_line_is_not_a_blank_line`。
    """
    stream = "\n" + _request("a", request_id=1) + "\n\n" + _request("b", request_id=2) + "\n"

    code, responses, _ = _run(stream)
    assert code == 0
    assert [r["id"] for r in responses] == [1, 2]


def test_a_whitespace_only_line_is_not_a_blank_line() -> None:
    """**边界 #4 的落点：只认真空行。**

    `"   \\n"` 不是空行 —— JSON 规范里空白不是合法值，
    它意味着客户端发了畸形数据。跳过它等于把问题藏起来
    （服务端一声不吭地空转，没有任何线索），所以按解析失败处理。

    强写法：夹一条真空行、一条纯空白行、两条好请求，
    断言纯空白行**产生了** `-32700`，而真空行**没有**。
    """
    stream = (
        "\n"                                      # 真空行：跳过
        + _request("a", request_id=1)
        + "   \n"                                  # 纯空白行：拒
        + "\t \t\n"                                # 制表符 + 空格：也拒
        + _request("b", request_id=2)
    )

    code, responses, logs = _run(stream)

    assert code == 0
    assert [r.get("id") for r in responses] == [1, None, None, 2]
    assert [r["error"]["code"] for r in responses if "error" in r] == [PARSE_ERROR] * 2
    assert len(logs) == 2, "纯空白行必须留下痕迹，不能静默跳过"


def test_a_blank_line_that_is_not_newline_terminated_is_also_rejected() -> None:
    """末尾一行全是空格且**没有**换行：同样是畸形数据，不是空行。"""
    code, responses, _ = _run(_request("a", request_id=1) + "   ")

    assert code == 0
    assert [r.get("id") for r in responses] == [1, None]
    assert responses[1]["error"]["code"] == PARSE_ERROR


def test_an_escaped_newline_inside_a_message_is_not_a_frame_break() -> None:
    """消息内部的换行必须是转义的 —— 这正是"一行 = 一条消息"能成立的前提。"""
    payload = "line1\nline2"
    code, responses, _ = _run(_request("echo", request_id=7, params={"text": payload}))

    assert code == 0
    assert len(responses) == 1
    assert responses[0]["result"]["echo_params"]["text"] == payload


def test_the_last_line_without_a_trailing_newline_is_still_processed() -> None:
    """客户端最后一条没补换行，仍是完整的一条消息，不该被吞掉。"""
    code, responses, _ = _run(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}))

    assert code == 0
    assert [r["id"] for r in responses] == [9]


def test_an_oversized_line_is_dropped_whole_and_the_next_line_survives() -> None:
    """超长行：整行丢弃 + `-32700`，**残余不被当成下一条消息**，后续照常。

    这条同时验证"有界读 + 排空到换行"：用 `readline()` 无界读会把整行吃进内存，
    而只读到一半就放弃会把残余当下一条消息，产生幽灵响应。
    """
    oversized = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"pad": "y" * 500}})
    assert len(oversized) > 50

    code, responses, logs = _run(
        oversized + "\n" + _request("after", request_id=2),
        max_message_chars=50,
    )

    assert code == 0
    assert len(responses) == 2, "多出来的响应说明残余被当成了消息"
    assert responses[0]["error"]["code"] == PARSE_ERROR
    assert responses[0]["id"] is None
    assert responses[1]["id"] == 2 and responses[1]["result"]["echo_method"] == "after"
    assert any("exceeds" in entry for entry in logs)


def test_a_line_exactly_at_the_limit_is_accepted() -> None:
    """边界不该差一：`limit` 个字符（不含换行）是合法的。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x"}, ensure_ascii=False)

    code, responses, _ = _run(body + "\n", max_message_chars=len(body))
    assert code == 0
    assert responses[0]["id"] == 1

    # 少一个字符就必须被拒 —— 否则"上限"其实是 len+1
    code, responses, _ = _run(body + "\n", max_message_chars=len(body) - 1)
    assert code == 0
    assert responses[0]["error"]["code"] == PARSE_ERROR


def test_oversize_guard_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        serve(io.StringIO(""), io.StringIO(), _echo, max_message_chars=0)


# ---------------------------------------------------------------------------
# 二、id 语义：原样回显，类型不变
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given", [1, 0, -5, 1.5, 2.0, "1", "req-1", "", None])
def test_id_is_echoed_verbatim_with_its_exact_type(given: object) -> None:
    """`1` 与 `"1"` 是不同的 ID。断言**类型**而不只是相等。

    `2.0` 这个参数就是为这条断言准备的：`int(2.0) == 2.0` 为真，
    所以只写 `resp["id"] == req["id"]` 时，"顺手 `int()` 规范化一下"
    这种改法**会静默通过**。类型断言才拦得住。
    """
    code, responses, _ = _run(_request("ping", request_id=given))

    assert code == 0
    assert len(responses) == 1
    assert responses[0]["id"] == given
    assert isinstance(responses[0]["id"], type(given))
    assert type(responses[0]["id"]) is type(given)


def test_numeric_and_string_ids_stay_distinct_in_the_same_stream() -> None:
    code, responses, _ = _run(
        _request("a", request_id=1) + _request("b", request_id="1")
    )

    assert code == 0
    assert type(responses[0]["id"]) is int and responses[0]["id"] == 1
    assert type(responses[1]["id"]) is str and responses[1]["id"] == "1"


def test_a_null_id_is_a_request_not_a_notification() -> None:
    """`"id": null` 是"有 ID 但值为空"，**必须响应**，不能靠真值判断当成通知。"""
    code, responses, _ = _run(_msg(jsonrpc="2.0", id=None, method="ping"))

    assert code == 0
    assert len(responses) == 1
    assert "id" in responses[0] and responses[0]["id"] is None
    assert responses[0]["result"]["echo_method"] == "ping"


@pytest.mark.parametrize("bad_id", [True, False, {}, [], [1]])
def test_a_structurally_invalid_id_is_an_invalid_request(bad_id: object) -> None:
    """id 只能是字符串 / 数字 / null。`true` 不是数字而是布尔，也在拒绝之列。"""
    code, responses, _ = _run(_msg(jsonrpc="2.0", id=bad_id, method="ping"))

    assert code == 0
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert responses[0]["id"] is None


# ---------------------------------------------------------------------------
# 三、通知：执行但不响应
# ---------------------------------------------------------------------------

def test_a_notification_gets_no_response_and_does_not_break_the_next_request() -> None:
    """强写法：先发通知、再发请求，断言**只**收到一条响应，且是请求那条。

    只断言"通知没收到响应"是假通过 —— 整条流一个响应都没有也满足它。
    """
    seen: list[str] = []

    def dispatch(method: str, params: dict) -> dict:
        seen.append(method)
        return {"echo_method": method}

    code, responses, _ = _run(
        _request("notify_me") .replace('"id": 1, ', "") + _request("real", request_id=2),
        dispatch,
    )

    assert code == 0
    assert seen == ["notify_me", "real"], "通知必须被执行，不是被忽略"
    assert len(responses) == 1
    assert responses[0]["id"] == 2 and responses[0]["result"]["echo_method"] == "real"


def test_a_failing_notification_is_logged_but_never_answered() -> None:
    """通知出错也不能回响应（规范如此），但必须留下 stderr 痕迹。"""
    def dispatch(method: str, params: dict) -> dict:
        raise RuntimeError("boom")

    code, responses, logs = _run(
        json.dumps({"jsonrpc": "2.0", "method": "note"}) + "\n" + _request("after", request_id=1),
        dispatch,
    )

    assert code == 0
    assert len(responses) == 1 and responses[0]["id"] == 1
    assert any("note" in entry and "boom" in entry for entry in logs)


def test_a_notification_that_resolves_to_method_not_found_is_still_not_answered() -> None:
    def dispatch(method: str, params: dict) -> JsonRpcError:
        return JsonRpcError(METHOD_NOT_FOUND, f"unknown method: {method}")

    code, responses, logs = _run(
        json.dumps({"jsonrpc": "2.0", "method": "nope"}) + "\n", dispatch
    )

    assert code == 0
    assert responses == []
    assert any(str(METHOD_NOT_FOUND) in entry for entry in logs)


# ---------------------------------------------------------------------------
# 四、错误码分工：传输层走 error，业务层走 result
# ---------------------------------------------------------------------------

def test_a_business_error_travels_inside_result_and_never_uses_the_error_field() -> None:
    """**这是错误码分工的核心断言。**

    业务失败（`SCHEMA_INVALID` 之类）必须是 `result` 里的结构化对象。
    一旦实现顺手改成 `error` 字段，客户端就分不清"方法不存在"与"参数写错了"。
    """
    def dispatch(method: str, params: dict) -> dict:
        return {"ok": False, "error_code": "SCHEMA_INVALID", "message": "input is not valid"}

    code, responses, _ = _run(_request("execute_workflow", request_id=5), dispatch)

    assert code == 0
    assert "error" not in responses[0], "业务错误不得占用 error 字段"
    assert responses[0]["result"] == {
        "ok": False,
        "error_code": "SCHEMA_INVALID",
        "message": "input is not valid",
    }


def test_a_successful_business_payload_is_passed_through_untouched() -> None:
    payload = {"execution_id": "exec_1", "status": "PENDING", "nested": {"a": [1, 2]}}

    code, responses, _ = _run(_request("execute_workflow"), lambda m, p: payload)

    assert responses[0]["result"] == payload
    assert set(responses[0]) == {"jsonrpc", "id", "result"}


def test_a_transport_error_uses_the_error_field_and_has_no_result() -> None:
    def dispatch(method: str, params: dict) -> JsonRpcError:
        return JsonRpcError(METHOD_NOT_FOUND, f"unknown method: {method}")

    code, responses, _ = _run(_request("no_such_tool", request_id=3), dispatch)

    assert code == 0
    assert "result" not in responses[0]
    assert responses[0]["error"]["code"] == METHOD_NOT_FOUND
    assert responses[0]["error"]["message"] == "unknown method: no_such_tool"
    assert responses[0]["id"] == 3


def test_error_data_is_omitted_when_absent() -> None:
    def dispatch(method: str, params: dict) -> JsonRpcError:
        return JsonRpcError(METHOD_NOT_FOUND, "nope")

    _, responses, _ = _run(_request("x"), dispatch)
    assert set(responses[0]["error"]) == {"code", "message"}

    def with_data(method: str, params: dict) -> JsonRpcError:
        return JsonRpcError(METHOD_NOT_FOUND, "nope", data={"method": method})

    _, responses2, _ = _run(_request("y"), with_data)
    assert responses2[0]["error"]["data"] == {"method": "y"}


# ---------------------------------------------------------------------------
# 五、信封校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        '{"id":1,"method":"ping"}',                     # 缺 jsonrpc
        '{"jsonrpc":"1.0","id":1,"method":"ping"}',      # 版本不对
        '{"jsonrpc":2.0,"id":1,"method":"ping"}',        # 版本字段类型不对
        '{"jsonrpc":"2.0","id":1}',                      # 缺 method
        '{"jsonrpc":"2.0","id":1,"method":123}',          # method 非字符串
        '{"jsonrpc":"2.0","id":1,"method":""}',           # method 空串
    ],
)
def test_a_broken_envelope_is_never_treated_as_a_valid_request(raw: str) -> None:
    """信封坏了就是坏了，**不回退**成"当它没写 jsonrpc"或"当它是通知"。"""
    code, responses, _ = _run(raw + "\n")

    assert code == 0
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert "result" not in responses[0]
    assert responses[0]["id"] == 1, "id 可用时要用它回，客户端才能对上号"


@pytest.mark.parametrize(
    "raw",
    [
        '{"method":"ping"}',                    # 什么都没有
        '{"jsonrpc":"1.0","method":"ping"}',     # 版本不对且无 id
        '{"jsonrpc":"2.0"}',                     # 只有版本
    ],
)
def test_a_broken_envelope_without_an_id_still_gets_answered_with_null(raw: str) -> None:
    """**这条是"缺 id = 通知"的边界。**

    通知的定义是"**well-formed** 的请求但不带 id"。信封本身不合法时，
    就无从知道它本来是什么 —— 所以必须回一条 `id: null`，
    而不是借"缺 id"之名把它当成通知默默丢掉、也不是默默当成合法请求。
    """
    code, responses, _ = _run(raw + "\n")

    assert code == 0
    assert len(responses) == 1, "坏信封被当通知吞掉了"
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert responses[0]["id"] is None


def test_a_broken_envelope_with_a_good_id_echoes_that_id() -> None:
    """信封坏了但 id 可用时，用它的 id 回 —— 客户端才能把错误对上是哪条请求。"""
    code, responses, _ = _run('{"id":"req-42","method":"ping"}\n')

    assert code == 0
    assert responses[0]["id"] == "req-42"
    assert responses[0]["error"]["code"] == INVALID_REQUEST


@pytest.mark.parametrize("raw", ['"hello"', "42", "null", "true"])
def test_a_non_object_message_is_an_invalid_request(raw: str) -> None:
    """顶层不是对象、也不是数组 → 无效请求。"""
    code, responses, _ = _run(raw + "\n")

    assert code == 0
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert responses[0]["id"] is None


def test_a_batch_request_is_rejected_with_exactly_one_error() -> None:
    """**边界 #1 的落点：批量请求显式拒绝，回一条 `-32600`。**

    JSON-RPC 2.0 允许顶层是数组，MCP 不用它。三条理由决定了必须**显式拒绝**：

    1. 逐条处理会造出"部分成功、部分失败"的响应数组，
       `serve()` 的"一条入、一条出"不变式当场被破坏；
    2. 静默丢弃会让客户端等不到响应而**卡死**；
    3. `-32600` 是规范允许的拒绝方式。

    强写法：断言"**恰好一条**响应" —— 只断言"有 error"的话，
    一个逐条处理并返回响应数组的实现也能通过。
    """
    batch = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "a"},
        {"jsonrpc": "2.0", "id": 2, "method": "b"},
    ]) + "\n"

    code, responses, logs = _run(batch + _request("after", request_id=3))

    assert code == 0
    assert len(responses) == 2, "批量必须只回一条，不能逐条响应"
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert responses[0]["id"] is None
    assert "result" not in responses[0]
    assert responses[1]["id"] == 3, "批量被拒后主循环照常"
    assert any("batch" in entry for entry in logs)


def test_an_empty_batch_is_rejected_too() -> None:
    """空数组也是数组 —— 不回响应等于让客户端卡死，所以照样拒。"""
    code, responses, _ = _run("[]\n")

    assert code == 0
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == INVALID_REQUEST


def test_a_batch_never_reaches_dispatch() -> None:
    """拒绝要在**分派之前**发生：批量请求不该触发任何工具调用。

    只断言"返回了 `-32600`"是不够的 —— 一个"先逐条分派、再拼一条错误"的
    实现也满足它，而那时候副作用（真的建了 workflow、真的起了 execution）
    已经发生过了，撤不回来。
    """
    seen: list[str] = []

    def dispatch(method: str, params: dict) -> dict:
        seen.append(method)
        return {}

    code, responses, _ = _run(
        json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "create_workflow"}]) + "\n",
        dispatch,
    )

    assert code == 0
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert seen == [], "批量请求绝不能流到 dispatch"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_number_literal_is_a_parse_error(token: str) -> None:
    """**边界 #2 的落点：`NaN` / `Infinity` / `-Infinity` 在入口就拒。**

    Python 的 `json.loads` 默认**接受**这三个（Python 扩展，不是 JSON 规范）。
    不拦的话 `x = float("nan")` 一路传到 runtime，等落库时 `json.dumps` 才炸 ——
    那时栈里全是仓库层的帧，离错误源头已经很远。

    强写法：不发整个请求，而是断言 `dispatch` **根本没被调用**。
    只断言"返回了 `-32700`"拦不住"先解析成功、再在下游某处失败"的实现。
    """
    seen: list[dict] = []

    def dispatch(method: str, params: dict) -> dict:
        seen.append(params)
        return {}

    raw = '{"jsonrpc":"2.0","id":1,"method":"foo","params":{"x":%s}}\n' % token
    code, responses, logs = _run(raw, dispatch)

    assert code == 0
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == PARSE_ERROR
    assert responses[0]["id"] is None
    assert seen == [], "非有限数绝不能被解析出来送进 dispatch"
    assert any("non-finite" in entry for entry in logs)


def test_rejecting_non_finite_numbers_does_not_reject_ordinary_floats() -> None:
    """反方向：正常的浮点数、大数、负数照收 —— 别把整类数字一起禁掉。

    `parse_constant` 只在这三个 token 上被调用，普通浮点走的不是这条路径。
    没有这条断言，"实现改成自己写 tokenizer 正则"这种过度收紧照样全绿。
    """
    payload = {"pi": 3.14159, "big": 1e308, "neg": -0.0, "exp": 2.5e-10}
    code, responses, _ = _run(
        _request("echo", request_id=1, params=payload)
    )

    assert code == 0
    assert responses[0]["result"]["echo_params"] == payload


def test_params_must_be_an_object_when_present() -> None:
    """`params` 存在但不是对象 = 信封坏掉，走传输层错误。"""
    for bad in ('"text"', "123", "[1,2]", "null"):
        code, responses, _ = _run(
            '{"jsonrpc":"2.0","id":1,"method":"ping","params":%s}\n' % bad
        )
        assert responses[0]["error"]["code"] == INVALID_PARAMS, bad


def test_missing_params_becomes_an_empty_object() -> None:
    """params 缺失 = 无参调用。工具函数统一按 dict 处理，不必各自判 None。"""
    seen: list[dict] = []

    def dispatch(method: str, params: dict) -> dict:
        seen.append(params)
        return {}

    _run(_msg(jsonrpc="2.0", id=1, method="ping"), dispatch)
    assert seen == [{}]


# ---------------------------------------------------------------------------
# 六、坏消息隔离 + 顺序 + EOF
# ---------------------------------------------------------------------------

def test_a_parse_error_only_affects_that_one_message() -> None:
    """强写法：坏消息之后**再发一条好请求**，断言它也被正常处理。

    只测"返回了 error"则一个"解析失败就 return"的实现照样通过。
    """
    code, responses, logs = _run(
        "{not json\n" + _request("good", request_id=2) + "also-bad\n" + _request("later", request_id=3)
    )

    assert code == 0
    assert [r.get("id") for r in responses] == [None, 2, None, 3]
    assert [r["error"]["code"] for r in responses if "error" in r] == [PARSE_ERROR, PARSE_ERROR]
    assert len(logs) == 2


def test_a_dispatch_exception_becomes_internal_error_and_the_loop_survives() -> None:
    """dispatch 抛异常 → `-32603`，且**不回显异常文本**（可能带路径/SQL/内部结构）。"""
    calls: list[str] = []

    def dispatch(method: str, params: dict) -> dict:
        calls.append(method)
        if method == "boom":
            raise RuntimeError("secret internal path C:/db/protocol.db")
        return {"ok": True}

    code, responses, logs = _run(
        _request("boom", request_id=1) + _request("fine", request_id=2), dispatch
    )

    assert code == 0
    assert calls == ["boom", "fine"], "第一条失败不该终止主循环"
    assert responses[0]["error"]["code"] == INTERNAL_ERROR
    assert "secret" not in json.dumps(responses[0])
    assert responses[1]["id"] == 2 and responses[1]["result"] == {"ok": True}
    assert any("secret" in entry for entry in logs), "细节必须留在 stderr"


def test_a_dispatch_returning_a_non_dict_becomes_internal_error() -> None:
    """返回类型不对是编程错误：不能把 `None` 原样塞进 `result` 让客户端拿到 null。"""
    for bad in (None, "text", 42, [1], (1,)):
        code, responses, _ = _run(_request("x", request_id=1), lambda m, p, b=bad: b)
        assert responses[0]["error"]["code"] == INTERNAL_ERROR, bad


def test_eof_returns_zero() -> None:
    code, responses, logs = _run("")
    assert code == 0
    assert responses == []
    assert logs == []


def test_the_loop_is_strictly_serial_and_in_order() -> None:
    """读到一条处理一条：dispatch 的调用顺序必须与流顺序一致，不重排、不并发。"""
    order: list[int] = []

    def dispatch(method: str, params: dict) -> dict:
        order.append(params["n"])
        return {"n": params["n"]}

    stream = "".join(
        _request("tick", request_id=n, params={"n": n}) for n in range(1, 6)
    )
    code, responses, _ = _run(stream, dispatch)

    assert code == 0
    assert order == [1, 2, 3, 4, 5]
    assert [r["id"] for r in responses] == [1, 2, 3, 4, 5]


def test_an_unwritable_stdout_terminates_the_loop_with_a_non_zero_code() -> None:
    """客户端断了：继续跑没有意义，返回非 0。"""

    class DeadWriter(io.StringIO):
        def write(self, _s: str) -> int:
            raise BrokenPipeError("client is gone")

    reader = io.StringIO(_request("a") + _request("b"))
    code = serve(reader, DeadWriter(), _echo, log=lambda _m: None)

    assert code == 1


# ---------------------------------------------------------------------------
# 七、stdout 只走协议
# ---------------------------------------------------------------------------

def test_stdout_carries_only_protocol_frames(capsys, monkeypatch) -> None:
    """强写法：把真实 `sys.stdout` 接进去，用 `capsys` 断言 stdout **只有**协议帧。

    这是唯一能在 CI 里抓到"某处顺手 `print()`"的办法 ——
    用 `StringIO` 当 writer 时，误写的 `print()` 会跑到真 stdout 上而测试照样绿。
    """
    def dispatch(method: str, params: dict) -> dict:
        if method == "boom":
            raise RuntimeError("internal detail")
        return {"ok": True}

    stream = _request("ok", request_id=1) + _request("boom", request_id=2) + _request("ok", request_id=3)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stream))

    code = serve(sys.stdin, sys.stdout, dispatch, log=lambda m: print(m, file=sys.stderr))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == (
        '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
        '{"jsonrpc":"2.0","id":2,"error":{"code":-32603,"message":"Internal error"}}\n'
        '{"jsonrpc":"2.0","id":3,"result":{"ok":true}}\n'
    ), "stdout 里除了协议帧不该有任何字符"
    assert "internal detail" in captured.err, "细节只能在 stderr"


def test_the_default_log_sink_falls_back_to_stderr(capsys) -> None:
    """**边界 #5 的落点：不注入 `log` 时兜底 `sys.stderr`，不是静默丢弃。**

    静默丢弃的话，`stdio.py` 万一忘了传 `log`，出问题时**完全无声**。
    而 stderr 从来没被禁止 —— 铁律只针对 stdout（它是协议流）。

    强写法：断言 stdout **一字不出**（这是不能破的那条），
    同时断言 stderr **确实拿到了**那条诊断。
    """
    reader = io.StringIO("garbage\n" + _request("ok", request_id=1))
    writer = io.StringIO()
    serve(reader, writer, _echo)

    captured = capsys.readouterr()
    assert captured.out == "", "协议流之外 stdout 不许有任何字符"
    assert "parse error" in captured.err, "兜底日志必须真的落到 stderr"

    # 兜底生效的同时，协议帧照样只走 writer（这次流里有两条：坏消息 + 好请求）
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [f["id"] for f in frames] == [None, 1]


def test_an_injected_log_sink_replaces_the_stderr_fallback(capsys) -> None:
    """反方向：注入了 `log` 就走注入的，不再往 stderr 写 —— 不能两边都写。"""
    collected: list[str] = []
    reader = io.StringIO("garbage\n")
    serve(reader, io.StringIO(), _echo, log=collected.append)

    captured = capsys.readouterr()
    assert collected and "parse error" in collected[0]
    assert captured.err == "", "注入 log 后不该再往 stderr 写"


def _entry_path() -> Path:
    """`mcp_server.ENTRY_MODULE`（点号全名）→ 磁盘路径。"""
    return REPO_ROOT / (ENTRY_MODULE.replace(".", "/") + ".py")


def test_the_transport_module_never_writes_to_stdout_or_stdin() -> None:
    """架构不变量：`jsonrpc.py` **不写 stdout、也不引用 stdin/stdout**。

    注意判据是**标准流**，不是"碰 `sys`" —— `_default_log` 的兜底就是要写
    `sys.stderr`，而 stderr 从来没被禁止。锁"不写 stdout"才是那条真正要守的性质；
    锁"不碰 `sys`"是个过宽的代理指标，会把合法的 stderr 兜底一起禁掉。

    两条规则都查：`jsonrpc.py` 不是入口模块，所以规则 B 对它**没有**豁免。
    """
    source = (REPO_ROOT / "mcp_server" / "jsonrpc.py").read_text(encoding="utf-8")

    writes = stdout_write_offenders(source)
    references = process_stream_references(source)

    assert writes == [], f"jsonrpc.py 里出现了不该有的写入：{writes}"
    assert references == [], f"jsonrpc.py 里引用了标准流：{references}"


def test_no_module_in_the_package_prints_to_stdout() -> None:
    """整个 `mcp_server/` 都不许往 stdout 写（裸 `print()` 也算）。

    要点日志就往注入的 `log` 里写，或显式 `print(..., file=sys.stderr)`。

    规则 A 对**所有**模块成立 —— 入口模块也不例外，它记日志走的是
    `stderr_log`（显式 `file=sys.stderr`），不是裸 `print`。
    """
    offenders = {
        path.name: stdout_write_offenders(path.read_text(encoding="utf-8"))
        for path in package_modules(REPO_ROOT / "mcp_server")
        if stdout_write_offenders(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}


def test_there_is_exactly_one_entry_module() -> None:
    """**"唯一入口"是条硬约束**：实际引用标准流的模块集合必须**恰好**是 `{ENTRY_MODULE}`。

    这条把"豁免"从字符串匹配变成结构事实 —— 判据是**谁真的碰了标准流**，
    不是"谁的名字被写进了某个白名单"。于是：

    - 将来加 `cli.py` 之类的第二入口 → 它一旦摸 `sys.stdin` 就红，
      必须先改 `mcp_server.ENTRY_MODULE`（那是个需要想清楚的显式动作）；
    - 入口模块哪天不碰标准流了 → 也红（说明它没把流转交给 `serve()`）。

    两个方向都在一条断言里，比"遍历非入口模块断言为空"更强，
    也免了"豁免名单"在两处各写一遍。
    """
    offenders = {
        path.name: process_stream_references(path.read_text(encoding="utf-8"))
        for path in package_modules(REPO_ROOT / "mcp_server")
    }
    touching = {name for name, hits in offenders.items() if hits}

    assert touching == {_entry_path().name}, (
        f"引用标准流的模块应当恰好是 {_entry_path().name}，实际是 {touching}"
    )


def test_the_entry_module_constant_names_a_real_module() -> None:
    """字面量绊线 + 存在性：`ENTRY_MODULE` 改了要有人注意到，且它必须真的存在。

    没有"存在性"这一半的话，把常量写成 `"mcp_server.nope"` 会让上面那条
    断言**永远为假而红**（不是绿），但那时的报错信息是"集合不等"，
    看不出根因是"这个名字根本不是个模块"。
    """
    assert ENTRY_MODULE == "mcp_server.__main__"
    assert _entry_path().is_file(), f"{ENTRY_MODULE} 对应的文件不存在：{_entry_path()}"


# ---------------------------------------------------------------------------
# 八、纯函数式：不依赖全局
# ---------------------------------------------------------------------------

def test_the_stdout_write_scanner_actually_detects_violations(tmp_path: Path) -> None:
    """反证扫描器本身不是空转：故意写一份违规源码，必须全被抓到。

    没有这条，"`offenders == []`"可能只是因为扫描函数永远返回空 —— 那是假绿。
    """
    path = tmp_path / "bad.py"
    path.write_text(
        "import sys\n"
        "\n"
        "def f():\n"
        "    print('debug')\n"
        "    sys.stdout.write('x')\n"
        "    sys.stdin.read()\n"
        "    print('y', file=sys.stdout)\n",
        encoding="utf-8",
    )
    source = path.read_text(encoding="utf-8")

    writes = stdout_write_offenders(source)
    assert "sys.stdout.write" in writes
    assert "print(...) without file= (defaults to stdout)" in writes
    assert "print(file=...) where file is not stderr" in writes

    # 规则 B 抓的是"引用"，`sys.stdout.write` / `sys.stdin.read` 会给出两个引用。
    references = process_stream_references(source)
    assert "sys.stdout" in references
    assert "sys.stdin" in references


def test_the_scanners_leave_clean_code_alone(tmp_path: Path) -> None:
    """也不能误报：三种**合法**写法都不能被判违规。

    1. `sys` 只出现在 docstring / 注释里；
    2. 显式写 stderr 的 `print`（`_default_log` 就是这么写的）；
    3. 往注入的 `writer` / `log` 写。

    第 2 条特别要紧 —— 它是"判据从'不碰 sys'收紧为'不写 stdout'"之后
    唯一需要放行的写法，漏了这条就等于把兜底日志重新禁掉。
    """
    path = tmp_path / "ok.py"
    path.write_text(
        '"""说明：日志走 sys.stderr，stdout 是协议流，别 print 到 sys.stdout。"""\n'
        "import sys\n"
        "\n"
        "def f(writer, log):\n"
        "    print('fallback', file=sys.stderr)\n"
        "    writer.write('frame')\n"
        "    log('fine')   # print( 也不能写在注释里被判违规\n",
        encoding="utf-8",
    )
    source = path.read_text(encoding="utf-8")

    assert stdout_write_offenders(source) == []
    assert process_stream_references(source) == []


def test_configuring_a_stream_is_a_reference_but_not_a_write(tmp_path: Path) -> None:
    """`sys.stdout.reconfigure(...)`：规则 A **放行**，规则 B **拦下**。

    这条同时锁两件事，也是规则 B 为什么必须单独存在、以及入口模块为什么必须豁免的
    **第二个**理由（第一个是 `run(reader=…, writer=…)`）：

    - 规则 A 不管它：`reconfigure` 调的是**流配置**（编码、换行），
      不是往流里塞字节。把它算成"写 stdout"就会把入口层的正当职责禁掉 ——
      而正是这一层在钉 `errors="replace"` 与 `newline="\\n"`（边界 #9 与 Windows 分帧）；
    - 规则 B 拦它：它**确实**引用了 `sys.stdout`。所以任何非入口模块想去配标准流，
      都会在规则 B 上撞墙 —— 那是对的，配流是入口层的事。
    """
    path = tmp_path / "configures.py"
    path.write_text(
        "import sys\n"
        "\n"
        "def f():\n"
        "    sys.stdout.reconfigure(encoding='utf-8', newline='\\n')\n",
        encoding="utf-8",
    )
    source = path.read_text(encoding="utf-8")

    assert stdout_write_offenders(source) == []
    assert process_stream_references(source) == ["sys.stdout"]


def test_serve_takes_all_three_dependencies_by_injection() -> None:
    """签名即契约：`reader` / `writer` / `dispatch` 都是参数，没有隐藏全局。"""
    signature = inspect.signature(serve)
    assert list(signature.parameters)[:3] == ["reader", "writer", "dispatch"]
    assert signature.parameters["log"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["max_message_chars"].kind is inspect.Parameter.KEYWORD_ONLY


def test_two_runs_on_fresh_streams_are_independent() -> None:
    """没有跨调用残留状态（模块级缓存、全局 id 计数器之类）。"""
    first = _run(_request("a", request_id=1))
    second = _run(_request("a", request_id=1))

    assert first[1] == second[1]
