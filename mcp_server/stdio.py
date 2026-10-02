"""进程入口层：**组装** `ServerContext`，把标准流接到 `serve()`。

这一层是三件事，顺序重要：

```
build_server_context(...)   身份 + 库路径 → ServerContext（可能因配置不对而抛）
run(ctx, reader, writer)    ctx → 工具表 → 方法表 → dispatch → serve() → 退出码
close_quietly(ctx)          释放两个域的连接；失败只记日志，不改退出码
```

## 为什么 `build` / `run` / `close` 分开，而不是一个 `main()`

因为**测试要能只测其中一段**。合成一个 `main()` 的话：

- "身份映射对不对"要起一个进程才知道；
- "退出码是不是被清理异常覆盖了"要构造一次真实的 IO 失败；
- 而 `serve()` 本来就是**注入式**的（`reader` / `writer` / `dispatch` 全从外面给，
  见 `jsonrpc.serve`）—— 它在测试里可以直接塞 `io.StringIO`，不需要子进程。

把这三段露出来，测试就与 `serve()` 同一个风格；`__main__.py` 只负责
"命令行 → 这三段"，单薄到不值得测。**能力的注入方向始终是自上而下**：
本模块自己不读环境变量、不解析 argv（那是 `__main__.py` 的活）。

## 子进程还是同进程？两种都能用，所以不需要选

`app/adapters/protocol_client.py`（GUI 侧的适配层）现在是个 in-process mock，
以后要么起子进程说 stdio，要么在同一进程里复用这套组装。**这个选择不该由本模块替它做**：

- 要子进程 → `python -m mcp_server --db … --identity agent`，本模块就是入口；
- 要同进程 → 自己 `build_server_context(...)`，再把管道喂给 `run(...)`。

两条路一行都不用改，因为 `run` 收的是 `reader` / `writer` 而不是 `sys.stdin`。
（P1 期间 `app/` 一行不改，所以适配层的落地属 GUI 那边的活。）

## 日志

本模块**自己提供一个** `stderr_log` 并显式往下传（`make_dispatch` / `serve` / 清理）。
`jsonrpc` 里那个兜底仍保留，但它从此只兜"忘了传"这一种情况，
而不是日常路径 —— 靠兜底当主路径的话，哪天兜底被改掉就全静默了。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from runtime.artifacts import ArtifactStore
from runtime.registry import WorkflowRegistry
from runtime.repository import ExecutionRepository
from runtime.router import MatchFn

from .dispatch import make_dispatch
from .identity import build_creator_context
from .jsonrpc import LogFn, serve
from .mcp import make_method_table
from .tools import ServerContext, build_toolset

SERVICE_NAME = "stillroom-protocol"
"""进程名，只出现在日志里（stdout 上一个字节都不许有）。"""


def stderr_log(message: str) -> None:
    """本进程的日志落点：`sys.stderr`，非缓冲。

    `flush=True`：stderr 在管道里也可能是缓冲的，
    真出事时"日志还压在缓冲区里"等于没有日志。
    """
    print(message, file=sys.stderr, flush=True)


def build_server_context(
    *,
    db_path: str | Path,
    identity: str,
    artifacts_dir: str | Path | None = None,
    router: MatchFn | None = None,
    log: LogFn | None = None,
) -> ServerContext:
    """组装一次服务进程的全部依赖。参数**全部显式**，本函数不读环境、不看 argv。

    `artifacts_dir` 为 `None` 时不自己算默认值，而是让 `WorkflowRegistry` 用它
    那条既有规则（`<db 所在目录>/artifacts`）—— "一个库 + 一个目录 = 完整备份单元"，
    见 `runtime/registry.py`。默认值只该有一个出处。

    失败是**启动期**失败，一律往上抛（`RepositoryError(SCHEMA_INVALID)` 表示库里
    的 schema 比代码新 —— 那是"库不能给这份代码用"，不是"某次调用出错了"）。
    已经开了一半的连接要先关掉再抛：否则调用方只能看到异常，
    看不到"这个进程还挂着一条连接"。
    """
    repo = ExecutionRepository(db_path)
    try:
        registry = WorkflowRegistry(
            db_path,
            artifacts=ArtifactStore(artifacts_dir) if artifacts_dir is not None else None,
        )
    except BaseException:
        # 清理后**原样再抛**，不吞：这里只负责把已开的连接收掉，
        # 判断"为什么起不来"是调用方的事。
        repo.close()
        raise

    context = ServerContext(
        repo=repo,
        registry=registry,
        creator_context=build_creator_context(identity),
        router=router,
        log=log,
    )
    emit = log or stderr_log
    emit(f"{SERVICE_NAME}: identity={identity} db={repo.db_path}")
    return context


def run(ctx: ServerContext, *, reader: Any, writer: Any, log: LogFn | None = None) -> int:
    """服务主循环，返回进程退出码（`0` = 读到 EOF）。

    组装顺序是三层，方向单一：

    ```
    build_toolset(ctx)      → 11 个绑好 ctx 的工具
    make_method_table(…)    → 3 个标准 MCP 方法（initialize / tools/list / tools/call）
    make_dispatch(…)        → 方法表 → 路由（未知方法 → -32601）
    serve(reader, writer, dispatch)
    ```

    **wire 上只有 3 个方法，不是 11 个** —— 见 `mcp.py` 的模块说明：
    11 个工具名当方法名的话，标准 MCP 客户端一个都连不上。

    **不负责关闭 `ctx`** —— 资源是调用方建的，就由调用方关。
    测试想在同一进程里跑完再看库，正是靠这一点。
    """
    emit = log or ctx.log or stderr_log
    methods = make_method_table(build_toolset(ctx), log=emit)
    dispatch = make_dispatch(methods, log=emit)
    return serve(reader, writer, dispatch, log=emit)


def close_quietly(ctx: ServerContext, *, log: LogFn | None = None) -> None:
    """关掉两个域的连接。**任何失败都只记日志，不抛。**

    为什么吞：调用它的位置在 `main()` 的 `finally` 里，而 `finally` 里抛出的异常
    会**顶掉** `serve()` 的返回值 —— 于是"stdout 写不出去了（退出码 1）"
    会被报成"关库失败"，根因就此丢失。这与 `rollback_quietly` 是同一条规矩：
    **清理阶段的失败不许盖掉真正的结果**。

    两个域各开各的连接（`runtime/base.py` 的有意设计）、彼此没有跨域事务，
    所以关闭顺序无关紧要 —— 但**两个都要关**，只关一个会留着 WAL 句柄。
    """
    emit = log or ctx.log or stderr_log
    for name, domain in (("registry", ctx.registry), ("repository", ctx.repo)):
        try:
            domain.close()
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：这里必须吞
            emit(f"{SERVICE_NAME}: closing {name} failed: {exc!r}")


__all__ = [
    "SERVICE_NAME",
    "build_server_context",
    "close_quietly",
    "run",
    "stderr_log",
]
