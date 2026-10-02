"""newline-delimited JSON-RPC 2.0 主循环（MCP 用的就是这一种）。

## 为什么是换行分隔，不是 `Content-Length`

MCP 走 **newline-delimited JSON-RPC 2.0**：一行一条消息，**没有** LSP 那种
`Content-Length` 头。选错就跟 Claude Desktop / Cursor 的实际行为对不上。

三条分帧规则：

1. 每条消息以 `\n` 结尾；消息内部**不允许**出现裸 `\n`（JSON 里必须转义成 `\\n`），
   所以"一行 = 一条消息"成立。
2. **真空行跳过** —— 客户端偶发多写一个换行不该炸。判据是 `line == "\n"`，
   不是 `line.strip() == ""`：见下面"空行的精确判定"。
3. 单行有长度上限（默认 4 MiB 字符）：超长的那一行**整行丢弃**并回一条
   `-32700`，不会把它的残余当成下一条消息，也不会为了读完它把内存吃光。

## 空行的精确判定：只认真空行

`"\n"` 跳过；`"   \n"` 之类**纯空白行按解析失败处理**（`-32700`）。

理由：JSON 规范里空白**不是**合法值，纯空白行意味着客户端发了畸形数据。
跳过它等于把问题藏起来 —— 而"藏起来"的代价是将来某个客户端持续发空行时，
服务端一声不吭地空转，没有任何线索。真要宽松也该记日志而不是静默。
所以判定用 `line == "\n"`（去掉尾换行后 `== ""`），不是 `strip()`。

## `NaN` / `Infinity` / `-Infinity`：在入口就拒

Python 的 `json.loads` **默认接受** `NaN` / `Infinity` / `-Infinity`
（这是 Python 的扩展，不是 JSON 规范）。不处理的话：

```json
{"jsonrpc":"2.0","id":1,"method":"foo","params":{"x":NaN}}
```

解析成功、`x = float("nan")` 一路往 runtime 传，等落库时 `json.dumps` 才炸 ——
那时离错误源头已经很远，栈里全是仓库层的帧。

所以解析时传 `parse_constant`，遇到这三个 token 直接 `-32700`。
**在入口拒绝的成本，比在仓库层排查低一个数量级。**

## 批量请求：显式拒绝，不静默也不逐条处理

JSON-RPC 2.0 允许顶层是数组（批量请求）。MCP **不使用**批量，但客户端可能误发。

顶层是数组 → 回**一条** `-32600`（`id: null`）。三条理由：

1. 逐条处理会产生"部分成功、部分失败"的响应数组，
   `serve()` 的"一条入、一条出"不变式当场被破坏；
2. 静默丢弃会让客户端等不到任何响应而**卡死**；
3. 显式 `-32600` 是规范允许的拒绝方式。

不定义的话，将来某个客户端误发批量时行为不确定 —— 这正是要避免的。

## `id` 语义：原样回显，不做任何规范化

| 客户端发的 | 回的 | 说明 |
|---|---|---|
| `"id": 1` | `"id": 1` | 数字保持数字 |
| `"id": "1"` | `"id": "1"` | `1` 与 `"1"` 是**不同**的 ID，绝不强转 |
| `"id": null` | `"id": null` | `null` 是合法 ID，**仍要响应** |
| 无 `id` 字段 | —— | **通知**，不响应 |

关键：`"id": null` 与缺 `id` 是两回事，靠 `"id" in message` 判定，不靠真值判断。

## 错误码分工：传输层 vs 业务层（这条最容易漏）

| 层 | 错误 | 响应形态 |
|---|---|---|
| 传输层 | 解析失败 / 信封无效 / 方法未找到 / params 不是对象 | `{"jsonrpc":"2.0","id":…,"error":{"code":-32xxx,…}}` |
| **业务层** | `SCHEMA_INVALID` / `EXECUTION_NOT_FOUND` / `RETRY_EXHAUSTED` … | `{"jsonrpc":"2.0","id":…,"result":{"ok":false,"error_code":"…","message":"…"}}` |

**业务错误一律走 `result` 里的结构化对象，绝不走 `error` 字段。**
JSON-RPC 的 `error` 表示"调用链断了"（方法不存在、信封坏掉），
而 `SCHEMA_INVALID` 表示"调用链通了、工具告诉你输入不合法"。混在一起，
客户端没法区分"方法不存在"与"参数写错了"。

本模块对这一分工的强制方式：**只把 `dispatch` 返回的 `JsonRpcError` 当错误**，
其它返回值一律原样塞进 `result`。所以 `tools.py` 里的工具函数永远不构造
JSON-RPC 错误对象 —— 那不是它们的职责。

### `dispatch` 抛异常：由本模块兜底为 `-32603`

`dispatch` **未捕获**的异常在这里被兜成 `-32603 Internal error`：

- 让异常冒泡的话，客户端只看到连接断开，**拿不到任何诊断信息**，
  也对不上是哪条请求出的问题；
- 当成 `-32601` 语义就错了，会**掩盖真 bug**（"工具内部炸了"被说成"没这个工具"）。

堆栈只进 `stderr`，**不回显给客户端** —— 异常文本可能带路径、SQL、内部结构。

因此 `tools.py` 的分工是：
**业务错误 → 返回 `result` 里的结构化 `error_code`；
编程错误（`TypeError` / `KeyError` / 不该发生的分支）→ 让它抛，由这里兜底。**
工具函数不去 `try/except` 自己的 bug —— 那样只会把编程错误伪装成业务错误，
让真正的缺陷在日志里消失。

## 串行，不引入并发

读到一条、处理一条，不启线程 / 协程 / 队列。`dispatch` 后面的
`ExecutionRepository` 是**单连接 + RLock**，多线程会踩锁；
`jsonrpc.py` 保持串行，后面所有层才能维持"单线程假设"。
（应用层的并发由 Stillroom 自己的调度负责，不是传输层的职责。）

## 单条坏消息绝不掀桌子

解析失败、信封无效、dispatch 抛异常 —— 一律记 stderr、回错误响应、
**继续读下一条**。只有 stdin 读不出来或 stdout 写不进去才返回非 0。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import IO, Any, Callable

# ---- JSON-RPC 2.0 标准错误码（传输层专用）--------------------------------

PARSE_ERROR = -32700
"""`json.loads` 都过不去。id 未知 → 回 `id: null`（规范如此规定）。"""

INVALID_REQUEST = -32600
"""信封无效：缺 `jsonrpc`、`jsonrpc` 值不对、`method` 缺失或非字符串、`id` 类型非法。"""

METHOD_NOT_FOUND = -32601
"""方法不存在。由 `dispatch` 返回 `JsonRpcError` 表达。"""

INVALID_PARAMS = -32602
"""`params` 存在但不是对象。**注意**：`params` 内部字段不合法属于**业务层**，
走 `result` 里的结构化错误，不走这里。"""

INTERNAL_ERROR = -32603
"""`dispatch` 抛了异常，或返回了既不是 dict 也不是 `JsonRpcError` 的东西。

对客户端只说"内部错误"，细节（含 traceback）只进 stderr ——
异常文本可能带路径、SQL、内部结构，不该出现在协议流里。
"""

JSONRPC_VERSION = "2.0"

DEFAULT_MAX_MESSAGE_CHARS = 4 * 1024 * 1024
"""单行上限。超长整行丢弃并回 `-32700`，见模块说明第 3 条。"""

# `json.loads` 的 `parse_constant` 钩子只在这三个 token 上被调用。
# 抛 `ValueError` 会被 `_handle_one` 的 `except ValueError` 接住，翻成 `-32700`。
_NON_FINITE_TOKENS = frozenset({"NaN", "Infinity", "-Infinity"})


def _reject_non_finite(token: str) -> Any:
    """`json.loads(..., parse_constant=...)` 的钩子：拒掉非标准的三个常量。

    Python 把 `NaN` / `Infinity` / `-Infinity` 当合法 JSON 收下（扩展行为），
    但它们进不了标准 JSON —— 一旦流到 runtime 的 `json.dumps` 就会在**离错误源头
    很远**的地方炸。在入口拒掉，栈里就只有这一帧。
    """
    raise ValueError(f"non-finite number is not valid JSON: {token}")


def _default_log(message: str) -> None:
    """未注入 `log` 时的兜底：写 `sys.stderr`。

    **stderr 从来没被禁止** —— 铁律只针对 stdout（它是协议流）。
    兜底 stderr 而非静默丢弃，是为了 `stdio.py` 万一忘了传 `log` 时
    出问题还有线索，不至于完全无声。
    """
    print(message, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class JsonRpcError:
    """**传输层**错误。业务错误不要用它，见模块说明。"""

    code: int
    message: str
    data: dict[str, Any] | None = None


DispatchFn = Callable[[str, dict[str, Any]], "dict[str, Any] | JsonRpcError"]
"""`(method, params) -> result | JsonRpcError`。

- 返回 `dict` → 原样作为 `result`（业务成功或业务失败都在里面表达）
- 返回 `JsonRpcError` → 作为 `error`（方法未找到之类）
- 抛异常 → 本模块翻译成 `-32603`
"""

LogFn = Callable[[str], None]


def serve(
    reader: IO[str],
    writer: IO[str],
    dispatch: DispatchFn,
    *,
    log: LogFn | None = None,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
) -> int:
    """主循环。返回进程退出码：`0` = 正常读到 EOF；非 0 = 不可恢复的 IO 失败。

    三个依赖全部注入（`reader` / `writer` / `dispatch`），
    **不打开文件、不读全局配置** —— 所以测试直接塞 `io.StringIO`
    就能跑，不需要 subprocess。

    `log` 默认是 `sys.stderr`：不注入也有兜底，出问题时不会完全无声。
    默认实现只碰 stderr，**stdout 永远只由 `writer` 承载协议帧**。
    要接到别的去处（测试收集、日志文件）就传一个 `log` 覆盖它。

    唯一的例外是 `max_message_chars < 1` —— 那是**编程错误**，直接 `ValueError`
    抛给调用方，不当成"某条消息的问题"处理。
    """
    if max_message_chars < 1:
        raise ValueError("max_message_chars must be positive")

    emit_log = log if log is not None else _default_log

    while True:
        line, oversized = _read_line(reader, max_message_chars)
        if line is None and not oversized:
            return 0                                    # EOF：干净退出
        if oversized:
            _log(emit_log, f"jsonrpc: message exceeds {max_message_chars} chars, dropped")
            if not _emit(
                writer,
                _error_envelope(None, JsonRpcError(PARSE_ERROR, "Parse error")),
            ):
                return 1
            continue
        assert line is not None

        # 空行的精确判定：只认**真空行**。`"   \n"` 是畸形数据，不放行。
        body = line[:-1] if line.endswith("\n") else line
        if body == "" and line.endswith("\n"):
            continue
        if not body.strip():
            # 纯空白行（或"最后一行全是空格且无换行"）：按解析失败处理，
            # 与 `json.loads("  ")` 的结果一致，不搞特例。
            _log(emit_log, "jsonrpc: blank-but-not-empty line, treated as parse error")
            if not _emit(writer, _error_envelope(None, JsonRpcError(PARSE_ERROR, "Parse error"))):
                return 1
            continue

        if not _handle_one(body, writer, dispatch, emit_log):
            return 1


# ---------------------------------------------------------------------------
# 单条消息
# ---------------------------------------------------------------------------

def _handle_one(
    text: str,
    writer: IO[str],
    dispatch: DispatchFn,
    log: LogFn | None,
) -> bool:
    """处理一条消息。返回 `False` 仅当 stdout 不可写（要终止主循环）。

    `text` 是**去掉行尾换行后**的原文（不是 strip 过的）——
    这样 `-32700` 的日志里能看出客户端到底发了什么。
    """
    try:
        message = json.loads(text, parse_constant=_reject_non_finite)
    except ValueError as exc:                           # JSONDecodeError 是其子类
        _log(log, f"jsonrpc: parse error: {exc}")
        return _emit(writer, _error_envelope(None, JsonRpcError(PARSE_ERROR, "Parse error")))

    if isinstance(message, list):
        # JSON-RPC 的批量请求。MCP 不用它，但客户端可能误发 —— 见模块说明。
        # 一句话：逐条处理会造出"部分成功"的响应数组、破坏"一条入一条出"；
        # 静默丢弃会让客户端等不到响应而卡死。所以显式拒绝，回**一条** `-32600`。
        _log(log, f"jsonrpc: batch request with {len(message)} entries rejected")
        return _emit(writer, _error_envelope(None, JsonRpcError(INVALID_REQUEST, "Invalid Request")))

    if not isinstance(message, dict):
        _log(log, f"jsonrpc: message is {type(message).__name__}, not an object")
        return _emit(writer, _error_envelope(None, JsonRpcError(INVALID_REQUEST, "Invalid Request")))

    has_id = "id" in message
    request_id = message.get("id")
    if has_id and not _is_valid_id(request_id):
        _log(log, f"jsonrpc: invalid id type {type(request_id).__name__}")
        return _emit(writer, _error_envelope(None, JsonRpcError(INVALID_REQUEST, "Invalid Request")))

    # 信封坏掉时**不能**靠"缺 id"判定它是通知 ——
    # 通知的定义是"well-formed 的请求但不带 id"。信封本身不合法，
    # 就无从知道它本来是什么，所以必须回一条（id 用 null），
    # 而不是默默当成合法请求、也不是默默当成通知丢掉。
    if message.get("jsonrpc") != JSONRPC_VERSION:
        _log(log, "jsonrpc: missing or wrong 'jsonrpc' version field")
        return _emit(writer, _error_envelope(request_id if has_id else None,
                                            JsonRpcError(INVALID_REQUEST, "Invalid Request")))

    method = message.get("method")
    if not isinstance(method, str) or not method:
        _log(log, "jsonrpc: missing or non-string 'method'")
        return _emit(writer, _error_envelope(request_id if has_id else None,
                                            JsonRpcError(INVALID_REQUEST, "Invalid Request")))

    params = message.get("params", {})
    if not isinstance(params, dict):
        # params 缺失 = 无参调用（约定 {}）。存在但不是对象 = 信封坏掉。
        _log(log, f"jsonrpc: params is {type(params).__name__}, not an object")
        return _emit(writer, _error_envelope(request_id if has_id else None,
                                            JsonRpcError(INVALID_PARAMS, "Invalid params")))

    if not has_id:
        # 合法的通知：执行但**不响应**。执行结果只进 stderr。
        try:
            outcome = dispatch(method, params)
        except Exception as exc:                        # noqa: BLE001 - 见下方说明
            _log(log, f"jsonrpc: notification {method!r} failed: {exc!r}")
            return True
        if isinstance(outcome, JsonRpcError):
            _log(log, f"jsonrpc: notification {method!r} -> {outcome.code} {outcome.message}")
        return True

    try:
        outcome = dispatch(method, params)
    except Exception as exc:                            # noqa: BLE001
        # 一**条**消息的失败绝不终止主循环。同时不能把异常文本回给客户端。
        _log(log, f"jsonrpc: dispatch({method!r}) raised {exc!r}")
        return _emit(writer, _error_envelope(request_id, JsonRpcError(INTERNAL_ERROR, "Internal error")))

    if isinstance(outcome, JsonRpcError):
        return _emit(writer, _error_envelope(request_id, outcome))
    if not isinstance(outcome, dict):
        _log(log, f"jsonrpc: dispatch({method!r}) returned {type(outcome).__name__}, expected dict")
        return _emit(writer, _error_envelope(request_id, JsonRpcError(INTERNAL_ERROR, "Internal error")))

    return _emit(writer, _success_envelope(request_id, outcome))


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------

def _success_envelope(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error_envelope(request_id: Any, error: JsonRpcError) -> dict[str, Any]:
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        body["data"] = error.data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": body}


def _is_valid_id(value: Any) -> bool:
    """JSON-RPC 的 id 只能是字符串、数字或 `null`。

    `bool` 是 `int` 的子类，但 `true` 不是合法 id（它是布尔不是数字）—— 显式排除。
    """
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def _read_line(reader: IO[str], limit: int) -> tuple[str | None, bool]:
    """读一行，返回 `(文本, 是否超长)`。文本为 `None` 且不超长 = EOF。

    用**有界**的 `readline(limit + 1)` 而不是 `readline()`：后者会把一整行
    （可能是几个 GB）全读进内存才发现太长。有界读超出上限时，
    把该行残余**丢弃到换行**，避免残余被当成下一条消息。
    """
    head = reader.readline(limit + 1)
    if head == "":
        return None, False
    if head.endswith("\n") or len(head) <= limit:
        # 带换行 = 完整一行；不带换行但没到上限 = 文件末尾最后一行（无换行结尾）
        return head, False

    # 到上限还没换行 → 超长，排空剩余
    while True:
        chunk = reader.readline(limit + 1)
        if chunk == "" or chunk.endswith("\n"):
            break
    return None, True


def _emit(writer: IO[str], envelope: dict[str, Any]) -> bool:
    """写一整帧（一行 JSON + 换行）。返回 `False` 表示对端已断。"""
    line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    try:
        writer.write(line)
        writer.write("\n")
        # 必须 flush：客户端就在等这一行，缓冲住会挂死。
        writer.flush()
    except (BrokenPipeError, ValueError, OSError):
        return False
    return True


def _log(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


__all__ = [
    "serve",
    "JsonRpcError",
    "DispatchFn",
    "LogFn",
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "DEFAULT_MAX_MESSAGE_CHARS",
]
