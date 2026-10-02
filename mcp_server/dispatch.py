"""方法名 → 工具函数的路由。**只有一条判定。**

```
method 在表里 → 调用它，把返回值原样作为 result
method 不在   → METHOD_NOT_FOUND（走 error 字段）
```

**其余一切都不是本模块的事。** 尤其：

- `params` 缺字段、类型不对、值越界 → **不是**这里的错误。
  那是业务错误，由 `tools.py` 返回 `{"ok": false, "error": {...}}`，
  照样走 `result`。在这里拒绝会让"工具不存在"与"参数写错了"在客户端看来一模一样。
- `params` 不是对象 → 已经由 `jsonrpc.py` 在信封层拦下（`-32602`），到这里必然是 dict。
- 工具抛异常 → **不在这里兜**。业务异常由 `tools.py` 的统一包装转成结构化错误；
  真正的编程错误（`TypeError` / `KeyError`）让它继续往上抛，
  由 `jsonrpc.serve()` 兜成 `-32603`。这里 `try/except` 只会把编程错误
  伪装成业务错误，让真缺陷在日志里消失。

## 为什么方法表是**注入**的，不是 import 进来的

`make_dispatch(methods)` 收一张 `{方法名: 函数}` 表，模块本身
**不 import `tools` / `mcp` / `runtime` / `validator`**。三个后果，都是想要的：

1. `dispatch.py` 可以脱离数据库单测 —— 塞一张假表就够；
2. 路由的正确性（精确匹配、无副作用、快照）与 11 个工具的业务逻辑互不干扰，
   两边各自的测试都更小更准；
3. `stdio.py` 成为唯一的组装点，与 `serve()` 收 `dispatch` 是同一个风格：
   **每一层的能力都由上层注入，而不是自己去摸全局。**

**表里装的是方法名，不是工具名。** `stdio.py` 喂进来的是
`mcp.make_method_table(...)` 的产物 —— 三个标准 MCP 方法
（`initialize` / `tools/list` / `tools/call`）加若干个通知。
本模块对此**一无所知，也不想知情**：它只认"名字在不在表里"。
（早先这里直接收 `build_toolset` 的结果，于是 11 个工具名变成了方法名，
标准 MCP 客户端一个都连不上 —— 那次改动的落脚点是 `mcp.py`，不是这里。）

工具清单的单一来源仍是 `tools.TOOLS`：`get_capabilities` 与 `tools/list` 都从它取，
所以"契约列了 11 个、实现少了一个"这类漂移**照样**无处可藏。

表里的值统一按 `ToolFn` 类型标注：**收 `params`、回 `result` 的可调用**。
工具与 MCP 方法是同一个形状，所以同一张路由表能装下两者。

## 匹配是精确的，不做任何规范化

`"get_capabilities"` 就是它自己。大小写、前后空白、连字符/下划线互换
**一律不匹配**。理由：MCP 的工具名是精确标识符，
fuzzy 匹配会让 `"execute_workflow"` 的拼写错误**静默命中另一个工具** ——
那比报错危险得多。拼错了就应当拿到 `-32601`，并附上可用清单。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .jsonrpc import METHOD_NOT_FOUND, DispatchFn, JsonRpcError, LogFn

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]
"""一个工具：收 `params`，回 `result`（dict）。只做业务，不管协议。"""


def make_dispatch(
    methods: Mapping[str, ToolFn],
    *,
    log: LogFn | None = None,
) -> DispatchFn:
    """把一张方法表包成 `DispatchFn`。

    `methods` 在**构造时**浅拷贝一份：之后调用方再改自己那张 dict
    不会影响已建好的路由 —— 否则"服务跑起来之后路由悄悄变了"这种事
    在并发下无法复现、也无法测试。

    非法表项（名字不是非空字符串、值不可调用）在**构造时**就抛 `ValueError`：
    这是编程错误，越早暴露越好，不该等到某次调用才发现。
    """
    table: dict[str, ToolFn] = {}
    for name, fn in methods.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"tool name must be a non-empty string, got {name!r}")
        if not callable(fn):
            raise ValueError(f"tool {name!r} is not callable: {fn!r}")
        table[name] = fn

    # 错误里回这份清单是**有界**的：方法表由 `mcp.make_method_table` 定死
    # （三个标准方法 + 四个通知）。客户端拼错一个字母时，
    # 能在错误里直接看到正确拼法，不必去翻文档。
    available = sorted(table)

    def dispatch(method: str, params: dict[str, Any]) -> dict[str, Any] | JsonRpcError:
        tool = table.get(method)
        if tool is None:
            _log(log, f"dispatch: unknown method {method!r}")
            return JsonRpcError(
                METHOD_NOT_FOUND,
                f"unknown method: {method}",
                data={"method": method, "available": available},
            )
        # 返回值原样透传：它是不是 `{"ok": false, ...}` 由 `tools.py` 决定，
        # 本模块不检查、不包装、不翻译。
        return tool(params)

    return dispatch


def _log(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


__all__ = ["make_dispatch", "ToolFn"]
