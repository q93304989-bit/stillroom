"""MCP 方法层：**三个标准方法**，把内部 11 个工具接到线上。

```
stdio（wire）上只有三个方法：
    initialize   → 握手：protocolVersion / capabilities / serverInfo
    tools/list   → 工具清单（name + description + inputSchema）
    tools/call   → {name, arguments} → 内部工具，结果包成 MCP content
```

## 为什么必须有这一层

`dispatch.py` 是**通用**的「表 → 函数」路由，它不关心表里装的是什么。
`stdio.py` 早先直接 `make_dispatch(build_toolset(ctx))`，于是 **11 个工具名就是方法名** ——
标准 MCP 客户端永远不会发 `get_capabilities` 这种方法，它只发 `tools/call`，
结果就是**一个标准客户端都连不上**，而 P1 的阶段目标恰恰是"能被 MCP 客户端连上"
（`docs/P1-实施计划.md` §一、§三 WP1、决策 #1）。

本模块只做一件事：**换一张表**。把 11 个工具名收进 `tools/call` 的 `params.name`，
给 `dispatch.py` 一张 3 项的方法表。`dispatch.py` 因此**一行不改**，
它仍然"只有一条判定"（方法名在不在表里）。

组装顺序（`stdio.run`）就是这一句话：

```
build_toolset(ctx)   → 11 个绑好 ctx 的工具
make_method_table(…) → 3 个 MCP 方法（name → 函数）
make_dispatch(…)     → 方法表 → 路由（未知方法 → -32601）
serve(reader, writer, dispatch)
```

★ `mcp.py` 是**我们手写的**方法层，与官方 `mcp` SDK 无关（决策 #1：零新第三方依赖）。

## 错误码分工（契约 §五.1 / §五.7）

| 情形 | 走哪 | 谁判 |
|---|---|---|
| 方法名不认识（不是这三个） | `error` `-32601` | `dispatch.py` |
| `tools/call` 的 `name` / `arguments` 形状不对 | `error` `-32602` + `data.reason` | 本模块 |
| 工具返回 `{"ok": false}`（含**入参不合法**） | `result` 里 `isError: true` | `tools.py` |

最后一条正是 MCP 的规定（tool execution error → `isError`），
也正好落回契约那条"**业务错误绝不走 `error` 字段**" —— 两套规矩是同一件事。

## `-32602` 为什么要带 `data.reason`

`-32602` 一个码扛了四种**客户端反应完全不同**的情形。只给 message 的话，
客户端只能靠字符串匹配分支 —— 那是脆弱的（改一个字就崩），
所以给一个稳定字段：

| `data.reason` | 客户端该做什么 |
|---|---|
| `unknown_tool` | 检查**工具名**拼写（`data.available` 里有全部 11 个） |
| `invalid_name` | `name` 缺失 / 不是字符串 —— 检查调用形状 |
| `invalid_arguments` | `arguments` 不是对象 —— 检查调用形状 |
| `unexpected_param` | 出现了不认识的字段（可能是拼错） |

★ **`arguments` 里缺字段 / 类型不对，不在这里。** 那是工具自己的入参问题，
由 `tools.py` 返回 `{"ok": false, "error": {"code": "INPUT_SCHEMA_INVALID", …}}`，
包在 `result` 的 `isError: true` 里。客户端要处理的**参数校验失败**走的是那条路，
不是 `-32602` —— 混起来会把"服务器拒绝执行"说成"协议调用写错了"。
两者都是"参数不行"，但一层是**调用形状**、一层是**业务语义**。

## 通知

`notifications/initialized` 之类是 MCP 的**通知**（不带 `id`）。`jsonrpc.serve()`
对不带 `id` 的消息**本来就不响应**，所以它们天然安全。这里登记它们只为让日志
不把它们报成 `unknown method` —— 那是句错话。

## P1 的两处如实留白

1. **`inputSchema` 是宽松的** `{"type": "object", "additionalProperties": true}`。
   不为 11 个工具手抄 11 份 JSON Schema：那是一份会和实现漂移的第二真理，
   而真正的入参校验在**服务端**（`tools.py` 的业务层，错误码取自 `ErrorCode`）。
   人读的入参说明在 `contracts/mcp-tools.md` §二。
2. **没有握手状态**。MCP 规定客户端应先 `initialize`；P1 不做「未握手就不许调工具」
   的门禁 —— `serve()` / `dispatch.py` 都是无状态的，加状态会分成两个会话模型。
   登记这件事本身写在这里，免得以后被当成漏做。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .jsonrpc import INVALID_PARAMS, JsonRpcError, LogFn

MCP_VERSION = "2024-11-05"
"""本服务声明的 MCP 协议版本。

协商规则（MCP 规定）：客户端请求的版本**受支持就原样回**，
不受支持就回一个我们支持的版本，由客户端决定要不要继续。
"""

SUPPORTED_PROTOCOL_VERSIONS = (MCP_VERSION,)
"""P1 只声明**一个**版本。

多版本意味着"同一份代码要按版本改行为"，而 P1 的三个方法在所列版本里形状一致 ——
先把版本表列成一个元组，将来真要支持第二个版本时，改动点是明确的。
"""

SERVER_NAME = "stillroom-protocol"
SERVER_VERSION = "1.0.0"
"""`serverInfo`。名字与 `stdio.SERVICE_NAME` 一致，读日志和读握手是同一个名字。"""


TOOL_SUMMARIES: dict[str, str] = {
    "get_capabilities": "握手：返回工具清单、能力集与本次进程的身份",
    "list_skills": "列出已登记的技能，可按 capability 过滤",
    "create_skill": "登记一个技能定义（入参即定义文档本身）",
    "list_workflows": "列出工作流定义及其版本与激活状态",
    "match_workflow": "按意图匹配工作流；P1 恒定返回 none",
    "create_workflow": "登记工作流定义（经四层校验后落库）",
    "execute_workflow": "执行工作流；同一 request_id 幂等",
    "retry_execution": "对 FAILED 的执行发起重试（不接受输入覆盖）",
    "get_execution_status": "读一次执行的当前状态、步骤与预算",
    "abort_execution": "请求中止执行（RUNNING 下先进入 ABORT_PENDING）",
    "resume_execution": "从 WAITING_INPUT 恢复，补一份输入",
}
"""`tools/list` 里给客户端（其实是给客户端的 LLM）看的**一行描述**。

为什么不从各工具的 `__docstring__` 里截首行：那 11 份 docstring 是写给**维护者**的
实现说明（"入参：无。"、"没有 override 字段（契约 §二.8）"），有的干脆是空的 ——
拿它们当面向 LLM 的工具描述，等于让模型去猜这个工具干什么。

键集必须与 `tools.TOOLS` 完全一致，由 `tests/test_mcp_methods.py` 锁死
（与 `IDENTITY_FIELDS` 同一套路：**单一来源 + 两端同步测试**）。
"""

NOTIFICATION_METHODS = (
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
    "notifications/roots/list_changed",
)
"""客户端会发的通知（无 `id`，因此 `serve()` 不响应）。

登记它们**不是为了处理**，而是为了日志诚实：不登记的话，每次标准客户端连上来
都会在 stderr 留一句 `unknown method 'notifications/initialized'` —— 那是假的，
它不是未知方法，是通知。
"""

_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}
"""见模块说明「P1 的两处如实留白」第 1 条。"""

# ---- `-32602` 的 `data.reason` 取值（稳定字段，客户端据此分支）----------------

REASON_UNKNOWN_TOOL = "unknown_tool"
"""`name` 是个字符串，但表里没这个工具 —— 客户端该查**工具名拼写**。"""

REASON_INVALID_NAME = "invalid_name"
"""`name` 缺失或不是字符串 —— 客户端该查**调用形状**。"""

REASON_INVALID_ARGUMENTS = "invalid_arguments"
"""`arguments` 存在但不是对象 —— 客户端该查**调用形状**。"""

REASON_UNEXPECTED_PARAM = "unexpected_param"
"""出现了不认识的字段（很可能是拼错）。`tools/list` 与 `tools/call` 共用这个取值。"""

INVALID_PARAMS_REASONS: tuple[str, ...] = (
    REASON_INVALID_NAME,
    REASON_UNKNOWN_TOOL,
    REASON_INVALID_ARGUMENTS,
    REASON_UNEXPECTED_PARAM,
)
"""**全部**取值，顺序与契约 §五.7 那张表一致。

导出它有两个用处：① 测试拿它与契约表**机器比对**（少了/多了都红）；
② 将来加取值时，"契约改了没、客户端分支改了没"两件事都躲不掉。
"""

MethodFn = Callable[[dict[str, Any]], "dict[str, Any] | JsonRpcError"]


def _invalid_params(reason: str, message: str, **data: Any) -> JsonRpcError:
    """构造带 `data.reason` 的 `-32602`。

    四种情形共用这一个构造点，于是"忘了带 reason"在代码里看不出来 ——
    所以 `reason` 是位置参数且**只能是** `INVALID_PARAMS_REASONS` 里的值
    （`tests/test_mcp_methods.py` 把这条也锁了）。
    """
    if reason not in INVALID_PARAMS_REASONS:
        raise ValueError(f"unknown invalid-params reason: {reason!r}")
    return JsonRpcError(INVALID_PARAMS, message, data={"reason": reason, **data})


def make_method_table(
    toolset: Mapping[str, Callable[[dict[str, Any]], Any]],
    *,
    server_name: str = SERVER_NAME,
    server_version: str = SERVER_VERSION,
    protocol_version: str = MCP_VERSION,
    log: LogFn | None = None,
) -> dict[str, MethodFn]:
    """把 11 个工具包成 3 个 MCP 方法。返回可直接喂给 `make_dispatch` 的表。

    `toolset` 就是 `build_toolset(ctx)` 的产物（`{工具名: 收 params 回 dict}`）。

    **`tools/list` 的清单直接取自 `toolset` 的键**，不另存一份 ——
    否则"清单说有、`tools/call` 却不认"这种漂移又要靠人眼发现
    （与 `get_capabilities` 取 `TOOLS` 是同一条规矩：**表里有什么就是什么**）。
    描述按名字去查 `TOOL_SUMMARIES`，查不到是空串（不该发生，同步测试锁着）。
    """
    table: dict[str, Callable[[dict[str, Any]], Any]] = dict(toolset)
    catalog = [
        {
            "name": name,
            "description": TOOL_SUMMARIES.get(name, ""),
            "inputSchema": dict(_INPUT_SCHEMA),
        }
        for name in sorted(table)
    ]

    def _log(message: str) -> None:
        if log is not None:
            log(message)

    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        """握手。`params` 里可能有 `protocolVersion` / `capabilities` / `clientInfo`。

        本方法**不挑 params 的字段**：MCP 客户端发什么由它自己决定（各版本字段不同），
        在这里做白名单只会把新版客户端拒在门外。它又不是业务工具，没有入参契约。
        """
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            version = requested
        else:
            version = protocol_version
            if requested is not None:
                _log(f"mcp: client asked protocol {requested!r}, answering {version!r}")
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": server_name, "version": server_version},
        }

    def tools_list(params: dict[str, Any]) -> "dict[str, Any] | JsonRpcError":
        """工具清单。P1 不分页：不返回 `nextCursor`。

        只放行 `cursor`（MCP 的分页键），其余字段一律拒 —— 这是**协议**方法，
        写错的字段名属于调用错误，不该被静默忽略。因为从不返回 `nextCursor`，
        合规客户端不会翻页；带上来的 `cursor` 也就无关紧要。
        """
        unexpected = sorted(set(params) - {"cursor"})
        if unexpected:
            return _invalid_params(
                REASON_UNEXPECTED_PARAM,
                f"unexpected param(s) for tools/list: {', '.join(unexpected)}",
                unexpected=unexpected,
            )
        return {"tools": [dict(item) for item in catalog]}

    def tools_call(params: dict[str, Any]) -> "dict[str, Any] | JsonRpcError":
        """调用一个内部工具。

        `params` = `{"name": <工具名>, "arguments": <对象，可缺省>}`（`_meta` 忽略）。

        **不 catch 工具抛出的异常**：`tools.py` 的 binder 只把 `StillroomRuntimeError`
        翻成 `{"ok": false}`，剩下的（`TypeError` / `KeyError`）必须继续往上冒，
        由 `jsonrpc.serve()` 兜成 `-32603`。在这里 catch 会把真缺陷伪装成业务失败。
        """
        unexpected = sorted(set(params) - {"name", "arguments", "_meta"})
        if unexpected:
            return _invalid_params(
                REASON_UNEXPECTED_PARAM,
                f"unexpected param(s) for tools/call: {', '.join(unexpected)}",
                unexpected=unexpected,
            )

        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _invalid_params(
                REASON_INVALID_NAME, "tools/call requires a string 'name'"
            )

        tool = table.get(name)
        if tool is None:
            # -32602 而不是 -32601：`tools/call` 这个方法在，错的是 name。
            # 附上可用清单，客户端拼错一个字母时能直接看到正确拼法（有界：11 个）。
            return _invalid_params(
                REASON_UNKNOWN_TOOL,
                f"unknown tool: {name}",
                name=name,
                available=sorted(table),
            )

        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _invalid_params(
                REASON_INVALID_ARGUMENTS,
                f"'arguments' must be an object, got {type(arguments).__name__}",
                got=type(arguments).__name__,
            )

        result = tool(arguments)
        if not isinstance(result, dict):
            # 工具函数是内部契约的直接实现，返回非 dict 是**编程错误** ——
            # 让它抛，`serve()` 会翻成 -32603，别在这里包装成业务失败。
            raise TypeError(f"tool {name!r} returned {type(result).__name__}, expected dict")
        return _content(result)

    def _ignore_notification(params: dict[str, Any]) -> dict[str, Any]:
        """通知的落点。`serve()` 不会响应它，返回值只为了类型一致。"""
        return {}

    methods: dict[str, MethodFn] = {
        "initialize": initialize,
        "tools/list": tools_list,
        "tools/call": tools_call,
    }
    methods.update({name: _ignore_notification for name in NOTIFICATION_METHODS})
    return methods


def _content(result: dict[str, Any]) -> dict[str, Any]:
    """把内部工具的返回值包成 MCP 的 `CallToolResult`。

    `isError` 只认 `{"ok": true}` 这一种成功 —— 用 `is True` 而不是真值判断，
    免得 `{"ok": 1}` / `{"ok": "yes"}` 这种写错信封的产物被当成成功放过去。

    文本用 `sort_keys` + 紧凑分隔符：帧是给人看也是给测试比的，
    键序随机会让"同一份结果"出现两种字节，测试只能退化成解析后比较。
    """
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "isError": result.get("ok") is not True,
    }


__all__ = [
    "INVALID_PARAMS_REASONS",
    "MCP_VERSION",
    "NOTIFICATION_METHODS",
    "REASON_INVALID_ARGUMENTS",
    "REASON_INVALID_NAME",
    "REASON_UNEXPECTED_PARAM",
    "REASON_UNKNOWN_TOOL",
    "SERVER_NAME",
    "SERVER_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOL_SUMMARIES",
    "make_method_table",
]
