"""`python -m mcp_server` —— 命令行入口。

只做四件事：**解析 argv / 环境变量 → 配好标准流 → 调 `stdio` 的三段 → 退出码**。
所有业务逻辑在 `mcp_server/stdio.py`，所有协议逻辑在 `jsonrpc.py` / `dispatch.py` / `tools.py`。

```
python -m mcp_server --db D:\\data\\protocol.db --artifacts D:\\data\\artifacts --identity agent
```

## 路径与身份：参数 > 环境变量 > 默认（`--identity` 没有默认）

| 项 | 参数 | 环境变量 | 缺省 |
|---|---|---|---|
| 库 | `--db` | `STILLROOM_PROTOCOL_DB` | `%APPDATA%\\Stillroom\\protocol.db`（无 `APPDATA` 时 `~/.stillroom/protocol.db`） |
| 产物目录 | `--artifacts` | `STILLROOM_ARTIFACTS_DIR` | **不给** —— 由 `WorkflowRegistry` 用 `<库所在目录>/artifacts`（默认值只该有一个出处） |
| 身份 | `--identity` | `STILLROOM_IDENTITY` | **无缺省，必填** |

链的前两段是 `docs/P1-实施计划.md` 定下的（决策 #2）。`--identity` 不给缺省是**唯一**一处
我把它做得比"有个安全默认"更严的地方：身份是安全模型的最后一环，
"忘了配"不该有静默落点（理由见 `identity.build_creator_context`）。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 正常：stdin 读到 EOF（客户端关了管道） |
| `1` | 运行期不可恢复 IO（stdout 写不出），或**启动失败**（库的 schema 比代码新、路径不可用） |
| `2` | 命令行 / 配置错误（缺 `--identity`、未知身份、`--db` 指向目录）—— 与 `argparse` 的用法错误一致 |
| `130` | Ctrl-C。`finally` 里照样关连接，但**退出码反映"被打断"**而不是"正常收工" |

`1` 与 `2` 分开是有用的：`2` 说明"你命令行写错了"，`1` 说明"这台机器 / 这个库有问题"。
混成一个的话，排障时得先去看日志才知道该改哪边。

## 配流：两件在别的层做不了的事

`serve()` 收的是**文本流**，所以两件与字节有关的事必须在这里做完：

1. **`errors="replace"`**（stdin）：一个坏字节不该让进程带着 traceback 死掉。
   换成 U+FFFD 之后它只是一行解析失败 —— `-32700`，主循环继续（契约 §五.3 边界 #9）。
   不配的话 `readline()` 抛 `UnicodeDecodeError`，它穿过整个 `serve()`，
   于是"一条坏消息"升级成"服务挂了"。
2. **`newline="\\n"`**（stdin/stdout）：Windows 文本模式会把 `\\n` 翻成 `\\r\\n`，
   于是每一帧尾巴都多一个 `\\r`。宽容的客户端看不出来，严格按行切分的客户端会。
   分帧规则是我们自己定的（"一行一条"），那就得由我们保证字节流真的长那样。

`--help` 是唯一允许写 stdout 的东西 —— 它在进入服务模式**之前**就退出了，
不可能与服务期共用同一个 stdout。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from runtime.errors import StillroomRuntimeError

from .identity import IDENTITY_CHOICES
from .stdio import SERVICE_NAME, build_server_context, close_quietly, run, stderr_log

ENV_DB = "STILLROOM_PROTOCOL_DB"
ENV_ARTIFACTS = "STILLROOM_ARTIFACTS_DIR"
ENV_IDENTITY = "STILLROOM_IDENTITY"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def default_db_path() -> Path:
    """缺省库位置。`APPDATA` 不在（非 Windows / 精简环境）时退到 `~/.stillroom`。"""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) / "Stillroom" if appdata else Path.home() / ".stillroom"
    return base / "protocol.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {__package__ or 'mcp_server'}",
        description="Stillroom 协议层（无头 MCP Server，newline-delimited JSON-RPC 2.0 over stdio）",
        epilog="协议见 contracts/mcp-tools.md；本进程的 stdout 只承载协议帧。",
    )
    parser.add_argument("--db", default=None, help=f"协议库路径（默认 {ENV_DB} 或 {default_db_path()}）")
    parser.add_argument(
        "--artifacts", default=None,
        help=f"内容寻址产物目录（默认 {ENV_ARTIFACTS}，都没有则用 <库所在目录>/artifacts）",
    )
    parser.add_argument(
        "--identity", default=None, choices=IDENTITY_CHOICES,
        help=f"本进程的身份（默认取 {ENV_IDENTITY}，**没有缺省值**）；"
             "它决定 creator_context，客户端无法影响",
    )
    return parser


def resolve_settings(args: argparse.Namespace, environ: dict[str, str]) -> tuple[Path, Path | None, str]:
    """参数 > 环境变量 > 默认。返回 `(db_path, artifacts_dir, identity)`。

    **环境变量只在这里读一次**，之后整条链路都拿显式值 ——
    `stdio.build_server_context` 因此不需要认识环境变量，
    "配置从哪来"与"依赖怎么组装"也就不会互相渗透。
    """
    db_raw = args.db or environ.get(ENV_DB) or str(default_db_path())
    artifacts_raw = args.artifacts or environ.get(ENV_ARTIFACTS)
    identity = args.identity or environ.get(ENV_IDENTITY)

    if not identity:
        raise ValueError(
            f"identity is required: pass --identity {{{','.join(IDENTITY_CHOICES)}}} "
            f"or set {ENV_IDENTITY}"
        )

    db_path = Path(db_raw)
    if db_path.is_dir():
        raise ValueError(f"--db points at a directory, not a file: {db_path}")

    artifacts_dir = None
    if artifacts_raw:
        artifacts_dir = Path(artifacts_raw)
        if artifacts_dir.is_file():
            raise ValueError(f"--artifacts points at a file, not a directory: {artifacts_dir}")

    return db_path, artifacts_dir, identity


def configure_streams(log: Any = stderr_log) -> None:
    """把标准流钉成 UTF-8 + LF。见模块说明的两条理由。

    认不出 `reconfigure` 的流（`io.StringIO`、被替换过的 `sys.stdout`）
    直接跳过 —— 本函数只处理"真实的进程标准流"这一种情况，
    在测试里被替换掉时不该假装做了什么。
    """
    for label, stream, errors in (
        ("stdin", sys.stdin, "replace"),
        ("stdout", sys.stdout, "strict"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", newline="\n", errors=errors)
        except (ValueError, OSError) as exc:
            log(f"{SERVICE_NAME}: cannot pin {label} to utf-8/LF: {exc!r}")


def main(argv: Sequence[str] | None = None, *, environ: dict[str, str] | None = None) -> int:
    """进程入口。返回退出码（`__main__` 里 `sys.exit(main())`）。

    `environ` 可注入，这样"环境变量优先级"能在同一进程里被测到，
    不必起子进程去摆环境（`main` 只读这一次）。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    log = stderr_log

    try:
        db_path, artifacts_dir, identity = resolve_settings(args, environ or os.environ)
    except ValueError as exc:
        # `parser.error` 打 usage 到 stderr 并退出 2 —— 配置错误是用法错误。
        parser.error(str(exc))

    if identity == "system":
        log(f"{SERVICE_NAME}: WARNING identity=system —— 本进程可自我授权（T3 + 自动激活）")

    configure_streams(log)

    try:
        ctx = build_server_context(
            db_path=db_path, identity=identity, artifacts_dir=artifacts_dir, log=log
        )
    except (StillroomRuntimeError, OSError) as exc:
        # 库 schema 太新、路径不可用、库文件损坏 —— 都属"这台机器/这个库有问题"。
        log(f"{SERVICE_NAME}: cannot start: {type(exc).__name__}: {exc}")
        return EXIT_FAILED

    try:
        return run(ctx, reader=sys.stdin, writer=sys.stdout, log=log)
    except KeyboardInterrupt:
        log(f"{SERVICE_NAME}: interrupted")
        return EXIT_INTERRUPTED
    finally:
        _close_without_masking_the_exit_code(ctx, log)


def _close_without_masking_the_exit_code(ctx: Any, log: Any) -> None:
    """在 `finally` 里关连接，**绝不让失败冒出去**。

    `close_quietly` 自己已经逐域吞了，这里再兜一层，是因为**`finally` 里冒出的异常会
    顶掉 `return` 的返回值** —— 那条路径会让"stdout 写不出去（退出码 1）"被报成
    "关库失败"，根因就此丢失。两层都要：

    - 内层（`close_quietly` 逐域 catch）：一个域坏了，另一个照样关；
    - 外层（这里）：任何**意外**（`ctx` 缺属性、`log` 本身抛）都不改退出码。

    两层的失败都会记日志，所以"吞"不等于"无声"。
    """
    try:
        close_quietly(ctx, log=log)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：这一层存在的意义就是兜住意外
        log(f"{SERVICE_NAME}: cleanup failed unexpectedly: {exc!r}")


if __name__ == "__main__":  # pragma: no cover —— 子进程冒烟测试走的是下面这一行
    sys.exit(main())
