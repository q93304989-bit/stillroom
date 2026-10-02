"""MCP Server（P1）—— 手写极简 JSON-RPC over stdio。

依赖方向：`mcp_server/ → runtime/ → validator/ → schemas/`。
本包不得 import PySide6 或 `app.*`，也不得打开文件/读全局配置 —— 一切由入口注入。

| 模块 | 职责 |
|---|---|
| `jsonrpc.py` | 分帧 + 信封校验 + 单条错误隔离 + **串行**主循环 |
| `dispatch.py` | 方法名 → 函数的路由；方法未找到在这里判定 |
| `mcp.py` | **MCP 方法层**：11 个工具 → 3 个标准方法（`initialize` / `tools/list` / `tools/call`） |
| `tools.py` | 11 个工具的实现（读 params → 调 runtime → 返回 result dict） |
| `identity.py` | **身份的唯一来源**：启动配置的三种预设 → `creator_context` |
| `stdio.py` | 入口层：组装 `ServerContext`、把标准流接到 `serve()`、收连接 |
| `__main__.py` | CLI / 环境变量 / 默认路径 / 退出码 + 流编码 |

**wire 上是 3 个方法，不是 11 个** —— 11 个工具名当方法名的话，标准 MCP 客户端
一个都连不上（它只发 `initialize` / `tools/list` / `tools/call`）。见 `mcp.py`。

**这一层没有身份信息**（`identity.py` 只把启动配置翻成 `creator_context`）。
`mcp_server/` 自己不读环境变量也不解析 argv —— 那是 `__main__.py` 的活，
它读完之后以**显式参数**交给 `stdio.build_server_context`。

**一条铁律**：`sys.stdout` 只走协议帧。任何日志、诊断、traceback 一律 `sys.stderr`。
判据是**标准流**而非"碰 `sys`" —— `jsonrpc.py` 的 `log` 不注入时会兜底写
`sys.stderr`（静默丢弃会让入口忘了传 `log` 时完全无声）。
`tests/test_mcp_jsonrpc.py` 用 AST 扫描锁死，且拆成**两条独立规则**：

| 规则 | 内容 | 范围 |
|---|---|---|
| A | 不**写** stdout（含裸 `print`） | 全包（入口也不能写） |
| B | 不**引用** `sys.stdin` / `sys.stdout` | 除 `__main__.py`（它必须引用，才接得上 `run`） |

拆开不是洁癖：只留 A 的话"`sys.stdin.readline()` 自己读"是合规的，
但那样 `serve()` 的 `reader` 注入就形同虚设，测试再也没法用 `io.StringIO` 顶掉它。

`writer` 是协议的唯一出口：本包任何模块都不写进程 stdout。
"""

from .dispatch import ToolFn, make_dispatch
from .identity import IDENTITY_CHOICES, IDENTITY_PRESETS, build_creator_context
from .jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    DispatchFn,
    JsonRpcError,
    serve,
)
from .tools import CONTRACT_VERSION, InputError, ServerContext, TOOLS, ToolImpl, build_toolset

# `stdio` 与 `__main__` **不在这里 re-export**：它们是入口层，
# 由调用方显式 `from mcp_server import stdio`。理由不是洁癖 ——
# `stdio` 需要 `dispatch`/`jsonrpc`/`tools`，在包 `__init__` 里 import 它
# 会让"先有鸡还是先有蛋"取决于本文件的语句顺序，那种脆弱不值得换一个短名字。

ENTRY_MODULE = "mcp_server.__main__"
"""本包**唯一**的入口模块（点号全名，与 `__name__` 同形）。

它是"豁免"的**单一出处**：源码扫描的规则 B（不引用 `sys.stdin` / `sys.stdout`）
只放行这一个模块，而那条规则会被多个测试文件用到。写成本包的常量之后，
"谁能碰标准流"只有一处定义 —— 将来若要加第二入口（`cli.py` 之类），
必须**先改这里**；改不动就说明那个模块本来就不该碰标准流。

`tests/test_headless_boundary.py::test_there_is_exactly_one_entry_module`
反过来验这件事：遍历包内所有模块，**实际引用**标准流的那个集合必须恰好等于 `{ENTRY_MODULE}`。
于是"唯一入口"是条硬约束，而不是两个测试文件里各写一遍的字符串比对。
"""

__all__ = [
    "ENTRY_MODULE",
    # 传输层
    "serve",
    "JsonRpcError",
    "DispatchFn",
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    # 路由
    "make_dispatch",
    "ToolFn",
    # 工具层
    "build_toolset",
    "ServerContext",
    "InputError",
    "ToolImpl",
    "TOOLS",
    "CONTRACT_VERSION",
    # 身份
    "IDENTITY_CHOICES",
    "IDENTITY_PRESETS",
    "build_creator_context",
]
