"""AST 源码扫描器：把"纪律"从散文变成可执行断言。

**一份实现，多处引用。** 规则散在各测试文件里各写一遍的后果很具体：
改一次规则要改 N 个地方，漏一个就留下一条**在守旧规则**的测试 ——
它照绿，但它已经不代表任何人的意图了。

| 函数 | 规则 |
|---|---|
| `stdout_write_offenders` | A：不**写** stdout（含裸 `print`） |
| `process_stream_references` | B：不**引用** `sys.stdin` / `sys.stdout` |
| `imported_modules` | 依赖方向：不许 import 谁 |

判据全部基于 **AST**，不是子串匹配。理由不是洁癖：解释这些规则的 docstring 里
必然会写出违禁词（"日志走 `sys.stderr`，别 `print` 到 `sys.stdout`"），
子串匹配会把散文当代码 —— 一条会误报的断言，最后只会被注释掉。

三个函数都收**源码文本**（不是路径）：同一份实现既要能用在实际文件上，
也要能在 `tmp_path` 里造的反例上跑。反证是必须的 ——
"`offenders == []`"在扫描器永远返回空时同样成立。
"""

from __future__ import annotations

import ast
from pathlib import Path

# ---- 规则 A：不写 stdout ---------------------------------------------------

def stdout_write_offenders(source: str) -> list[str]:
    """**不向 stdout 写内容。** 对包内**所有**模块成立。

    判据是"写"，不是"碰 `sys`" —— `jsonrpc.py` 的 `_default_log` 兜底就是要写
    `sys.stderr`，而 stderr 从来没被禁止（铁律只针对 stdout，它是协议流）。

    `sys.stdout.reconfigure(...)` **不算写**：它调的是流配置，不是往流里塞字节。
    这条区分是刻意的 —— 入口层的职责之一就是配流。
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_stdout_write(node):
            found.append("sys.stdout.write")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            file_kw = next((kw for kw in node.keywords if kw.arg == "file"), None)
            if file_kw is None:
                found.append("print(...) without file= (defaults to stdout)")
            elif not (isinstance(file_kw.value, ast.Attribute) and file_kw.value.attr == "stderr"):
                found.append("print(file=...) where file is not stderr")
    return found


def _is_stdout_write(node: ast.Call) -> bool:
    """`sys.stdout.write(...)` / `sys.stdout.writelines(...)`。"""
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"write", "writelines"}
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "sys"
        and node.func.value.attr == "stdout"
    )


# ---- 规则 B：不引用标准流 --------------------------------------------------

def process_stream_references(source: str) -> list[str]:
    """**不引用 `sys.stdin` / `sys.stdout`（也不碰 `os.stdin`/`os.stdout`）。**

    比规则 A 更严：连"读一下"都不行。它守的是**注入纪律** ——
    一层的依赖要么是参数、要么是它自己造的，不能顺手去摸全局。

    为什么必须单独存在：只有规则 A 的话，"`sys.stdin.readline()` 自己读"是合规的
    （它不是"写"），但那样 `serve()` 的 `reader` 注入就形同虚设，
    测试再也没法用 `io.StringIO` 顶掉它。

    **唯一被放行的模块是 `mcp_server.ENTRY_MODULE`** —— 就是它把这两个流
    交给 `run(reader=…, writer=…)`。豁免只有一处定义，见那个常量的 docstring。

    注意 `sys.stdout.reconfigure(...)` **会**被这条判为引用（它确实引用了）。
    这是对的，也是豁免的**第二个**理由：配流（`errors="replace"`、`newline="\\n"`）
    本来就是入口层的职责，而它必然要写出 `sys.stdout` ——
    规则 A 放行它（不是"写"），规则 B 拦下所有非入口模块。
    """
    return [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"sys", "os"}
        and node.attr in {"stdout", "stdin"}
    ]


# ---- 依赖方向 --------------------------------------------------------------

def imported_modules(source: str, *, package: str = "") -> set[str]:
    """源码 import 到的**绝对**模块名集合。

    `package` 是这个源码所属的包（如 `"app.adapters"`），用来把
    `from ...mcp_server import x` 这类**相对** import 归一化 —— 不归一化的话
    "用相对 import 绕过检查"就是一条现成的漏洞（level 3 从 `app.adapters`
    出发正好够到顶层 `mcp_server`）。
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                resolved = _resolve_relative(package, node.level, module)
                if resolved:
                    found.add(resolved)
            elif module:
                found.add(module)
    return found


def top_levels(names: set[str]) -> set[str]:
    """取顶层包名：`mcp_server.jsonrpc` → `mcp_server`。"""
    return {name.split(".")[0] for name in names if name}


def _resolve_relative(package: str, level: int, module: str) -> str:
    """把 `from ...x import y` 解析成绝对模块名。

    `level` 1 指当前包，每多一个点少一层。`app.adapters` 下 level=3 → 顶层，
    于是 `from ...mcp_server import x` 解析成 `mcp_server`（**不是**被忽略的相对名）。
    """
    parts = [part for part in package.split(".") if part]
    keep = len(parts) - (level - 1)
    if keep < 0:
        return module                      # 越出顶层：包结构本身就不合法，原样记
    return ".".join([*parts[:keep], *([module] if module else [])])


# ---- 包内模块枚举 ----------------------------------------------------------

def package_modules(package_dir: Path, *, recursive: bool = False) -> list[Path]:
    """包内的 `.py` 文件（默认只看顶层，与 `glob("*.py")` 同义）。

    用 `sorted` 保证顺序稳定：收集型断言失败时，报告里的清单不该每次换个顺序，
    否则「这次多了哪个」要人眼比对两次输出。
    """
    pattern = "**/*.py" if recursive else "*.py"
    return sorted(package_dir.glob(pattern))


def source_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


__all__ = [
    "imported_modules",
    "package_modules",
    "process_stream_references",
    "source_of",
    "stdout_write_offenders",
    "top_levels",
]
