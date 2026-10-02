"""工作流注册中心。

要锁住的东西分四层：

1. **定义内容不进 SQLite** —— 行里只有 `definition_hash` + `definition_ref`，
   本体在内容寻址的 store 里。`workflow_version` 只是版本号，版本号不等于内容。
2. **`(workflow_id, version)` 内容不可变** —— 否则"某 execution 跑的是 v3"这句话没有意义。
3. **只过校验才落库** —— 校验失败不留行、不留 blob。
4. **两域互不依赖** —— 共用 `db_path`，但 `ExecutionRepository` 不持有 registry，
   它对定义指纹只是"被喂三个入参"。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from runtime import (
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_PENDING_ACTIVATION,
    ExecutionRepository,
    RepositoryError,
    WorkflowRegistry,
)
from validator.errors import ErrorCode

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "workflow"

_CREATOR_T2: dict[str, Any] = {
    "identity": "agent",
    "is_system": False,
    "allowed_capabilities": ["llm.call", "file.read", "file.write"],
    "max_trust_level": "T2",
    "can_auto_activate": False,
}

_CREATOR_T3: dict[str, Any] = {**_CREATOR_T2, "max_trust_level": "T3", "can_auto_activate": True}


def _fixture(name: str) -> dict[str, Any]:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return json.loads(text)


def _definition(**overrides: Any) -> dict[str, Any]:
    """一份合法定义（`valid_minimal`）+ 覆写。每个用例都用独立副本，避免交叉污染。"""
    definition = copy.deepcopy(_fixture("valid_minimal.json"))
    definition.update(overrides)
    return definition


def _registry(tmp_path: Path) -> WorkflowRegistry:
    return WorkflowRegistry(tmp_path / "protocol.db")


# ---------------------------------------------------------------------------
# 一、注册、幂等、不可变
# ---------------------------------------------------------------------------

def test_register_stores_hash_and_ref_but_not_the_definition(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        record = registry.register(_definition(), creator=_CREATOR_T2)

        assert record.workflow_id == "minimal_job" and record.version == 1
        assert record.status == STATUS_PENDING_ACTIVATION     # T2 + human_required
        assert record.activated_at is None
        # ref 里的 hex 与行内指纹**必须**是同一个 —— 否则"内容寻址"只是装饰
        assert record.definition_ref == f"artifact:sha256:{record.definition_hash}"

        # 定义本体不在 SQLite 里：行里只有那两个字段
        columns = {
            row[1] for row in registry._conn.execute("PRAGMA table_info(workflows)")
        }
        assert "definition" not in columns and "definition_json" not in columns


def test_re_registering_identical_content_is_idempotent(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        first = registry.register(_definition(), creator=_CREATOR_T2)
        second = registry.register(_definition(), creator=_CREATOR_T2)

        assert first == second
        assert _row_count(registry, "workflows") == 1
        assert _blob_count(registry) == 1        # 没写第二份盘


def test_same_version_with_different_content_is_rejected(tmp_path: Path) -> None:
    """版本号一旦发布就绑死内容 —— 这是"可复现"的前提。"""
    with _registry(tmp_path) as registry:
        original = registry.register(_definition(), creator=_CREATOR_T2)

        with pytest.raises(RepositoryError) as exc:
            registry.register(
                _definition(resource_policy={"max_tokens": 9999, "max_execution_time": 60,
                                             "max_retries": 1}),
                creator=_CREATOR_T2,
            )
        assert exc.value.code == ErrorCode.WORKFLOW_VERSION_IMMUTABLE.value
        assert exc.value.details["registered_hash"] == original.definition_hash

        # 原记录没被动过
        assert registry.get("minimal_job", 1) == original


def test_a_new_version_is_allowed_and_coexists(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        v1 = registry.register(_definition(), creator=_CREATOR_T2)
        v2 = registry.register(_definition(version=2), creator=_CREATOR_T2)

        # `version` 本身是定义的一部分，所以两版的指纹**不同** —— 这正是"版本号绑内容"的体现
        assert v1.definition_hash != v2.definition_hash
        assert (v1.version, v2.version) == (1, 2)
        assert len(registry.list(workflow_id="minimal_job")) == 2
        assert _blob_count(registry) == 2          # 两份不同的内容 → 两个地址


def test_content_hash_is_order_insensitive(tmp_path: Path) -> None:
    """键序不影响指纹 —— 规范化 JSON 的键排序在这里兑现。"""
    with _registry(tmp_path) as registry:
        straight = registry.register(_definition(), creator=_CREATOR_T2)

    shuffled = _definition()
    shuffled["step_overrides"] = dict(reversed(list(shuffled["step_overrides"].items())))
    with _registry(tmp_path) as registry:
        reordered = registry.register(shuffled, creator=_CREATOR_T2)
        assert reordered.definition_hash == straight.definition_hash


# ---------------------------------------------------------------------------
# 二、四层校验是闸门
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture_name,expected_code",
    [
        ("invalid_schema_missing_required.json", ErrorCode.SCHEMA_INVALID),
        ("invalid_semantic_understand_disabled.json", ErrorCode.SEMANTIC_INVALID),
        ("invalid_trust_t0_agent.json", ErrorCode.INSUFFICIENT_TRUST),
        ("invalid_capability_denied.json", ErrorCode.CAPABILITY_DENIED),
        ("invalid_metadata_control_field.json", ErrorCode.METADATA_FORBIDDEN),
    ],
)
def test_a_rejected_definition_lands_nothing(
    tmp_path: Path, fixture_name: str, expected_code: ErrorCode
) -> None:
    """校验失败必须**完全没落库**：没有行，也没有 blob。

    只写盘不写行，或反过来，都是半个状态 —— 将来按 ref 读会拿到孤儿内容。
    """
    with _registry(tmp_path) as registry:
        with pytest.raises(RepositoryError) as exc:
            registry.register(_fixture(fixture_name), creator=_CREATOR_T2)

        assert exc.value.code == expected_code.value
        assert registry.list() == ()
        assert _row_count(registry, "workflows") == 0
        assert _blob_count(registry) == 0


def test_rejection_reports_the_offending_path(tmp_path: Path) -> None:
    """诊断信息要能指到字段 —— 只说"不合法"对调用方没用。"""
    with _registry(tmp_path) as registry:
        with pytest.raises(RepositoryError) as exc:
            registry.register(_fixture("invalid_metadata_control_field.json"),
                              creator=_CREATOR_T2)
        assert exc.value.details["issues"]
        assert exc.value.details["issues"][0]["path"]


# ---------------------------------------------------------------------------
# 三、取回定义本体
# ---------------------------------------------------------------------------

def test_definition_round_trips_through_the_store(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        definition = _definition()
        registry.register(definition, creator=_CREATOR_T2)

        assert registry.get_definition("minimal_job", 1) == definition


def test_definition_is_readable_from_another_instance(tmp_path: Path) -> None:
    """store 是纯文件系统 + 库是纯 SQLite：换一个实例、甚至换一个进程都读得到。"""
    db = tmp_path / "protocol.db"
    definition = _definition()
    WorkflowRegistry(db).register(definition, creator=_CREATOR_T2)

    with WorkflowRegistry(db) as fresh:
        assert fresh.get_definition("minimal_job", 1) == definition


def test_a_forged_fingerprint_is_caught(tmp_path: Path) -> None:
    """行内指纹被改过 → 与 store 里那份对不上 → 炸。

    这条与 `ArtifactStore` 的读时校验是**两道**：store 保证"文件内容 == 地址"，
    这里保证"地址 == 登记时记下的指纹"。少了后者，改一行 SQL 就能让
    `get_definition()` 交出另一份定义而没人发现。
    """
    db = tmp_path / "protocol.db"
    with WorkflowRegistry(db) as registry:
        registry.register(_definition(), creator=_CREATOR_T2)
        registry._conn.execute(
            "UPDATE workflows SET definition_hash = ? WHERE workflow_id = 'minimal_job'",
            ("0" * 64,),
        )
        registry._conn.commit()

        with pytest.raises(RepositoryError) as exc:
            registry.get_definition("minimal_job", 1)
        assert exc.value.code == ErrorCode.SCHEMA_INVALID.value
        assert exc.value.details["recorded_hash"] == "0" * 64


# ---------------------------------------------------------------------------
# 四、生命周期
# ---------------------------------------------------------------------------

def test_auto_activation_only_for_t3_creators(tmp_path: Path) -> None:
    """`activation: auto` 成立的条件是 **creator 达标**，不是定义自己声明了就算。

    `register()` 只是照抄校验层（L3）的结论落状态 —— 判定不在本模块。
    """
    with _registry(tmp_path) as registry:
        auto = registry.register(_fixture("valid_t3_auto.json"), creator=_CREATOR_T3)
        assert auto.status == STATUS_ACTIVE
        assert auto.activated_at is not None

        # 同样声明 auto 的定义，换成 T2 creator —— 连注册都过不了，而不是"注册成 pending"
        with pytest.raises(RepositoryError) as exc:
            registry.register(
                _definition(
                    permissions={
                        "trust_level": "T3",
                        "activation": "auto",
                        "required_capabilities": ["llm.call"],
                    }
                ),
                creator=_CREATOR_T2,
            )
        assert exc.value.code == ErrorCode.INSUFFICIENT_TRUST.value
        # 被拒的那版没落下：库里还是只有前面那个成功的
        assert [r.workflow_id for r in registry.list()] == ["auto_summarize"]


def test_human_required_definitions_always_land_pending(tmp_path: Path) -> None:
    """`human_required` 就是那道人工激活闸门 —— 连 T3 也要等人点。"""
    with _registry(tmp_path) as registry:
        record = registry.register(
            _definition(
                permissions={
                    "trust_level": "T3",
                    "activation": "human_required",
                    "required_capabilities": ["llm.call"],
                }
            ),
            creator=_CREATOR_T3,
        )
        assert record.status == STATUS_PENDING_ACTIVATION
        assert record.activated_at is None


def test_require_active_is_the_gate_for_execution(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        registry.register(_definition(), creator=_CREATOR_T2)

        with pytest.raises(RepositoryError) as exc:
            registry.require_active("minimal_job", 1)
        assert exc.value.code == ErrorCode.WORKFLOW_NOT_ACTIVE.value
        assert exc.value.details["status"] == STATUS_PENDING_ACTIVATION

        registry.activate("minimal_job", 1)
        assert registry.require_active("minimal_job", 1).active is True


def test_activate_is_not_idempotent_and_says_why(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        registry.register(_definition(), creator=_CREATOR_T2)
        registry.activate("minimal_job", 1)

        with pytest.raises(RepositoryError) as exc:
            registry.activate("minimal_job", 1)
        assert exc.value.code == ErrorCode.SEMANTIC_INVALID.value
        assert exc.value.details["status"] == STATUS_ACTIVE


def test_deprecate_keeps_the_definition_reachable(tmp_path: Path) -> None:
    """下架 ≠ 删除：历史 execution 仍要能找回它跑过的那版定义。"""
    with _registry(tmp_path) as registry:
        definition = _definition()
        registry.register(definition, creator=_CREATOR_T2)
        registry.activate("minimal_job", 1)
        deprecated = registry.deprecate("minimal_job", 1)

        assert deprecated.status == STATUS_DEPRECATED
        assert registry.get_active("minimal_job") is None
        assert registry.get_definition("minimal_job", 1) == definition


def test_get_active_picks_up_the_activated_version(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        registry.register(_definition(), creator=_CREATOR_T2)
        registry.register(_definition(version=2), creator=_CREATOR_T2)
        assert registry.get_active("minimal_job") is None

        registry.activate("minimal_job", 2)
        active = registry.get_active("minimal_job")
        assert active is not None and active.version == 2


def test_missing_workflow_is_reported_as_not_found(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        with pytest.raises(RepositoryError) as exc:
            registry.get("nope", 1)
        assert exc.value.code == ErrorCode.WORKFLOW_NOT_FOUND.value
        assert registry.get_active("nope") is None


def test_list_filters_and_rejects_unknown_status(tmp_path: Path) -> None:
    with _registry(tmp_path) as registry:
        registry.register(_definition(), creator=_CREATOR_T2)
        registry.register(_definition(version=2), creator=_CREATOR_T2)
        registry.register(_fixture("valid_full.json"), creator=_CREATOR_T2)
        registry.activate("minimal_job", 1)

        assert len(registry.list()) == 3
        assert [r.version for r in registry.list(workflow_id="minimal_job")] == [2, 1]
        assert [r.version for r in registry.list(status=STATUS_ACTIVE)] == [1]

        with pytest.raises(ValueError):
            registry.list(status="draft")      # 契约里没有这个状态


def test_summary_shape_matches_the_list_contract(tmp_path: Path) -> None:
    """`list_workflows` 的出参形态 —— 不含定义本体，只给能用来匹配的元信息。"""
    with _registry(tmp_path) as registry:
        record = registry.register(_definition(), creator=_CREATOR_T2)
        summary = record.to_summary()

        assert set(summary) == {
            "workflow_id", "version", "definition_hash", "status", "trust_level", "active"
        }
        assert summary["active"] is False


# ---------------------------------------------------------------------------
# 五、两域互不依赖 + 读者边界
# ---------------------------------------------------------------------------

def test_the_execution_repository_does_not_hold_a_registry(tmp_path: Path) -> None:
    """共用 `db_path` ≠ 相互依赖：`ExecutionRepository` 上不该出现 registry 引用。"""
    db = tmp_path / "protocol.db"
    with ExecutionRepository(db) as repo:
        attributes = vars(repo)
        assert not any(
            isinstance(value, WorkflowRegistry) for value in attributes.values()
        ), "ExecutionRepository 不该持有 WorkflowRegistry —— 将来要物理分离就换 db_path"

    with WorkflowRegistry(db) as registry:
        assert not any(
            isinstance(value, ExecutionRepository) for value in vars(registry).values()
        )


def test_registry_readers_respect_the_snapshot_boundary() -> None:
    """架构不变量：公共读者要么走 `_read()`，要么只由别的公共读者组合而成。

    「直接查 DB 的」必须过快照边界；「组合的」不许自己碰连接。
    分成两类而不是一刀切，是因为 `require_active` / `get_definition` 这类
    天然是"取一次 + 判断"或"取一次 + 读文件"，硬塞 `_read()` 会把文件 IO 关进事务里。
    """
    import inspect

    direct = ("get", "list")
    composed = ("require_active", "get_active", "get_definition")

    for name in direct:
        source = inspect.getsource(getattr(WorkflowRegistry, name))
        assert "self._read(" in source, f"{name}() 没有走 _read() 快照边界"
        assert "self._conn" not in source, f"{name}() 直连了 self._conn"

    for name in composed:
        source = inspect.getsource(getattr(WorkflowRegistry, name))
        assert "self._conn" not in source, f"{name}() 直连了 self._conn"


def test_registry_reuses_the_shared_transaction_discipline() -> None:
    """事务纪律只有一份 —— 两个域都从 `DomainDatabase` 继承，不各写一套。"""
    from runtime.base import DomainDatabase

    assert issubclass(ExecutionRepository, DomainDatabase)
    assert issubclass(WorkflowRegistry, DomainDatabase)


# ---------------------------------------------------------------------------
# 六、定义指纹：execution 跑的是哪一版
# ---------------------------------------------------------------------------

def test_execution_records_the_workflow_definition_it_ran_against(tmp_path: Path) -> None:
    """**这条是"钉住定义指纹"的价值锚点。**

    注册 v1 → 执行 → 之后再注册 v2、把 v1 下架 →
    那个 execution 记录的指纹**必须**仍是 v1 的，且 v1 的定义内容仍取得到。
    不写这条测试，`workflow_definition_hash` 就会变成"没人读也懒得维护"的死列。
    """
    db = tmp_path / "protocol.db"
    v1_body = _definition()

    with WorkflowRegistry(db) as registry:
        registry.register(v1_body, creator=_CREATOR_T2)
        registry.activate("minimal_job", 1)
        ran_against = registry.require_active("minimal_job", 1)

    with ExecutionRepository(db) as repo:
        bound = repo.bind_request(
            request_id="req_fingerprint",
            workflow_id="minimal_job",
            workflow_version=1,
            input_snapshot={"prompt": "x"},
            trust_level="T2",
            workflow_definition_hash=ran_against.definition_hash,
            workflow_definition_ref=ran_against.definition_ref,
        )
        execution_id = bound.record.execution_id

    # registry 继续往前走：注册 v2、激活它、下架 v1
    with WorkflowRegistry(db) as registry:
        registry.register(_definition(version=2), creator=_CREATOR_T2)
        registry.activate("minimal_job", 2)
        registry.deprecate("minimal_job", 1)

        active = registry.get_active("minimal_job")
        assert active is not None and active.version == 2

        with ExecutionRepository(db) as repo:
            record = repo.get(execution_id)
            # 它跑的是 v1 —— 不是"当前 active"
            assert record.workflow_version == 1
            assert record.workflow_definition_hash == ran_against.definition_hash
            assert record.workflow_definition_ref == ran_against.definition_ref

        # 而且能靠这个指纹把当初那份定义原样取回来
        assert registry.get_definition("minimal_job", 1) == v1_body


def test_retry_inherits_the_pinned_definition(tmp_path: Path) -> None:
    """retry 必须跑回原来那一版定义，否则"重试"变成了"跑新定义"。"""
    db = tmp_path / "protocol.db"
    with WorkflowRegistry(db) as registry:
        registry.register(_definition(), creator=_CREATOR_T2)
        registry.activate("minimal_job", 1)
        pinned = registry.require_active("minimal_job", 1)

    with ExecutionRepository(db) as repo:
        first = repo.bind_request(
            request_id="req_retry_fingerprint",
            workflow_id="minimal_job",
            workflow_version=1,
            input_snapshot={"prompt": "x"},
            workflow_definition_hash=pinned.definition_hash,
            workflow_definition_ref=pinned.definition_ref,
        ).record

        repo.append_event(first.execution_id, "engine_started")
        repo.append_event(first.execution_id, "engine_failed", error_code="PROVIDER_ERROR")

        child = repo.retry(first.execution_id, max_retries=1).record
        assert child.attempt == 1
        assert child.workflow_definition_hash == pinned.definition_hash
        assert child.workflow_definition_ref == pinned.definition_ref


def test_an_execution_without_a_pinned_definition_is_still_valid(tmp_path: Path) -> None:
    """指纹是可选的：P1 之前的调用方不传也能建 execution，只是失去可复现性。"""
    with ExecutionRepository(tmp_path / "protocol.db") as repo:
        record = repo.bind_request(
            request_id="req_unpinned",
            workflow_id="minimal_job",
            workflow_version=1,
        ).record
        assert record.workflow_definition_hash is None
        assert record.workflow_definition_ref is None


# ---------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------

def _row_count(registry: WorkflowRegistry, table: str) -> int:
    return int(registry._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _blob_count(registry: WorkflowRegistry) -> int:
    root = registry.artifacts.root
    if not root.exists():
        return 0
    return len([p for p in root.rglob("*") if p.is_file()])
