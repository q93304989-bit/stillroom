"""A1 无头边界：`validator/` `runtime/` `mcp_server/` **不依赖 GUI，也不依赖 `app/`**。

A1 不是一条断言，是五条（见 `docs/P1-实施计划.md` §二）。只写"import 时不加载 PySide6"
会漏掉反方向与"能不能跑"：

| # | 断言 | 本文件的落点 |
|---|---|---|
| A1.1 | 拦截器下 import 三包**及其全部子模块** | `test_the_three_packages_import_with_the_gui_blocked` |
| A1.2 | **反向**源码扫描：源码里不出现 `app` / `PySide6` 的 import | `test_no_headless_source_imports_the_gui` |
| A1.3 | 在拦截状态下**跑通**完整链路 | `test_the_e2e_chain_runs_while_the_gui_is_blocked` |
| A1.4 | 唯一入口：引用标准流的模块恰好是 `{mcp_server.ENTRY_MODULE}` | 实现在 `test_mcp_jsonrpc.py`（与规则 B 同处，不重复写两份） |
| A1.5 | **跨边界同源锁**：GUI adapter 手抄的终态 ≡ `TERMINAL_STATES` | `test_the_gui_adapter_agrees_on_which_states_are_terminal` |

## A1.1 为什么必须起子进程

"import 三包不会加载 PySide6"这句话在本进程里**证不了**：`conftest.py` 与 GUI 测试
早就把 `PySide6` / `app.*` 放进 `sys.modules` 了，此刻再 import 三包，什么都不会发生 ——
测试恒绿，且与实现无关。只有在**干净进程**里、从零 import，才真的在验这件事。
（这与 `test_mcp_stdio.py` 那两个真进程用例是同一个理由：边界要在它成立的介质里验。）

子进程里同时断言 `PySide6` / `app` **确实没进 `sys.modules`** —— 拦截器只在有人
import 时才抛；若某个包用 `importlib.import_module("PySide6")` 之类绕开
`find_spec` 的正常路径，这条还能兜住。

## A1.5 为什么替代"禁止 app/adapters import mcp_server / subprocess"

那条写法是**过宽**的代理指标，会禁掉我们明确留白的设计：`mcp_server/stdio.py`
的模块说明里写着搭配 `app/adapters/protocol_client.py` 的**两条**路 ——
要么起子进程（`python -m mcp_server`，所以有 `--db`/`--identity` 这套入口），
要么在同一进程里复用 `build_server_context(...)` + 把管道喂给 `run(...)`。
禁 `subprocess` / 禁 import `mcp_server` 会把这两条一起禁掉，
于是那条断言迟早要因为"设计本来就是那样"而被删掉。

真正要守的性质是**协议知识只有一份来源**。而这件事今天就已经在漂：
GUI adapter 手抄了 5 个终态名。所以 A1.5 锁的是它 —— 而且是**跨边界**的锁，
GUI 测试与协议层测试都盖不到的那条缝。
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from validator.state_machine import TERMINAL_STATES

from source_scan import imported_modules, package_modules, top_levels

REPO_ROOT = Path(__file__).resolve().parents[1]

HEADLESS_PACKAGES = ("validator", "runtime", "mcp_server")
"""无头层的三个包。依赖方向只能是它仨内部向下，绝不能回头找 `app/`。"""

GUI_ADAPTER = REPO_ROOT / "app" / "adapters" / "protocol_client.py"
"""GUI 侧唯一的协议接触面。**只读它的源码，绝不 import 它** ——
import 它就把 PySide6 拖进无头测试进程了，A1 当场自毁。"""


# ---------------------------------------------------------------------------
# A1.1 干净进程里 import 三包及其全部子模块
# ---------------------------------------------------------------------------

_CHILD_PROGRAM = r'''
import importlib, json, pkgutil, sys

sys.path.insert(0, sys.argv[1])
BANNED = ("PySide6", "app")


class Blocker:
    """命中禁用前缀就**抛** —— 不是记一笔，是让 import 真的失败。

    `find_spec` 返回 None 只是"我不认识它"，下一个 finder 接着找；
    必须抛，才谈得上"挡住"。
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BANNED:
            raise AssertionError(f"headless layer tried to import {fullname!r}")
        return None


sys.meta_path.insert(0, Blocker())

loaded = []
for package in ("validator", "runtime", "mcp_server"):
    module = importlib.import_module(package)
    loaded.append(package)
    for info in pkgutil.walk_packages(module.__path__, prefix=package + "."):
        importlib.import_module(info.name)
        loaded.append(info.name)

present = [name for name in sys.modules if name.split(".")[0] in BANNED]
print(json.dumps({"loaded": sorted(loaded), "banned_present": sorted(present)}))
'''


def _import_everything_headless() -> dict[str, list[str]]:
    """在干净子进程里、带拦截器地 import 三包全部子模块。"""
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"无头层在干净进程里 import 失败 —— 它已经依赖上 GUI 了\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return json.loads(completed.stdout)


def test_the_three_packages_import_with_the_gui_blocked() -> None:
    """A1.1：拦截器一次都没被触发，且三包**全部子模块**都进来了。

    子模块也要：只 import 包的话，`mcp_server` 过了、`mcp_server.stdio` 才炸 ——
    而 `__init__.py` 里根本没有 import `stdio`（刻意的，见那个文件的注释）。
    """
    result = _import_everything_headless()

    assert result["banned_present"] == [], f"禁用模块进了 sys.modules：{result['banned_present']}"
    loaded = result["loaded"]
    assert "mcp_server" in loaded and "runtime" in loaded and "validator" in loaded
    # 每个包至少带进来一个子模块；否则 walk_packages 白跑了，这条就成了"包能 import"而已
    for package in HEADLESS_PACKAGES:
        assert any(name.startswith(package + ".") for name in loaded), package
    assert "mcp_server.stdio" in loaded, "入口层模块没被真的 import 过"
    assert "mcp_server.__main__" in loaded, "进程入口模块没被真的 import 过"


def test_the_import_check_would_catch_a_gui_dependency() -> None:
    """反证：拦截器真的会拦 —— 在同一个子进程机制里试着 import `app` 必须抛。

    没有这条，"拦截器没被触发"可能只是因为它根本没装上。
    """
    program = _CHILD_PROGRAM.replace(
        "    for info in pkgutil.walk_packages",
        '    importlib.import_module("PySide6")\n    for info in pkgutil.walk_packages',
    )
    completed = subprocess.run(
        [sys.executable, "-c", program, str(REPO_ROOT)],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=120,
    )

    assert completed.returncode != 0, "拦截器没拦住 PySide6 —— 它根本没生效"
    assert "PySide6" in (completed.stderr + completed.stdout)


# ---------------------------------------------------------------------------
# A1.2 反向源码扫描
# ---------------------------------------------------------------------------

def test_no_headless_source_imports_the_gui() -> None:
    """三包的**每一行源码**都不许 import `app` / `PySide6`。

    与 A1.1 互为保险：拦截器管"这次真的 import 了"（运行期事实），
    这条管"代码里写了"（静态事实）—— 一句写在没人调用的函数里的
    `import PySide6` 拦不住，但扫得到。
    """
    offenders: dict[str, set[str]] = {}
    for package in HEADLESS_PACKAGES:
        for path in package_modules(REPO_ROOT / package, recursive=True):
            source = path.read_text(encoding="utf-8")
            banned = top_levels(imported_modules(source, package=package)) & {"app", "PySide6"}
            if banned:
                offenders[str(path.relative_to(REPO_ROOT))] = banned

    assert offenders == {}, f"无头层源码里出现了 GUI 依赖：{offenders}"


def test_the_reverse_scan_handles_relative_imports() -> None:
    """反证：相对 import **也要**被解析成绝对名，否则它是一条现成的绕过路径。

    `app/adapters/x.py` 里写 `from ...mcp_server import y`（level 3）——
    从 `app.adapters` 出发正好够到顶层 `mcp_server`。不归一化的话，
    扫描器只看到一个相对名，什么都判不出来。
    """
    source = "from ...mcp_server import dispatch\nfrom .. import ui\n"

    found = imported_modules(source, package="app.adapters")

    assert found == {"mcp_server", "app"}


def test_the_reverse_scan_leaves_clean_source_alone() -> None:
    """也不能误报：`application` 不是 `app`，`PySide6_stub` 不是 `PySide6`。"""
    source = "import application\nfrom PySide6_stub import thing\nfrom . import tools\n"

    found = imported_modules(source, package="mcp_server")

    assert top_levels(found) & {"app", "PySide6"} == set()


# ---------------------------------------------------------------------------
# A1.3 拦截状态下跑通完整链路
# ---------------------------------------------------------------------------

class _Blocker:
    """进程内版的拦截器（A1.1 那个在网络模块里的弟弟）。

    只拦"新 import"：已经在 `sys.modules` 里的一概放行 —— 这一条测的不是
    "库里有没有 PySide6"，而是"**跑这条链路会不会新拉一个 GUI 模块进来**"。
    """

    BANNED = ("PySide6", "app")
    hits: list[str] = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.BANNED:
            self.hits.append(fullname)
            raise AssertionError(f"headless chain imported {fullname!r}")
        return None


def test_the_e2e_chain_runs_while_the_gui_is_blocked(tmp_path: Path) -> None:
    """A1.3：**"能 import" ≠ "能跑"** —— 这条是 A1 与 A9 的交点，也是本阶段的真门槛。

    在拦截器挂着的状态下跑一遍最小链路（create → execute → advance → status），
    并断言拦截器一次都没响。
    """
    from mcp_server import stdio
    from mcp_server.dispatch import make_dispatch
    from mcp_server.mcp import make_method_table
    from mcp_server.tools import build_toolset
    from runtime.stub_kernel import StubKernel

    blocker = _Blocker()
    blocker.hits = []
    before = set(sys.modules)
    sys.meta_path.insert(0, blocker)
    try:
        ctx = stdio.build_server_context(
            db_path=tmp_path / "protocol.db", identity="agent", log=lambda _m: None
        )
        try:
            dispatch = make_dispatch(make_method_table(build_toolset(ctx), log=lambda _m: None))
            definition = json.loads(
                (REPO_ROOT / "tests" / "fixtures" / "workflow" / "valid_minimal.json")
                .read_text(encoding="utf-8")
            )
            _ok(dispatch, "create_workflow", {"workflow": definition, "activate": True})
            started = _ok(dispatch, "execute_workflow", {
                "workflow_id": definition["workflow_id"], "version": 1,
                "request_id": "a1-chain", "input": {},
            })
            execution_id = started["execution_id"]
            kernel = StubKernel(ctx.repo, execution_id)
            assert kernel.run_to_completion() == "COMPLETED"
            status = _ok(dispatch, "get_execution_status", {"execution_id": execution_id})
        finally:
            stdio.close_quietly(ctx, log=lambda _m: None)
    finally:
        sys.meta_path.remove(blocker)

    assert blocker.hits == [], f"链路拉进了 GUI 模块：{blocker.hits}"
    newly = {name for name in set(sys.modules) - before if name.split(".")[0] in _Blocker.BANNED}
    assert newly == set(), f"链路新加载了：{newly}"
    assert status["status"] == "COMPLETED"


def _ok(dispatch, tool: str, arguments: dict) -> dict:
    """发一次 `tools/call`，断言它成功，返回业务 `data`。"""
    from mcp_server.jsonrpc import JsonRpcError

    outcome = dispatch("tools/call", {"name": tool, "arguments": arguments})
    assert not isinstance(outcome, JsonRpcError), outcome
    assert outcome["isError"] is False, outcome
    body = json.loads(outcome["content"][0]["text"])
    assert body["ok"] is True, body
    return body["data"]


# ---------------------------------------------------------------------------
# A1.5 跨边界同源锁：GUI adapter 手抄的终态
# ---------------------------------------------------------------------------
#
# **为什么是「两条方向」而不是一条「集合相等」。**
#
# 一开始写的是 `adapter_values == TERMINAL_STATES`。它今天能过（两侧都是那五个，
# 逐字相同 —— **今天没有漂**），但它把两件强度不同的性质焊死在了一条断言里：
#
#   方向 A（GUI ⊆ 协议）：界面认定的每个终态都必须是协议层**真正**的终态。
#                        这是**不变式**，任何情况下都不该红。
#   方向 B（协议 ⊆ GUI）：协议层的每个终态界面都认。这是**今天的覆盖度**，
#                        它会随界面的设计选择变化。
#
# 焊在一起之后，方向 B 的一次合法变化（界面决定把 TIMEOUT 并进笼统的"失败"）
# 会让整条断言变红，而报错文案说不出"是哪个方向错了"。**一条会喊狼来了的断言
# 最后会被放宽或删掉** —— 而删的时候方向 A 那条真不变式会跟着一起没。
#
# 所以拆开：方向 A 是主锁，方向 B 单列并写明"红了该改哪里"。

def _adapter_status_literals() -> dict[str, str]:
    """`app/adapters/protocol_client.py` 里 `STATUS_X = "X"` 那一组。

    用 AST 读源码，**不 import** —— 详见 `GUI_ADAPTER` 的注释。
    """
    tree = ast.parse(GUI_ADAPTER.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("STATUS_"):
                if isinstance(node.value.value, str):
                    found[target.id] = node.value.value
    return found


def _adapter_frozenset_names(name: str) -> set[str]:
    """`TERMINAL_STATUSES = frozenset({STATUS_A, …})` 里那一组**变量名**。

    光比字面量不够：`TERMINAL_STATUSES` 可以少收一个常量而常量还在 ——
    那是"改了一处忘了另一处"，两边都得锁。
    """
    tree = ast.parse(GUI_ADAPTER.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Set):
                return {e.id for e in sub.elts if isinstance(e, ast.Name)}
    raise AssertionError(f"{GUI_ADAPTER.name} 里找不到 {name}")


def test_the_gui_side_never_treats_a_non_terminal_state_as_terminal() -> None:
    """**方向 A（主锁，永不该红）**：GUI 认定的终态 ⊆ 协议层的 `TERMINAL_STATES`。

    抓两类真 bug，两类的后果都具体：

    - **值抄错**（`STATUS_TIMEOUT = "TimedOut"`）：界面永远匹配不上协议层报的
      `"TIMEOUT"`，这一轮会停在"正在回复…"上不结束。
    - **把中间态当终态**（把 `RUNNING` / `ABORT_PENDING` 收进 `TERMINAL_STATUSES`）：
      界面会在执行**还没结束**时判定"已结束"，而 `ABORT_PENDING` 恰恰是
      "中止请求已收到、当前步骤还在跑" —— 判成终态就是提前收工。

    这条不涉及"界面抄了几个"，只涉及"界面抄的每一个对不对"，
    所以它**不会**因为界面的设计选择而红。
    """
    gui_terminals = set(_adapter_status_literals().values())
    protocol_terminals = {state.value for state in TERMINAL_STATES}

    illegal = gui_terminals - protocol_terminals
    assert illegal == set(), (
        f"GUI adapter 把 {sorted(illegal)} 当成终态，但协议层里它们不是终态"
        f"（协议层终态：{sorted(protocol_terminals)}）—— "
        "值抄错会让界面匹配不上、把中间态当终态会让界面提前收工"
    )


def test_the_gui_side_still_recognises_every_terminal_state() -> None:
    """**方向 B（覆盖度）**：协议层的每个终态，界面这边都有对应的处理。

    今天成立（5/5）。它是**另一条性质**，与方向 A 分工：

    - 方向 A 抓"抄错了"，对"少抄了"**无感**（少抄了仍然是子集）；
    - 方向 B 抓"少抄了"，对"抄错了"**无感**（多出一个错的键不影响包含关系）。

    ## 红了怎么办（这条与方向 A 的处置**完全不同**）

    `app/ui/assistant_bridge.py` 是用 `.get(status, 兜底)` 查表的，
    所以漏一个终态**不会崩** —— 只会退化成一个笼统的"失败了"提示，
    代价是**排障时看不出是超时还是额度用尽**。因此：

    - 若界面是**有意**降级（决定不再单独区分某个终态）→ 改**这条**的期望，
      并在 `app/adapters/protocol_client.py` 里把理由写成注释；
    - 若是漏了 → 补上那个终态。

    **不要**为了让它变绿去动方向 A 那条 —— 它们锁的不是同一件事。
    """
    gui_terminals = set(_adapter_status_literals().values())
    protocol_terminals = {state.value for state in TERMINAL_STATES}

    missing = protocol_terminals - gui_terminals
    assert missing == set(), (
        f"协议层是终态、界面却不认识：{sorted(missing)} —— "
        "界面会用兜底文案糊过去（不会崩），但那一轮到底为什么结束就看不出来了"
    )


def test_the_frozenset_collects_exactly_the_literals_it_declares() -> None:
    """GUI 侧内部自洽：`TERMINAL_STATUSES` 收的就是那几个 `STATUS_*` 常量。

    （同侧的一致性问题。少收一个常量，那个终态在界面上的判断会与自己的常量定义不符 ——
    "改了一处忘了另一处"的经典形态。）
    """
    literals = _adapter_status_literals()

    assert _adapter_frozenset_names("TERMINAL_STATUSES") == set(literals), (
        "TERMINAL_STATUSES 收的常量与它自己声明的 STATUS_* 不一致"
    )


def test_the_terminal_status_count_is_a_tripwire() -> None:
    """字面量绊线：契约明写"终态固定五个"（`execution-state-machine.md` §一）。

    加第六个终态时这条会红 —— 那是**该红**的：客户端、GUI、文档都要跟着动。
    """
    assert len(_adapter_status_literals()) == 5
    assert len(TERMINAL_STATES) == 5


def test_the_two_directions_each_catch_what_the_other_misses() -> None:
    """反证：**两条方向分工**，不是互为保险 —— 把这条钉住，防止有人再合并回"相等"。

    下面每一行都是"某个方向对这个变异是**无感**的"的证明。
    """
    protocol = {state.value for state in TERMINAL_STATES}

    # 方向 A 抓得住：值抄错、把中间态当终态
    assert not {"TimedOut", "COMPLETED"} <= protocol, "值抄错居然通过了方向 A"
    assert not {"RUNNING", "COMPLETED"} <= protocol, "中间态居然通过了方向 A"

    # 方向 A 对"少抄"无感 —— 少了也仍然是子集。这是**分工**，不是缺陷
    assert {"COMPLETED"} <= protocol, "方向 A 本就不该管少抄"

    # 方向 B 抓得住：少抄
    assert not protocol <= {"COMPLETED", "ABORTED"}, "少抄居然通过了方向 B"

    # 方向 B 对"多抄一个错的/中间态"无感 —— 多出一个键不影响包含关系
    assert protocol <= protocol | {"RUNNING"}, "方向 B 本就不该管多抄"


@pytest.mark.parametrize("package", HEADLESS_PACKAGES)
def test_each_headless_package_is_importable_on_its_own(package: str) -> None:
    """每个包单独 import 都要成立（`sys.path` 就绪时）。

    挡的是"靠 `import mcp_server` 顺手把 `runtime` 拖进来"这种隐性依赖：
    单独 import 一验，包之间的依赖方向就得真的向下。
    """
    assert importlib.import_module(package) is not None
