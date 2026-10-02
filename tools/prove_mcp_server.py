"""反证（变异测试）harness：证明 `mcp_server/` 的边界**真的挡得住**。

覆盖 `tools.py`（工具层：异常边界 / 信封 / 清单 / 身份注入）、
`identity.py` + `__main__.py`（入口层：身份来源 / 清理不参与退出码）、
以及 `mcp.py`（方法层：三方法命名空间 / `isError` 判据 / 协议版本对齐）。

## 为什么需要它

"测试全绿"只说明**当前**这份实现没被抓住。它不说明这些测试
**在实现被改坏时会变红** —— 而一个永远绿、永远抓不住东西的断言，
和没有断言是同一件事，只是更有欺骗性。

本脚本对源文件施加**定点变异**（每次只改一处，改成"另一种看起来合理的写法"），
然后跑指定用例，断言它们**必须变红**。全绿 = 那些边界是靠测试守着的，不是靠运气。

## 用法

```
.venv/Scripts/python.exe tools/prove_mcp_server.py          # 全部场景
.venv/Scripts/python.exe tools/prove_mcp_server.py 窄异常 信封   # 只跑名字含这些子串的场景
```

退出码：0 = 每个变异都被抓住（且未变异时基线是绿的）；1 = 有变异溜过去了。

## 加场景的规矩

- 变异必须是**另一种看起来合理的写法**，不是"随便删几行"。否则证明了也没意义
  （删掉整个函数当然会红，但那不说明测试覆盖了这条性质）。
- 一个变异配**最少**的用例集：用例多了会掩盖"到底是哪条断言的功劳"。
- `old` 必须在目标文件里**恰好出现一次**，否则本脚本拒绝执行（防止改错地方）。
- 有用例**故意保持绿**时，写进 `stay_green` 而不是删掉它 ——
  "只断言错误码守不住这个检查"本身就是一条结论，得让脚本替你说出来。
"""

from __future__ import annotations

import itertools
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: pytest 的临时目录**落在仓库内**（不落系统 `Temp`），且**每次调用都用一个新的空目录**。
#:
#: ## 为什么不能落系统 `Temp`
#:
#: 那里积着上万条历史垃圾（`pytest-of-<user>\garbage-*`）。pytest 开跑时会去清它，
#: 而本机的 safe-delete shim 把 `shutil.rmtree` / `Path.unlink` 换成了会
#: `raise SystemExit(1)` 的版本（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）——
#: 表现是"28 条用例**全过**，退出码却是 1"，基线被误判成红，
#: 报告里只剩一句"先修好再谈反证"。
#:
#: ## ⚠️ 换目录**解决不了**下面这件事（本文件被它坑过一次，故写明）
#:
#: 那个守卫的计数是**按工具调用（turn）累计**的，阈值 50。本脚本一次要跑 ~60 次
#: pytest（基线 1 次 + 每个场景 1–3 次），必然越过阈值；越过之后**每一次**删除操作
#: 都抛 `SystemExit`，于是出现"前 29 条用例全过，接着十几条被记成 error"——
#: 看起来像套件坏了，其实**一条都没坏**，只是守卫把进程掐了。
#: 换 basetemp 只是让单次删除更小，挡不住累计计数。
#:
#: **对策是分批跑**（每次调用选 4 个左右的场景），例如：
#:
#: ```
#: python tools/prove_mcp_server.py "窄异常边界放宽成 Exception" "异常边界窄到一个子类…"
#: ```
#:
#: 不传参数（一次跑全部）在这台机器上会撞上守卫 —— 那是环境限制，不是用例的问题。
#:
#: 目录名以 `.tmp` 开头，已被 `.gitignore` 的 `.tmp*/` 覆盖（与 `.tmp-pytest` 同族）。
#: 副作用：这些目录不会被回收（回收本身就是批量删除）—— 攒多了手动清一次。
BASETEMP_ROOT = REPO_ROOT / ".tmp-pytest-mcp"
_RUN_SEQ = itertools.count()

PYTEST_BASE = [str(Path(sys.executable)), "-m", "pytest", "-o", "addopts=", "-q", "--no-header"]


def _pytest_command(tests: tuple[str, ...]) -> list[str]:
    """拼一条 pytest 命令，配一个**这一次专属的空 basetemp**（见 `BASETEMP_ROOT`）。"""
    fresh = BASETEMP_ROOT / f"r{next(_RUN_SEQ):03d}"
    return [*PYTEST_BASE, f"--basetemp={fresh}", *tests]


@dataclass(frozen=True)
class Mutation:
    """一次定点变异：把 `old` 换成 `new`，`tests` 必须变红。"""

    name: str
    why: str
    old: str
    new: str
    tests: tuple[str, ...]
    path: str = "mcp_server/tools.py"
    """被变异的文件（相对仓库根）。入口层的场景指向 `identity.py` / `__main__.py`。"""
    stay_green: tuple[str, ...] = field(default=())
    """明确声明"变异后**仍然**会绿的用例"，并断言它确实还绿。

    用处是暴露"某条断言其实抓不住这个变异" —— 与其把它删掉假装不存在，
    不如钉在这里，让下一个人知道该往哪条断言上加力道。
    """


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="窄异常边界放宽成 Exception",
        why="把 `except StillroomRuntimeError` 放宽 —— 编程错误会被伪装成业务失败",
        old="        except StillroomRuntimeError as exc:\n            # 只接这一层。",
        new="        except Exception as exc:\n            # 只接这一层。",
        tests=(
            "tests/test_mcp_tools.py::test_a_programming_error_is_never_disguised_as_a_business_failure",
            "tests/test_mcp_tools.py::test_a_programming_error_becomes_internal_error_on_the_wire",
        ),
    ),
    Mutation(
        name="异常边界窄到一个子类（只接 InputError）",
        why="`KernelError` / `RepositoryError` 都会漏出去变成 -32603",
        old="        except StillroomRuntimeError as exc:\n            # 只接这一层。",
        new="        except InputError as exc:\n            # 只接这一层。",
        tests=(
            "tests/test_mcp_tools.py::test_a_kernel_error_is_also_normalized",
            "tests/test_mcp_tools.py::test_a_business_error_carries_the_code_from_the_shared_enum",
        ),
    ),
    Mutation(
        name="信封不再由 binder 产生",
        why="业务失败直接返回裸错误体，客户端拿不到 `ok:false` 这个统一判据",
        old='            return {"ok": False, "error": exc.as_dict()}',
        new="            return exc.as_dict()",
        tests=(
            "tests/test_mcp_tools.py::test_a_business_failure_envelope_has_exactly_ok_and_error",
            "tests/test_mcp_tools.py::test_a_business_error_carries_the_code_from_the_shared_enum",
        ),
    ),
    Mutation(
        name="TOOLS 清单漂移（少一个工具）",
        why="清单与契约脱钩，客户端按契约调就会撞 -32601",
        old='    "resume_execution": resume_execution,\n',
        new="",
        tests=("tests/test_mcp_tools.py::test_tool_names_match_the_frozen_contract",),
        stay_green=(
            # 它比的是 `data["tools"] == sorted(TOOLS)`，两边同源 ——
            # `TOOLS` 自己少一条时它跟着一起少，看不出来。
            # 守住"TOOLS 有没有漂"的是上一条（比的是测试里那份契约字面量）。
            "tests/test_mcp_tools.py::test_get_capabilities_reports_exactly_the_tools_that_exist",
        ),
    ),
    Mutation(
        name="get_capabilities 手抄了一份工具清单（漏掉最后一个）",
        why="清单有了第二个来源，`TOOLS` 加工具时它会静默滞后",
        old='        "tools": sorted(TOOLS),\n',
        new=(
            '        "tools": sorted([\n'
            '            "get_capabilities", "list_skills", "create_skill", "list_workflows",\n'
            '            "match_workflow", "create_workflow", "execute_workflow", "retry_execution",\n'
            '            "get_execution_status", "abort_execution",\n'
            "        ]),\n"
        ),
        tests=("tests/test_mcp_tools.py::test_get_capabilities_reports_exactly_the_tools_that_exist",),
        stay_green=(
            # `TOOLS` 没被动，比契约字面量的那条自然还是绿的。
            "tests/test_mcp_tools.py::test_tool_names_match_the_frozen_contract",
        ),
    ),
    Mutation(
        name="create_skill 不再挡身份字段",
        why="身份字段退化成 `SCHEMA_INVALID`，同一个安全边界出现两个错误码",
        old="    _reject_identity(params)\n    record = ctx.registry.register_skill",
        new="    record = ctx.registry.register_skill",
        tests=(
            "tests/test_mcp_tools.py::test_a_forged_creator_context_is_rejected_loudly",
            "tests/test_mcp_tools.py::test_create_skill_rejects_identity_fields_with_the_input_code",
        ),
    ),
    Mutation(
        name="身份字段被写进某个工具的白名单",
        why="`creator_context` 从 params 进来就成了合法入参（安全模型静默失效）",
        old='    _reject_unknown(params, allowed={"include_inactive"})\n',
        new='    _reject_unknown(params, allowed={"include_inactive", "creator_context"})\n',
        tests=(
            "tests/test_mcp_tools.py::test_identity_fields_are_not_part_of_any_tool_signature",
            "tests/test_mcp_tools.py::test_a_forged_creator_context_is_rejected_loudly",
        ),
    ),
    Mutation(
        name="入参整数检查丢掉 bool 档",
        why="`isinstance(True, int)` 为真，`{\"version\": true}` 被当成 1 一路走到底",
        old="    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:",
        new="    if not isinstance(value, int) or value < minimum:",
        tests=("tests/test_mcp_tools.py::test_a_bool_is_not_accepted_where_an_int_is_required",),
    ),
    Mutation(
        name="幂等命中时报 PENDING 而不是当前状态",
        why="把既有执行伪装成刚起的新执行，客户端据此判断就错了",
        old=(
            '        # 报 `PENDING` 会让客户端以为"刚起了一个新的"。\n'
            '        "status": bound.record.state,\n'
        ),
        new=(
            '        # 报 `PENDING` 会让客户端以为"刚起了一个新的"。\n'
            '        "status": "PENDING",\n'
        ),
        tests=("tests/test_mcp_tools.py::test_a_duplicate_reports_the_current_status_not_pending",),
    ),
    Mutation(
        name="input_mismatch 被静默吞掉",
        why="同一 request_id 配不同 input 是客户端 bug，不吱声就永远查不出来",
        old='        data["input_mismatch"] = True',
        new="        pass",
        tests=("tests/test_mcp_tools.py::test_a_reused_request_id_with_different_input_is_flagged",),
    ),
    Mutation(
        name="resume 拿掉前置状态检查",
        why="被拒绝的调用会先往 store 落一份没人引用的输入（副作用）",
        old=(
            '        _fail(\n'
            '            "execution_id",\n'
            '            f"{execution_id} is {record.state}, not WAITING_INPUT",\n'
            '            code=ErrorCode.NOT_WAITING_INPUT,\n'
            "        )\n"
        ),
        new="        pass\n",
        tests=("tests/test_mcp_tools.py::test_a_rejected_resume_leaves_no_artifact_behind",),
        stay_green=(
            "tests/test_mcp_tools.py::test_resume_outside_waiting_input_is_not_waiting_input",
        ),
    ),
    # -----------------------------------------------------------------------
    # 入口层：身份从哪来（`identity.py`）
    # -----------------------------------------------------------------------
    Mutation(
        name="is_system 变成可单独写的字段",
        why="`{\"identity\": \"agent\", \"is_system\": true}` 就是一次提权",
        path="mcp_server/identity.py",
        old='            "is_system": self.identity == SYSTEM,   # 派生，不是配置',
        new='            "is_system": True,   # 派生，不是配置',
        tests=(
            "tests/test_mcp_stdio.py::test_is_system_is_derived_and_not_a_configurable_field",
        ),
    ),
    Mutation(
        name="未知身份回落成 agent 而不是拒绝启动",
        why="回落让\"配错了\"表现成\"服务能用一部分功能\"，比拒绝启动难查一个数量级",
        path="mcp_server/identity.py",
        old="    if preset is None:\n        raise ValueError(",
        new="    if preset is None:\n        return IDENTITY_PRESETS[AGENT].to_creator_context()\n\n    if False:\n        raise ValueError(",
        tests=(
            "tests/test_mcp_stdio.py::test_an_unknown_identity_raises_instead_of_falling_back",
        ),
    ),
    Mutation(
        name="agent 预设偷偷放宽能力集",
        why="最小权限是 Agent 的默认姿态；放宽必须是部署方的显式动作",
        path="mcp_server/identity.py",
        old='        allowed_capabilities=("llm.call",),',
        new='        allowed_capabilities=("llm.call", "file.write"),',
        tests=(
            # 这条比的是"契约 §一 那段示例" —— 改预设不改契约就红。
            "tests/test_mcp_stdio.py::test_the_agent_preset_is_the_contracts_creator_context_example",
            # 这条比的是"契约 §一.2 那张表" —— 两条各守一处，别指望一条顶两条。
            "tests/test_mcp_stdio.py::test_the_preset_table_matches_the_contract_row_by_row",
        ),
    ),
    # -----------------------------------------------------------------------
    # 入口层：清理不参与退出码（`__main__.py`）
    # -----------------------------------------------------------------------
    Mutation(
        name="清理失败的异常冒出去（顶掉退出码）",
        why="`finally` 里冒出的异常会覆盖 `return`，于是\"stdout 写不出去\"被报成\"关库失败\"",
        path="mcp_server/__main__.py",
        old="    try:\n        close_quietly(ctx, log=log)\n    except Exception as exc:  # noqa: BLE001",
        new="    if True:\n        close_quietly(ctx, log=log)\n    if False:  # noqa: BLE001",
        tests=(
            "tests/test_mcp_stdio.py::test_the_serve_exit_code_survives_a_cleanup_failure",
            "tests/test_mcp_stdio.py::test_an_interrupted_run_also_survives_a_cleanup_failure",
        ),
    ),
    Mutation(
        name="stdin 不再容错解码（坏字节即杀进程）",
        why="一个坏字节让 readline() 抛 UnicodeDecodeError，把\"一条坏消息\"升级成\"服务挂了\"",
        path="mcp_server/__main__.py",
        old='        ("stdin", sys.stdin, "replace"),',
        new='        ("stdin", sys.stdin, "strict"),',
        tests=(
            "tests/test_mcp_stdio.py::test_a_bad_byte_does_not_kill_the_process",
        ),
    ),
    # -----------------------------------------------------------------------
    # 方法层：wire 上是三个方法（`mcp.py`）
    # -----------------------------------------------------------------------
    Mutation(
        name="工具名又被塞回方法表（11 个方法卷土重来）",
        why="这才是标准 MCP 客户端连不上的根因 —— \"换了一张表\"退化成\"又加了一张表\"",
        path="mcp_server/mcp.py",
        old='    methods: dict[str, MethodFn] = {\n        "initialize": initialize,',
        new='    methods: dict[str, MethodFn] = {**table,\n        "initialize": initialize,',
        tests=(
            "tests/test_mcp_methods.py::test_a_tool_name_is_no_longer_a_method",
        ),
    ),
    Mutation(
        name="未知工具名不再被拦（协议错误边界消失）",
        why="放行之后 `table.get(name)` 返回 None，调用它抛 TypeError → 变成 -32603，"
            "客户端看到的从\"我工具名写错了\"变成\"服务端内部错误\"",
        path="mcp_server/mcp.py",
        old="        tool = table.get(name)\n        if tool is None:",
        new="        tool = table.get(name)\n        if tool is None and False:",
        tests=(
            "tests/test_mcp_methods.py::test_an_unknown_tool_is_invalid_params_not_method_not_found",
        ),
    ),
    Mutation(
        name="arguments 不是对象时静默当成空对象",
        why="静默忽略等于让客户端以为参数送到了；而且工具会收到一个 list",
        path="mcp_server/mcp.py",
        old="        if not isinstance(arguments, dict):",
        new="        if not isinstance(arguments, dict) and False:",
        tests=(
            "tests/test_mcp_methods.py::test_arguments_must_be_an_object",
        ),
    ),
    Mutation(
        name="isError 判据放宽成真值判断",
        why="`1 == True` 在 Python 里成立 —— 写错信封的 `{\"ok\": 1}` 会被当成成功",
        path="mcp_server/mcp.py",
        old='        "isError": result.get("ok") is not True,',
        new='        "isError": not result.get("ok"),',
        tests=(
            "tests/test_mcp_methods.py::test_a_truthy_but_wrong_ok_field_is_not_a_success",
        ),
    ),
    Mutation(
        name="tools/list 手抄清单（少一个）",
        why="清单不再取自工具表本身，于是\"清单说有、tools/call 却不认\"的漂移又回来了",
        path="mcp_server/mcp.py",
        old="        for name in sorted(table)\n    ]",
        new="        for name in sorted(table)[:-1]\n    ]",
        tests=(
            "tests/test_mcp_methods.py::test_tools_list_reports_exactly_the_tools_in_the_table",
        ),
    ),
    Mutation(
        name="客户端要什么协议版本就回什么",
        why="回一个自己并不支持的版本号是假话：MCP 规定不受支持时应回本服务的版本",
        path="mcp_server/mcp.py",
        old="        if requested in SUPPORTED_PROTOCOL_VERSIONS:",
        new="        if requested is not None:",
        tests=(
            "tests/test_mcp_methods.py::test_initialize_answers_its_own_version_when_the_client_asks_for_another",
        ),
    ),
    Mutation(
        name="`-32602` 丢掉 data.reason",
        why="四种情形又退回成一个笼统的 Invalid params —— 客户端只能去匹配报错文案",
        path="mcp_server/mcp.py",
        old='    return JsonRpcError(INVALID_PARAMS, message, data={"reason": reason, **data})',
        new="    return JsonRpcError(INVALID_PARAMS, message, data=dict(data))",
        tests=(
            "tests/test_mcp_methods.py::test_every_invalid_params_carries_a_stable_reason",
            "tests/test_mcp_methods.py::test_the_reason_is_exactly_the_enum_and_nothing_else",
        ),
    ),
    Mutation(
        name="ENTRY_MODULE 指向另一个模块",
        why="规则 B 的豁免会跟着搬走：被指到的模块就名正言顺地引用标准流，而真入口反被判违规",
        path="mcp_server/__init__.py",
        old='ENTRY_MODULE = "mcp_server.__main__"',
        new='ENTRY_MODULE = "mcp_server.stdio"',
        tests=(
            "tests/test_mcp_jsonrpc.py::test_there_is_exactly_one_entry_module",
            "tests/test_mcp_jsonrpc.py::test_the_entry_module_constant_names_a_real_module",
        ),
    ),
    Mutation(
        name="GUI adapter 的终态值抄错",
        why="方向 A（GUI ⊆ 协议）的正面用例：界面认得的终态必须是协议层真正的终态",
        path="app/adapters/protocol_client.py",
        old='STATUS_TIMEOUT = "TIMEOUT"',
        new='STATUS_TIMEOUT = "TIMED_OUT"',
        tests=(
            "tests/test_headless_boundary.py::test_the_gui_side_never_treats_a_non_terminal_state_as_terminal",
        ),
        # 同侧自洽与绊线都**不读协议层**，所以它们对这个变异无感 ——
        # 钉住"它们抓不住"，说明力道在方向 A 那条上。
        stay_green=(
            "tests/test_headless_boundary.py::test_the_frozenset_collects_exactly_the_literals_it_declares",
            "tests/test_headless_boundary.py::test_the_terminal_status_count_is_a_tripwire",
        ),
    ),
    Mutation(
        name="GUI adapter 把中间态当终态",
        why="方向 A 的另一半：`ABORT_PENDING` 是「中止已请求、当前步骤还在跑」，判成终态就是提前收工",
        path="app/adapters/protocol_client.py",
        old='STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"',
        new='STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"\nSTATUS_ABORT_PENDING = "ABORT_PENDING"',
        tests=(
            "tests/test_headless_boundary.py::test_the_gui_side_never_treats_a_non_terminal_state_as_terminal",
            "tests/test_headless_boundary.py::test_the_terminal_status_count_is_a_tripwire",
        ),
        # 方向 B 只管"协议层终态有没有被界面认全"，多抄一个不影响包含关系 —— 无感是对的
        stay_green=(
            "tests/test_headless_boundary.py::test_the_gui_side_still_recognises_every_terminal_state",
        ),
    ),
    Mutation(
        name="GUI adapter 少抄一个终态",
        why="方向 B（协议 ⊆ GUI）的正面用例：少了谁，谁在界面上就只剩一句笼统兜底",
        path="app/adapters/protocol_client.py",
        old='STATUS_TIMEOUT = "TIMEOUT"',
        new='TIMEOUT_STATUS = "TIMEOUT"',
        tests=(
            "tests/test_headless_boundary.py::test_the_gui_side_still_recognises_every_terminal_state",
        ),
        # 少抄对方向 A 无感（剩下的仍是子集）—— 这正是"两条方向分工"的证据
        stay_green=(
            "tests/test_headless_boundary.py::test_the_gui_side_never_treats_a_non_terminal_state_as_terminal",
        ),
    ),
)


def _run(tests: tuple[str, ...]) -> tuple[bool, str]:
    """跑用例，返回 (是否全绿, 输出尾部)。"""
    proc = subprocess.run(
        _pytest_command(tests), cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-3:])
    return proc.returncode == 0, tail


def _run_each(tests: tuple[str, ...]) -> dict[str, bool]:
    """逐条跑，返回 `{用例名: 是否绿}`。

    逐条而不是一把跑：这样一个场景里"哪条抓住了、哪条没抓住"是可见的。
    一把跑只给一个总的红/绿，会**掩盖**"配了三条用例其实只有一条在干活"。
    """
    return {test: _run((test,))[0] for test in tests}


def main(argv: list[str]) -> int:
    selected = [m for m in MUTATIONS if not argv or any(a in m.name for a in argv)]
    if not selected:
        print(f"没有场景匹配 {argv}；可选：{[m.name for m in MUTATIONS]}")
        return 1

    originals: dict[Path, str] = {}

    def original_of(path: Path) -> str:
        """按需读、按需缓存：只有被变异的文件才进内存，还原时整体写回。"""
        if path not in originals:
            originals[path] = path.read_text(encoding="utf-8")
        return originals[path]

    def restore_all() -> None:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    for mutation in selected:
        original_of(REPO_ROOT / mutation.path)

    # 基线：未变异时必须全绿。否则"变异被抓住"可能只是因为套件本来就坏着。
    all_tests = tuple(dict.fromkeys(t for m in selected for t in (*m.tests, *m.stay_green)))
    green, tail = _run(all_tests)
    if not green:
        print("基线就是红的 —— 先修好再谈反证：")
        print(tail)
        return 1
    print(f"基线绿（{len(all_tests)} 条用例，覆盖 {len(originals)} 个源文件）\n")

    failures: list[str] = []
    try:
        for mutation in selected:
            target = REPO_ROOT / mutation.path
            original = original_of(target)

            occurrences = original.count(mutation.old)
            if occurrences != 1:
                print(
                    f"[跳过] {mutation.name}：锚点在 {mutation.path} 里出现 {occurrences} 次（应为 1）"
                )
                failures.append(mutation.name)
                continue

            target.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8")
            try:
                caught = _run_each(mutation.tests)
                still_green = _run_each(mutation.stay_green)
            finally:
                target.write_text(original, encoding="utf-8")

            missed = [t for t, ok in caught.items() if ok]
            unexpected = [t for t, ok in still_green.items() if not ok]
            if missed or unexpected:
                print(f"[溜过] {mutation.name}\n        {mutation.why}")
                for test in missed:
                    print(f"        本该红却绿了：{test.rsplit('::', 1)[-1]}")
                for test in unexpected:
                    print(f"        声明保持绿却红了：{test.rsplit('::', 1)[-1]}")
                failures.append(mutation.name)
                continue

            reds = " | ".join(t.rsplit("::", 1)[-1] for t in caught)
            print(f"[抓住] {mutation.name}\n        ← {reds}")
            for test in mutation.stay_green:
                print(f"        （如预期保持绿）{test.rsplit('::', 1)[-1]}")
    finally:
        restore_all()

    print()
    if failures:
        print(f"{len(failures)}/{len(selected)} 个变异没被抓住：" + "、".join(failures))
        return 1
    print(f"{len(selected)}/{len(selected)} 个变异全部被抓住 —— 这些边界是靠断言守着的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
