"""进程入口层：身份来源 + 组装 + 退出码（`identity.py` / `stdio.py` / `__main__.py`）。

三条边界是本文件的重点：

1. **身份从启动配置来，且不可由客户端自报** —— 预设表与契约 §一.2 **机器比对**，
   两边任一处改了取值就红；
2. **`is_system` 是派生的，不可配置** —— 它一旦能单独写就是一次提权；
3. **清理不参与退出码** —— `finally` 里抛出的异常会顶掉 `serve()` 的返回值，
   根因就此丢失。
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_server import __main__ as entry
from mcp_server import identity, stdio
from mcp_server.identity import IDENTITY_PRESETS, IdentityPreset, build_creator_context
from runtime import ExecutionRepository, WorkflowRegistry
from runtime.errors import StillroomRuntimeError
from runtime.registry import STATUS_REGISTERED
from validator import KNOWN_CAPABILITIES
from validator.errors import ErrorCode

from source_scan import imported_modules, stdout_write_offenders, top_levels

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "contracts" / "mcp-tools.md"


# ---------------------------------------------------------------------------
# 契约的机器读入
# ---------------------------------------------------------------------------

def _fenced_json_blocks(text: str) -> list[str]:
    return re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)


def _contract_creator_context_example() -> dict[str, Any]:
    """契约 §一 里那段 `creator_context` 示例（带 `//` 注释，不是严格 JSON）。"""
    for block in _fenced_json_blocks(CONTRACT.read_text(encoding="utf-8")):
        if '"identity"' not in block:
            continue
        without_comments = re.sub(r"//[^\n]*", "", block)
        return json.loads(without_comments)
    raise AssertionError("契约里找不到 creator_context 示例 —— 扫描器失效了（反证见下）")


def _contract_table(after_header: list[str]) -> list[list[str]]:
    """取表头恰好等于 `after_header` 的那张 markdown 表的数据行。

    **按行扫、遇到第一行不是表格就停** —— 只把表格行筛出来再切片是不行的：
    契约里表挨着表，切片会一路吃进后面那张错误码表（第一列恰好也是反引号包着的词）。
    """
    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    is_row = lambda line: line.strip().startswith("|") and line.strip().endswith("|")
    cells = lambda line: [cell.strip() for cell in line.strip().strip("|").split("|")]

    for index, line in enumerate(lines):
        if is_row(line) and cells(line) == after_header:
            body: list[list[str]] = []
            for following in lines[index + 2:]:      # index+1 是分隔行
                if not is_row(following):
                    break
                body.append(cells(following))
            return body
    raise AssertionError(f"契约里找不到表头为 {after_header} 的表 —— 扫描器失效了")


def _caps_of_cell(cell: str) -> tuple[str, ...]:
    if "KNOWN_CAPABILITIES" in cell:
        return tuple(sorted(KNOWN_CAPABILITIES))
    return tuple(json.loads(cell.replace("`", "")))


IDENTITY_TABLE_HEADER = ["identity", "`allowed_capabilities`", "`max_trust_level`", "`can_auto_activate`"]


# ---------------------------------------------------------------------------
# 一、预设表与契约 §一.2 机器比对
# ---------------------------------------------------------------------------

def test_the_contract_states_the_identity_presets() -> None:
    """扫描器真读到了东西 —— 下面那条断言依赖它。

    比**集合**不比顺序：契约里那张表的行序（system → human → agent）是按权限递减排的，
    属于排版可读性，不是协议约定。锁顺序只会让人下次调表格版式时白红一次。
    """
    rows = _contract_table(IDENTITY_TABLE_HEADER)
    assert {r[0].strip("`") for r in rows} == set(IDENTITY_PRESETS)


def test_the_preset_table_matches_the_contract_row_by_row() -> None:
    """**这条是"身份从哪来"的契约锚点。**

    契约 §一.2 那张表改了取值而实现没跟 → 红；实现调了信任档而契约没改 → 也红。
    身份是安全模型的最后一环，它的取值不能靠"记得同步"。
    """
    for row in _contract_table(IDENTITY_TABLE_HEADER):
        name = row[0].strip("`")
        preset = IDENTITY_PRESETS[name]
        assert preset.allowed_capabilities == _caps_of_cell(row[1]), f"{name}.allowed_capabilities"
        assert preset.max_trust_level == row[2].strip("`"), f"{name}.max_trust_level"
        assert preset.can_auto_activate is (row[3].strip("`") == "true"), f"{name}.can_auto_activate"


def test_the_agent_preset_is_the_contracts_creator_context_example() -> None:
    """§一 那段示例就是 `agent` 预设的逐字定义 —— 两边机器比对，不是照着抄。"""
    example = _contract_creator_context_example()
    context = build_creator_context("agent")

    assert {key: context[key] for key in example} == example
    assert context["is_system"] is False


def test_the_creator_context_example_scanner_actually_sees_the_block() -> None:
    """反证：抽取器不是永远返回空。同时钉住"示例里确实有 identity"。"""
    example = _contract_creator_context_example()
    assert example["identity"] == "agent"
    assert set(example) == {"identity", "allowed_capabilities", "max_trust_level", "can_auto_activate"}


# ---------------------------------------------------------------------------
# 二、预设的语义约束
# ---------------------------------------------------------------------------

def test_every_preset_capability_is_a_known_capability() -> None:
    for name, preset in IDENTITY_PRESETS.items():
        assert set(preset.allowed_capabilities) <= KNOWN_CAPABILITIES, name


def test_only_the_system_identity_can_self_authorize() -> None:
    """只有 `system` 有 `T3 + can_auto_activate`。

    L3 里"自动激活"要求 declared T3 且 creator 达到 T3 —— 若还有第二个身份拿到这组合，
    "免人工批准"就变成默认可得了。
    """
    self_authorizing = [
        name for name, preset in IDENTITY_PRESETS.items()
        if preset.can_auto_activate or preset.max_trust_level == "T3"
    ]
    assert self_authorizing == ["system"]


def test_is_system_is_derived_and_not_a_configurable_field() -> None:
    """`is_system` 不可单独写 —— 能写就是一次提权。

    两条一起断：字段不在预设里（**不可表达**），且值确实跟着 identity 走。
    """
    assert "is_system" not in IdentityPreset.__dataclass_fields__
    assert build_creator_context("system")["is_system"] is True
    assert build_creator_context("human")["is_system"] is False
    assert build_creator_context("agent")["is_system"] is False


def test_an_unknown_identity_raises_instead_of_falling_back() -> None:
    """未知身份**拒绝启动**，不回落默认值。

    回落成 `agent` 看着"安全"，但它让"配错了"表现为"服务能用一部分功能" ——
    比拒绝启动难查一个数量级。
    """
    with pytest.raises(ValueError) as caught:
        build_creator_context("root")
    assert "root" in str(caught.value)
    assert all(name in str(caught.value) for name in IDENTITY_PRESETS)


def test_each_call_hands_out_its_own_list() -> None:
    """两次调用不共享同一个 list。

    共享的话，"改一处别处跟着变"会在 `ServerContext` 冻结**之前**发生，
    而冻结挡不住它（那时还没冻）。
    """
    first = build_creator_context("human")
    first["allowed_capabilities"].append("injected.capability")

    assert "injected.capability" not in build_creator_context("human")["allowed_capabilities"]


# ---------------------------------------------------------------------------
# 三、build_server_context
# ---------------------------------------------------------------------------

def _thaw(value: Any) -> Any:
    """把 `ServerContext` 冻过的结构还原成普通 dict/list，便于与未冻的比。"""
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def test_it_builds_a_context_whose_creator_is_frozen(tmp_path: Path) -> None:
    ctx = stdio.build_server_context(db_path=tmp_path / "protocol.db", identity="agent", log=lambda _m: None)

    assert _thaw(ctx.creator_context) == build_creator_context("agent")
    with pytest.raises(TypeError):
        ctx.creator_context["identity"] = "system"     # type: ignore[index]
    assert isinstance(ctx.creator_context["allowed_capabilities"], tuple)


def test_the_artifact_store_defaults_next_to_the_database(tmp_path: Path) -> None:
    """产物目录的默认值**只有一个出处**（`registry.py` 那条"一个库 + 一个目录"的规则）。"""
    ctx = stdio.build_server_context(db_path=tmp_path / "protocol.db", identity="agent", log=lambda _m: None)
    assert ctx.artifacts.root == tmp_path / "artifacts"


def test_an_explicit_artifact_directory_wins(tmp_path: Path) -> None:
    ctx = stdio.build_server_context(
        db_path=tmp_path / "protocol.db",
        identity="agent",
        artifacts_dir=tmp_path / "elsewhere",
        log=lambda _m: None,
    )
    assert ctx.artifacts.root == tmp_path / "elsewhere"


def test_it_reports_who_this_process_is(tmp_path: Path) -> None:
    """启动时必须留一句"这台机器现在是谁" —— 否则运维只能靠猜。"""
    lines: list[str] = []
    stdio.build_server_context(db_path=tmp_path / "protocol.db", identity="agent", log=lines.append)

    assert len(lines) == 1
    assert "identity=agent" in lines[0]


def test_it_never_writes_the_startup_line_to_stdout(tmp_path: Path, capsys) -> None:
    """没注入 `log` 时落 stderr，不落 stdout（stdout 只走协议帧）。"""
    stdio.build_server_context(db_path=tmp_path / "protocol.db", identity="agent")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "identity=agent" in captured.err


def _too_new_database(path: Path) -> None:
    """把库的 protocol 域版本抬到代码之上 —— 复现"库比代码新"。"""
    repo = ExecutionRepository(path)
    repo._conn.execute("UPDATE schema_meta SET version = 99 WHERE scope = 'protocol'")
    repo._conn.commit()
    repo.close()


def test_a_newer_schema_refuses_to_start(tmp_path: Path) -> None:
    """库比代码新 → 启动失败，**不做迁移尝试**（`ensure_schema` 已经这么判了）。"""
    path = tmp_path / "protocol.db"
    _too_new_database(path)

    with pytest.raises(StillroomRuntimeError) as caught:
        stdio.build_server_context(db_path=path, identity="agent", log=lambda _m: None)
    assert caught.value.code == ErrorCode.SCHEMA_INVALID.value
    assert "newer than code" in caught.value.message


def test_a_failed_start_closes_the_connection_it_already_opened(tmp_path: Path, monkeypatch) -> None:
    """起不来时不留半开的连接。

    造法：让 `WorkflowRegistry` 一定失败，看 `ExecutionRepository` 有没有被关掉。
    断言的是**行为**（关没关），不是"谁负责关"——所以用 spy 而不是看 WAL 文件。
    """
    opened: list[ExecutionRepository] = []

    class SpyRepository(ExecutionRepository):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("registry cannot start")

    monkeypatch.setattr(stdio, "ExecutionRepository", SpyRepository)
    monkeypatch.setattr(stdio, "WorkflowRegistry", explode)

    with pytest.raises(OSError):
        stdio.build_server_context(db_path=tmp_path / "protocol.db", identity="agent", log=lambda _m: None)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0]._conn.execute("SELECT 1")     # 连接已关：再用就报 ProgrammingError


# ---------------------------------------------------------------------------
# 四、run
# ---------------------------------------------------------------------------

def _ctx(tmp_path: Path, identity: str = "agent") -> Any:
    return stdio.build_server_context(db_path=tmp_path / "protocol.db", identity=identity, log=lambda _m: None)


def _call_request(tool: str, arguments: dict[str, Any] | None = None, *, request_id: Any = 1) -> dict[str, Any]:
    """一条 `tools/call` 请求（dict 形态）。

    **wire 上没有 11 个方法，只有 3 个**（`initialize` / `tools/list` / `tools/call`）——
    见 `mcp.py` 与契约 §五.7。工具名只能出现在 `params.name` 里。
    """
    params: dict[str, Any] = {"name": tool}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def _call_frame(tool: str, arguments: dict[str, Any] | None = None, *, request_id: Any = 1) -> str:
    """同上，但拼成可直接喂 `serve()` 的一帧。"""
    return json.dumps(_call_request(tool, arguments, request_id=request_id)) + "\n"


def test_run_answers_one_frame_and_returns_zero_on_eof(tmp_path: Path) -> None:
    reader = io.StringIO(_call_frame("get_capabilities", {}))
    writer = io.StringIO()

    code = stdio.run(_ctx(tmp_path), reader=reader, writer=writer, log=lambda _m: None)

    assert code == entry.EXIT_OK
    frame = json.loads(writer.getvalue().strip())
    assert frame["id"] == 1
    assert frame["result"]["isError"] is False
    payload = json.loads(frame["result"]["content"][0]["text"])
    assert payload["data"]["identity"] == "agent"


def test_run_reports_a_broken_writer(tmp_path: Path) -> None:
    """stdout 写不出去 = 不可恢复 → 非 0 退出码（`serve` 的约定）。"""
    class Broken(io.StringIO):
        def write(self, _data: str) -> int:      # type: ignore[override]
            raise BrokenPipeError("client went away")

    reader = io.StringIO(_call_frame("get_capabilities", {}))
    code = stdio.run(_ctx(tmp_path), reader=reader, writer=Broken(), log=lambda _m: None)

    assert code == entry.EXIT_FAILED


def test_run_does_not_close_the_context(tmp_path: Path) -> None:
    """资源是调用方建的，就由调用方关 —— 测试要在同一进程里跑完再看库，靠的就是这条。"""
    ctx = _ctx(tmp_path)
    stdio.run(ctx, reader=io.StringIO(""), writer=io.StringIO(), log=lambda _m: None)

    assert ctx.repo.get  # 连接还活着，没被 run 关掉


# ---------------------------------------------------------------------------
# 五、close_quietly
# ---------------------------------------------------------------------------

def test_close_quietly_closes_both_domains(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    stdio.close_quietly(ctx, log=lambda _m: None)

    for domain in (ctx.repo, ctx.registry):
        with pytest.raises(sqlite3.ProgrammingError):
            domain._conn.execute("SELECT 1")


def test_a_failing_close_is_logged_not_raised(tmp_path: Path) -> None:
    """清理失败要**响**（记日志）但**不抛** —— 抛出去就会顶掉真正的退出码。"""
    ctx = _ctx(tmp_path)
    lines: list[str] = []

    def exploding_close(domain: Any) -> Any:
        def close() -> None:
            raise OSError(f"{domain} refuses to close")
        domain.close = close               # type: ignore[method-assign]
        return domain

    exploding_close(ctx.registry)
    exploding_close(ctx.repo)
    stdio.close_quietly(ctx, log=lines.append)

    assert len(lines) == 2
    assert all("refuses to close" in line for line in lines)


# ---------------------------------------------------------------------------
# 六、CLI：参数 > 环境变量 > 缺省
# ---------------------------------------------------------------------------

@pytest.fixture
def parser() -> Any:
    return entry.build_parser()


def _resolve(parser: Any, argv: list[str], environ: dict[str, str], tmp_path: Path) -> Any:
    return entry.resolve_settings(parser.parse_args(argv), environ)


def test_settings_precedence_argument_over_environment(parser, tmp_path: Path) -> None:
    db, artifacts, who = _resolve(
        parser,
        ["--db", str(tmp_path / "a.db"), "--artifacts", str(tmp_path / "art"), "--identity", "human"],
        {"STILLROOM_PROTOCOL_DB": str(tmp_path / "env.db"), "STILLROOM_ARTIFACTS_DIR": str(tmp_path / "envart"),
         "STILLROOM_IDENTITY": "agent"},
        tmp_path,
    )
    assert (db, artifacts, who) == (tmp_path / "a.db", tmp_path / "art", "human")


def test_settings_fall_back_to_the_environment(parser, tmp_path: Path) -> None:
    db, artifacts, who = _resolve(
        parser, [],
        {"STILLROOM_PROTOCOL_DB": str(tmp_path / "env.db"),
         "STILLROOM_ARTIFACTS_DIR": str(tmp_path / "envart"), "STILLROOM_IDENTITY": "system"},
        tmp_path,
    )
    assert (db, artifacts, who) == (tmp_path / "env.db", tmp_path / "envart", "system")


def test_the_database_has_a_default_but_the_artifact_directory_does_not(parser, tmp_path: Path) -> None:
    """库有缺省（计划决策 #2 定的链），产物目录**没有** —— 那条规则在 `registry.py` 里。"""
    db, artifacts, _who = _resolve(parser, ["--identity", "agent"], {}, tmp_path)
    assert db == entry.default_db_path()
    assert artifacts is None


def test_a_missing_identity_is_a_configuration_error(parser, tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        _resolve(parser, [], {}, tmp_path)
    assert "identity is required" in str(caught.value)


def test_a_database_path_pointing_at_a_directory_is_rejected(parser, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _resolve(parser, ["--db", str(tmp_path), "--identity", "agent"], {}, tmp_path)


def test_an_artifact_path_pointing_at_a_file_is_rejected(parser, tmp_path: Path) -> None:
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        _resolve(parser, ["--identity", "agent", "--artifacts", str(a_file)], {}, tmp_path)


def test_an_unknown_identity_exits_with_the_usage_code(tmp_path: Path, capsys) -> None:
    """未知身份由 `argparse` 的 `choices` 拦下 —— 用法错误，退出码 2。

    造法上有一处**容易写错**：`capsys.readouterr()` 是**消费式**的，
    读第二次拿到的是空串。所以这里只读一次、存成变量再断言两件事。
    """
    with pytest.raises(SystemExit) as caught:
        entry.main(["--db", str(tmp_path / "p.db"), "--identity", "root"], environ={})

    captured = capsys.readouterr()
    assert caught.value.code == entry.EXIT_USAGE
    assert captured.out == "", "用法错误不许写 stdout"
    assert "invalid choice" in captured.err


def test_a_missing_identity_exits_with_the_usage_code(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        entry.main(["--db", str(tmp_path / "p.db")], environ={})
    assert caught.value.code == entry.EXIT_USAGE


def test_the_identity_can_come_from_the_environment(tmp_path: Path) -> None:
    """与上一条互为反证：环境变量确实被读了，不是"永远必填失败"。"""
    code = _serve_one_frame(
        ["--db", str(tmp_path / "p.db")], {"STILLROOM_IDENTITY": "human"}
    )
    assert code == entry.EXIT_OK


# ---------------------------------------------------------------------------
# 七、main：一次真正的会话（同进程）
# ---------------------------------------------------------------------------

def _serve_one_frame(
    argv: list[str],
    environ: dict[str, str],
    requests: list[dict[str, Any]] | None = None,
    monkeypatch: Any = None,
) -> int:
    """在**同一进程**里跑一次 `main`，用 StringIO 顶掉标准流。

    比 subprocess 快得多，而且能在跑完之后接着检查库。
    真要证明"进程入口也能用"的那一条在文件末尾（那个才起子进程）。
    """
    stream_in = io.StringIO(
        "".join(json.dumps(r) + "\n" for r in (
            requests or [_call_request("get_capabilities", {})]
        ))
    )
    stream_out = io.StringIO()
    if monkeypatch is None:
        return _main_with_streams(argv, environ, stream_in, stream_out)
    monkeypatch.setattr(sys, "stdin", stream_in)
    monkeypatch.setattr(sys, "stdout", stream_out)
    code = entry.main(argv, environ=environ)
    return code


def _main_with_streams(argv: list[str], environ: dict[str, str], stream_in: io.StringIO, stream_out: io.StringIO) -> int:
    """不依赖 monkeypatch 的版本：直接换掉 `sys` 上的两个流，跑完还原。"""
    original_in, original_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = stream_in, stream_out           # type: ignore[assignment]
    try:
        return entry.main(argv, environ=environ)
    finally:
        sys.stdin, sys.stdout = original_in, original_out   # type: ignore[assignment]


def _run_main(argv: list[str], environ: dict[str, str], requests: list[dict[str, Any]]) -> tuple[int, io.StringIO]:
    stream_out = io.StringIO()
    stream_in = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    code = _main_with_streams(argv, environ, stream_in, stream_out)
    return code, stream_out


def test_a_full_session_through_main(tmp_path: Path) -> None:
    """握手 → 建工作流 → 起执行，全程走 `main` 的 stdin/stdout。"""
    definition = json.loads(
        (Path(__file__).parent / "fixtures" / "workflow" / "valid_minimal.json").read_text(encoding="utf-8")
    )
    code, out = _run_main(
        ["--db", str(tmp_path / "protocol.db"), "--identity", "agent"],
        {},
        [
            _call_request("get_capabilities", {}, request_id=1),
            _call_request("create_workflow", {"workflow": definition, "activate": True}, request_id=2),
            _call_request("execute_workflow", {"workflow_id": definition["workflow_id"], "version": 1,
                                               "request_id": "req_main", "input": {}}, request_id=3),
        ],
    )

    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == entry.EXIT_OK
    assert [f["id"] for f in frames] == [1, 2, 3]
    # 每条都包在 MCP 的 content 里（wire 只有 3 个方法，业务信封在 text 里）
    payloads = [json.loads(f["result"]["content"][0]["text"]) for f in frames]
    assert all(p["ok"] is True for p in payloads), payloads
    assert all(f["result"]["isError"] is False for f in frames)
    assert payloads[0]["data"]["identity"] == "agent"


def test_main_warns_loudly_when_the_identity_is_system(tmp_path: Path, capsys) -> None:
    """`system` 是唯一能自我授权的身份 —— 本地用户加个参数就能拿到它，必须留痕。"""
    _run_main(["--db", str(tmp_path / "p.db"), "--identity", "system"], {}, [])

    assert "WARNING identity=system" in capsys.readouterr().err


def test_main_returns_failed_on_a_startup_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "protocol.db"
    _too_new_database(path)

    code, out = _run_main(["--db", str(path), "--identity", "agent"], {}, [])

    assert code == entry.EXIT_FAILED
    assert out.getvalue() == ""
    assert "cannot start" in capsys.readouterr().err


def test_an_interrupted_run_still_closes_and_reports_130(tmp_path: Path, monkeypatch) -> None:
    """Ctrl-C：连接照样关掉，但退出码反映"被打断"而不是"正常收工"。"""
    closed: list[str] = []

    def interrupted(*_args: Any, **_kwargs: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "run", interrupted)
    monkeypatch.setattr(entry, "close_quietly", lambda _ctx, **_: closed.append("closed"))

    code, _out = _run_main(["--db", str(tmp_path / "p.db"), "--identity", "agent"], {}, [])

    assert code == entry.EXIT_INTERRUPTED
    assert closed == ["closed"], "被打断也必须走清理"


def test_the_serve_exit_code_survives_a_cleanup_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    """**这条钉住"清理不参与退出码"，而且钉在决定退出码的那一层。**

    造法：`run` 报"stdout 写不出去"（退出码 1），同时让清理**意外**炸掉
    （模拟 `close_quietly` 自己失灵，而不是它内部的逐域 catch）。

    不兜的话，`finally` 里冒出的异常会顶掉 `return` 的返回值，
    于是"IO 坏了"被报成"关库失败" —— 排障时看到的是一个与被测对象无关的报错。
    """
    monkeypatch.setattr(entry, "run", lambda *_a, **_k: entry.EXIT_FAILED)

    def exploding_close(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("close() blew up")

    monkeypatch.setattr(entry, "close_quietly", exploding_close)

    code, _out = _run_main(["--db", str(tmp_path / "p.db"), "--identity", "agent"], {}, [])

    assert code == entry.EXIT_FAILED, "退出码被清理异常顶掉了"
    assert "cleanup failed unexpectedly" in capsys.readouterr().err, "吞可以，无声不行"


def test_an_interrupted_run_also_survives_a_cleanup_failure(tmp_path: Path, monkeypatch) -> None:
    """同一个不变式在 Ctrl-C 那条路上也要成立（两条路径各走一遍）。"""
    def interrupted(*_args: Any, **_kwargs: Any) -> int:
        raise KeyboardInterrupt

    def exploding_close(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("close() blew up")

    monkeypatch.setattr(entry, "run", interrupted)
    monkeypatch.setattr(entry, "close_quietly", exploding_close)

    code, _out = _run_main(["--db", str(tmp_path / "p.db"), "--identity", "agent"], {}, [])

    assert code == entry.EXIT_INTERRUPTED


# ---------------------------------------------------------------------------
# 八、真进程：证明"入口"这一层拼得起来
# ---------------------------------------------------------------------------

def test_the_real_process_answers_one_frame_and_exits_cleanly(tmp_path: Path) -> None:
    """唯一的子进程用例：证明 `python -m mcp_server` 这条路真的通。

    它验的是别的东西验不到的一件事 —— **标准流接得对**：
    `main` 里换 StringIO 能过，但"`sys.stdin` 到底被谁读了、`sys.stdout` 到底被谁写了"
    只有在真进程里才成立。

    顺带断言帧里**没有 `\\r`**：Windows 文本模式会把 `\\n` 翻成 `\\r\\n`，
    这个断言就是 `configure_streams` 的守卫。
    """
    request = json.dumps(_call_request("get_capabilities", {}, request_id=7), ensure_ascii=False)
    completed = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--db", str(tmp_path / "protocol.db"), "--identity", "agent"],
        input=request + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        timeout=60,
    )

    assert completed.returncode == entry.EXIT_OK, completed.stderr
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, completed.stdout
    assert "\r" not in completed.stdout
    frame = json.loads(lines[0])
    assert frame["id"] == 7
    payload = json.loads(frame["result"]["content"][0]["text"])
    assert payload["data"]["identity"] == "agent"
    assert "identity=agent" in completed.stderr


def test_the_real_process_refuses_to_start_without_an_identity(tmp_path: Path) -> None:
    """真进程里"没给身份"也是**用法错误**（2，不是 1）。

    环境**不清空**，只把 `STILLROOM_IDENTITY` 显式置空 ——
    `env={"PATH": ""}` 那种写法会把解释器自己的启动环境也弄没，
    于是进程在 `main` 之前就死了、拿到一个毫无意义的退出码 1，
    测到的根本不是被测对象。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--db", str(tmp_path / "protocol.db")],
        input="", capture_output=True, text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=60,
        env={**os.environ, "STILLROOM_IDENTITY": ""},
    )

    assert completed.returncode == entry.EXIT_USAGE, completed.stderr
    assert completed.stdout == ""
    assert "identity is required" in completed.stderr


def test_a_bad_byte_does_not_kill_the_process(tmp_path: Path) -> None:
    """**进程级证明边界 #9：一条坏消息不许把服务带走。**

    为什么必须在这里测：`tests/test_mcp_jsonrpc.py` 那批用的是 `io.StringIO`
    —— 字符串流上**根本产生不出解码错误**。而 `serve()` 收的是文本流，
    真 stdin 上一个坏字节会让 `readline()` 抛 `UnicodeDecodeError`，
    它穿过整个 `serve()`。所以"坏字节变成 `-32700` 且循环继续"这件事，
    只有在真进程里才成立，也只有在这里才能被证伪。

    断言三件事：坏行回 `-32700`；**后面那行照样被处理**；进程干净退出（0）。
    """
    good = json.dumps(_call_request("get_capabilities", {}, request_id=3)).encode("utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--db", str(tmp_path / "protocol.db"), "--identity", "agent"],
        input=b"\xff\xfe\xff not json\n" + good + b"\n",
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=60,
    )

    assert completed.returncode == entry.EXIT_OK, completed.stderr.decode("utf-8", "replace")
    frames = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines()]
    assert len(frames) == 2, completed.stdout
    assert frames[0]["error"]["code"] == -32700
    assert frames[1]["id"] == 3
    body = json.loads(frames[1]["result"]["content"][0]["text"])
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# 九、分层与 stdout 纪律
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["identity.py", "stdio.py", "mcp.py", "dispatch.py"])
def test_the_entry_modules_never_write_to_stdout(module: str) -> None:
    """这几个模块的 stdout 字节数必须是 0。

    它们的输出要么走 `writer`（协议帧），要么走 `log`（stderr）。
    在源码上钉住是因为"这次没写"证不了"下次不会顺手写"。

    扫描器实现是**共享**的（`tests/source_scan.py`）—— 规则只写一遍，
    否则改一次规则要改几个地方，漏一个就留下一条在守旧规则的测试。
    """
    source = (REPO_ROOT / "mcp_server" / module).read_text(encoding="utf-8")
    assert stdout_write_offenders(source) == []


def test_the_entry_layer_does_not_import_the_gui() -> None:
    """入口层与 GUI 之间**双向**不相干：这里查"不 import app / PySide6"。

    反方向（GUI 不许被无头层需要）由 `test_headless_boundary.py` 用
    import 拦截器 + 源码扫描两头锁 —— 见计划 §九 风险表"两者互为保险"。
    """
    for module in ("identity.py", "stdio.py", "mcp.py", "dispatch.py", "__main__.py"):
        source = (REPO_ROOT / "mcp_server" / module).read_text(encoding="utf-8")
        banned = top_levels(imported_modules(source, package="mcp_server")) & {"app", "PySide6"}
        assert banned == set(), f"{module} 不该 import {banned}"


def test_the_registry_round_trips_through_the_context_the_entry_built(tmp_path: Path) -> None:
    """入口装出来的 ctx 能真的干活（不是只有形状对）。"""
    ctx = _ctx(tmp_path, identity="system")
    record = ctx.registry.register_skill(
        {"skill_id": "s", "version": 1, "description": "d",
         "required_capabilities": ["llm.call"], "io_contract": {}},
        creator=dict(ctx.creator_context),
    )
    assert record.status == STATUS_REGISTERED
