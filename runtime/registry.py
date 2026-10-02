"""工作流注册中心（P1）。

## 与 `ExecutionRepository` 的关系：共用 `db_path`，互不依赖

两者共用同一个 `protocol.db`（同一个物理库，多个逻辑域），
但 `WorkflowRegistry` **不持有也不接受** `ExecutionRepository`，
`ExecutionRepository` 也不引用 registry —— 它只通过
`workflow_id + workflow_version + definition_hash/ref` 这三样入参被喂。

这么切的收益是"将来要物理分离，改动只是给某个域换 `db_path`"，
而不是"从一个大类里剥出半张表"（剥的过程一定会顺手改逻辑）。

**顺带纠一个常见假设**：共用同一个库**不**等于共用同一个连接 ——
两个类各开自己的连接，所以 `register()` 与 `bind_request()` 是**两个事务**，
它们之间没有原子性。这是**有意的**：工作流定义是长期资产，
"注册成功但这次执行失败"正是期望结果，不该被回滚掉。

## 定义内容不进 SQLite

行里只存 `definition_hash`（内容指纹）+ `definition_ref`（内容地址，见 `artifacts.py`）。
理由：`workflow_version` 只是版本号，**版本号不等于内容**。
把定义本体放进内容寻址的 store，"同一个版本号对应同一份内容"才是可验证的，
而不是靠约定。跨机搬运也因此自动解决：导出 = 导出 registry 行 + 打包对应 artifact。

## 状态机（三态）

```
pending_activation --activate()--> active --deprecate()--> deprecated
```

- `activate()` 就是契约里那道**人工激活**闸门（`activation: "auto"` 且 creator 是 T3
  时，`register()` 直接落 `active` —— 这条规则在 L3 校验里，不在本模块）。
- `deprecated` 让工作流能"下架而不删除"，历史 execution 仍能靠
  `(workflow_id, version)` 找回它跑过的定义。

刻意**没有** `draft` / `tested`：契约（`contracts/mcp-tools.md` 的 `create_workflow`）
只定义了 `pending_activation | active`，多两个状态就是多两处要同步的语义。

## 技能（`skills` 表）也在本类

`registry` 域的两张表 `workflows` / `skills` 归同一个类管（`runtime/__init__.py` 的
分工表就是这么写的）。技能比工作流简单得多：**没有 trust_level 列、没有三态生命周期**
（契约 §二.3 的出参是 `status: "registered"`，技能登记即生效），
所以只有 `register_skill` / `list_skills` 两个方法。

技能定义同样进内容寻址的 store，行里只留 `hash + ref` —— 与工作流是同一条理由。

**一处命名妥协**：`(skill_id, version)` 的内容不可变冲突复用
`WORKFLOW_VERSION_IMMUTABLE`。不变式完全一样（版本号一旦发布就绑死内容），
为它新造一个 `SKILL_VERSION_IMMUTABLE` 只会让同一件事有两套码。
契约下次变更时应把这条码的语义扩写成"注册物版本不可变"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from validator.errors import ErrorCode
from validator.skill_validator import validate_skill
from validator.workflow_validator import validate_workflow

from .artifacts import ArtifactStore
from .base import DomainDatabase
from .errors import RepositoryError
from .hashing import canonical_json, sha256_of_text

STATUS_PENDING_ACTIVATION = "pending_activation"
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"

STATUSES = (STATUS_PENDING_ACTIVATION, STATUS_ACTIVE, STATUS_DEPRECATED)

STATUS_REGISTERED = "registered"
"""技能的**唯一**状态。技能没有人工激活闸门 —— 它不含可执行逻辑，
只是一个被工作流引用的能力声明，登记即生效。"""

_SKILL_COLUMNS = "skill_id, version, definition_hash, definition_ref, status, created_at, activated_at"

_COLUMNS = (
    "workflow_id, version, definition_hash, definition_ref, "
    "status, trust_level, created_at, activated_at"
)


@dataclass(frozen=True)
class WorkflowRecord:
    """`workflows` 表的一行。定义本体不在这 —— 用 `definition_ref` 去 store 取。"""

    workflow_id: str
    version: int
    definition_hash: str
    definition_ref: str
    status: str
    trust_level: str
    created_at: str
    activated_at: str | None

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def to_summary(self) -> dict[str, Any]:
        """**行级**视图（不含定义本体）。

        ⚠️ 它**不是** `list_workflows` 的契约出参 —— 契约 §二.4 要的是
        `{workflow_id, version, display_name, trust_level, active}`，
        其中 `display_name` 在定义里，本方法拿不到。契约出参见 `WorkflowSummary`
        与 `list_workflow_summaries()`。

        保留本方法是因为"只看行、不读 store"这个用途本身成立
        （例如诊断、导出行级快照），但别拿它当接口出参用。
        """
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "definition_hash": self.definition_hash,
            "status": self.status,
            "trust_level": self.trust_level,
            "active": self.active,
        }


@dataclass(frozen=True)
class WorkflowSummary:
    """`list_workflows` 契约 §二.4 的出参形态。

    `display_name` 不在 `workflows` 行里（它是定义的一个字段），
    所以产出它的 `list_workflow_summaries()` 会读一次定义 —— 顺带过一遍指纹校验。
    定义没写 `display_name` 时回落到 `workflow_id`：契约的 schema 里它不是必填，
    回落到 ID 比回落到空串对客户端更有用。
    """

    workflow_id: str
    version: int
    display_name: str
    trust_level: str
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "display_name": self.display_name,
            "trust_level": self.trust_level,
            "active": self.active,
        }


@dataclass(frozen=True)
class SkillRecord:
    """`skills` 表的一行。定义本体不在这 —— 用 `definition_ref` 去 store 取。"""

    skill_id: str
    version: int
    definition_hash: str
    definition_ref: str
    status: str
    created_at: str
    activated_at: str | None


@dataclass(frozen=True)
class SkillSummary:
    """`list_skills` 契约的出参形态（§二.2）。

    注意它**不含** `status` / `active`：契约给的字段就是这四个。
    技能登记即生效，把内部状态泄漏到契约外只会造出第二个需要维护的字段。

    `description` 与 `required_capabilities` 不在 `skills` 行里，
    它们是登记时从 store 里那份定义读出来的 —— 所以 `list_skills()` 会
    顺带做一次指纹校验（读 store 必过的那道）。
    """

    skill_id: str
    version: int
    description: str
    required_capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "description": self.description,
            "required_capabilities": list(self.required_capabilities),
        }


class WorkflowRegistry(DomainDatabase):
    """注册中心：工作流定义（`workflows`）+ 技能（`skills`）。线程安全（单连接 + 可重入锁）。

    连接、锁、`_read` / `_write` 快照边界来自 `DomainDatabase`（见 `runtime/base.py`）。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        artifacts: ArtifactStore | None = None,
        clock=None,
    ) -> None:
        super().__init__(db_path, clock=clock)
        # store 默认放在库旁边，这样"一个库 + 一个目录"就是完整备份单元
        self._artifacts = artifacts or ArtifactStore(self._path.parent / "artifacts")

    # -- 生命周期 ---------------------------------------------------------

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts

    # -- 注册 -------------------------------------------------------------

    def register(
        self,
        definition: dict[str, Any],
        *,
        creator: dict[str, Any],
    ) -> WorkflowRecord:
        """校验并登记一版工作流定义。

        先过四层校验（`validate_workflow`），再把**内容**写进 store、
        把 `hash + ref` 写进行。校验失败时不落任何东西。

        同一个 `(workflow_id, version)`：
        - 内容一模一样 → 幂等返回既有记录（不重复写盘）；
        - 内容不同 → `WORKFLOW_VERSION_IMMUTABLE`。版本号一旦发布就绑死内容，
          否则"某 execution 跑的是 v3"这句话就没有意义。
        """
        result = validate_workflow(definition, creator)
        if not result.ok:
            first = result.issues[0]
            raise RepositoryError(
                first.code,
                f"workflow definition rejected at {first.path}: {first.message}",
                path=first.path,
                workflow_id=str(definition.get("workflow_id", "")),
                issues=[issue.__dict__ for issue in result.issues],
            )

        workflow_id = definition["workflow_id"]
        version = int(definition["version"])
        trust_level = definition["permissions"]["trust_level"]
        text = canonical_json(definition)
        definition_hash = sha256_of_text(text)

        def op(conn) -> WorkflowRecord:
            existing = self._fetch(conn, workflow_id, version)
            if existing is not None:
                if existing.definition_hash != definition_hash:
                    raise RepositoryError(
                        ErrorCode.WORKFLOW_VERSION_IMMUTABLE,
                        f"{workflow_id} v{version} already exists with different content",
                        path="version",
                        workflow_id=workflow_id,
                        version=version,
                        registered_hash=existing.definition_hash,
                        incoming_hash=definition_hash,
                    )
                return existing

            stored = self._artifacts.put_json(definition)
            status = (
                STATUS_ACTIVE
                if _auto_activates(definition, creator)
                else STATUS_PENDING_ACTIVATION
            )
            now = self._clock()
            conn.execute(
                f"INSERT INTO workflows({_COLUMNS}) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workflow_id, version, stored.sha256, stored.ref,
                    status, trust_level, now,
                    now if status == STATUS_ACTIVE else None,
                ),
            )
            record = self._fetch(conn, workflow_id, version)
            assert record is not None  # 刚插进去，读不回来就是实现错了
            return record

        return self._write(op)

    # -- 读 ---------------------------------------------------------------

    def get(self, workflow_id: str, version: int) -> WorkflowRecord:
        def op(conn) -> WorkflowRecord:
            record = self._fetch(conn, workflow_id, version)
            if record is None:
                raise RepositoryError(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    "no such workflow version",
                    workflow_id=workflow_id,
                    version=version,
                )
            return record

        return self._read(op)

    def require_active(self, workflow_id: str, version: int) -> WorkflowRecord:
        """取一版**已激活**的定义；未激活给 `WORKFLOW_NOT_ACTIVE`。

        `execute_workflow` 的前置检查 —— 契约里这两个错误码的落点就是这里。
        """
        record = self.get(workflow_id, version)
        if not record.active:
            raise RepositoryError(
                ErrorCode.WORKFLOW_NOT_ACTIVE,
                f"{workflow_id} v{version} is {record.status}",
                workflow_id=workflow_id,
                version=version,
                status=record.status,
            )
        return record

    def get_active(self, workflow_id: str) -> WorkflowRecord | None:
        """该工作流当前生效的那一版；没有就是 `None`。"""
        return next(iter(self.list(workflow_id=workflow_id, status=STATUS_ACTIVE)), None)

    def list(
        self,
        *,
        workflow_id: str | None = None,
        status: str | None = None,
    ) -> tuple[WorkflowRecord, ...]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown workflow status: {status!r}")

        def op(conn) -> tuple[WorkflowRecord, ...]:
            clauses: list[str] = []
            params: list[Any] = []
            if workflow_id is not None:
                clauses.append("workflow_id = ?")
                params.append(workflow_id)
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM workflows{where} "
                "ORDER BY workflow_id ASC, version DESC",
                params,
            ).fetchall()
            return tuple(self._row_to_record(row) for row in rows)

        return self._read(op)

    def get_definition(self, workflow_id: str, version: int) -> dict[str, Any]:
        """取回定义本体（从内容寻址的 store 里，按 ref 读并校验）。"""
        record = self.get(workflow_id, version)
        return self._verified_definition(record, kind="workflow", key=workflow_id)

    def list_workflow_summaries(
        self, *, include_inactive: bool = False
    ) -> tuple[WorkflowSummary, ...]:
        """`list_workflows` 的契约出参（§二.4）。读定义取 `display_name`。

        排序按 `(workflow_id, version)`，稳定可比对。
        """
        return tuple(
            WorkflowSummary(
                workflow_id=record.workflow_id,
                version=record.version,
                display_name=self._display_name_of(record),
                trust_level=record.trust_level,
                active=record.active,
            )
            for record in self.list()
            if include_inactive or record.active
        )

    def _display_name_of(self, record: WorkflowRecord) -> str:
        definition = self._verified_definition(
            record, kind="workflow", key=record.workflow_id
        )
        display = definition.get("display_name")
        return display if isinstance(display, str) and display.strip() else record.workflow_id

    # -- 技能：注册 -------------------------------------------------------

    def register_skill(
        self,
        definition: dict[str, Any],
        *,
        creator: dict[str, Any],
    ) -> SkillRecord:
        """校验并登记一版技能定义。语义与 `register()` 逐一对应。

        - 同一个 `(skill_id, version)`：内容一样 → 幂等返回；不一样 → 冲突报错。
        - 校验失败不落任何东西（校验在写事务**之前**）。
        """
        result = validate_skill(definition, creator)
        if not result.ok:
            first = result.issues[0]
            raise RepositoryError(
                first.code,
                f"skill definition rejected at {first.path}: {first.message}",
                path=first.path,
                skill_id=str(definition.get("skill_id", "")),
                issues=[issue.__dict__ for issue in result.issues],
            )

        skill_id = definition["skill_id"]
        version = int(definition["version"])
        definition_hash = sha256_of_text(canonical_json(definition))

        def op(conn) -> SkillRecord:
            existing = self._fetch_skill(conn, skill_id, version)
            if existing is not None:
                if existing.definition_hash != definition_hash:
                    # 与工作流复用同一条码：不变式完全一样（版本号绑死内容）
                    raise RepositoryError(
                        ErrorCode.WORKFLOW_VERSION_IMMUTABLE,
                        f"{skill_id} v{version} already exists with different content",
                        path="version",
                        skill_id=skill_id,
                        version=version,
                        registered_hash=existing.definition_hash,
                        incoming_hash=definition_hash,
                    )
                return existing

            stored = self._artifacts.put_json(definition)
            now = self._clock()
            conn.execute(
                f"INSERT INTO skills({_SKILL_COLUMNS}) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (skill_id, version, stored.sha256, stored.ref, STATUS_REGISTERED, now, None),
            )
            record = self._fetch_skill(conn, skill_id, version)
            assert record is not None
            return record

        return self._write(op)

    # -- 技能：读 ---------------------------------------------------------

    def get_skill(self, skill_id: str, version: int) -> SkillRecord:
        def op(conn) -> SkillRecord:
            record = self._fetch_skill(conn, skill_id, version)
            if record is None:
                raise RepositoryError(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    "no such skill version",
                    skill_id=skill_id,
                    version=version,
                )
            return record

        return self._read(op)

    def get_skill_definition(self, skill_id: str, version: int) -> dict[str, Any]:
        record = self.get_skill(skill_id, version)
        return self._verified_definition(record, kind="skill", key=skill_id)

    def list_skills(self, *, capability: str | None = None) -> tuple[SkillSummary, ...]:
        """列技能（可选按 capability 过滤）。

        **过滤在这里做，不在工具层做**：要判断一个技能是否提供某个能力，
        必须读它的定义；而读定义要过指纹校验 —— 那是本类的职责
        （`_artifacts` 归它持有）。把定义交给上层去筛，等于把
        "读 store 必过校验"这条规则漏到调用方手里。

        排序按 `(skill_id, version)`，稳定可比对。
        """
        rows = self._read(lambda conn: tuple(
            self._row_to_skill(row)
            for row in conn.execute(
                f"SELECT {_SKILL_COLUMNS} FROM skills ORDER BY skill_id ASC, version ASC"
            ).fetchall()
        ))

        summaries: list[SkillSummary] = []
        for record in rows:
            definition = self._verified_definition(
                record, kind="skill", key=record.skill_id
            )
            capabilities = tuple(definition["required_capabilities"])
            if capability is not None and capability not in capabilities:
                continue
            summaries.append(SkillSummary(
                skill_id=record.skill_id,
                version=record.version,
                description=definition["description"],
                required_capabilities=capabilities,
            ))
        return tuple(summaries)

    # -- 状态迁移 ---------------------------------------------------------

    def activate(self, workflow_id: str, version: int) -> WorkflowRecord:
        """人工激活闸门：`pending_activation` → `active`。"""
        return self._transition(
            workflow_id, version, to=STATUS_ACTIVE, allowed_from=(STATUS_PENDING_ACTIVATION,)
        )

    def deprecate(self, workflow_id: str, version: int) -> WorkflowRecord:
        """下架（不删除）：`active` / `pending_activation` → `deprecated`。"""
        return self._transition(
            workflow_id,
            version,
            to=STATUS_DEPRECATED,
            allowed_from=(STATUS_ACTIVE, STATUS_PENDING_ACTIVATION),
        )

    def _transition(
        self,
        workflow_id: str,
        version: int,
        *,
        to: str,
        allowed_from: tuple[str, ...],
    ) -> WorkflowRecord:
        def op(conn) -> WorkflowRecord:
            record = self._fetch(conn, workflow_id, version)
            if record is None:
                raise RepositoryError(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    "no such workflow version",
                    workflow_id=workflow_id,
                    version=version,
                )
            if record.status not in allowed_from:
                raise RepositoryError(
                    ErrorCode.SEMANTIC_INVALID,
                    f"cannot move {workflow_id} v{version} from {record.status} to {to}",
                    workflow_id=workflow_id,
                    version=version,
                    status=record.status,
                    target=to,
                    allowed_from=list(allowed_from),
                )
            now = self._clock()
            conn.execute(
                "UPDATE workflows SET status = ?, "
                "activated_at = COALESCE(?, activated_at) WHERE workflow_id = ? AND version = ?",
                (to, now if to == STATUS_ACTIVE else None, workflow_id, version),
            )
            updated = self._fetch(conn, workflow_id, version)
            assert updated is not None
            return updated

        return self._write(op)

    # -- 内部 -------------------------------------------------------------

    def _verified_definition(
        self,
        record: WorkflowRecord | SkillRecord,
        *,
        kind: str,
        key: str,
    ) -> dict[str, Any]:
        """从 store 读定义本体，并验证它与行内指纹一致。

        **工作流与技能共用这一份**：两道校验的顺序（内容寻址保证"文件 == 地址"，
        再比对行内指纹保证"地址 == 登记时那份"）是同一件事，
        抄两份的下场是只修好一份。
        """
        definition = self._artifacts.get_json(
            self._artifacts.parse_ref(record.definition_ref)
        )
        if sha256_of_text(canonical_json(definition)) != record.definition_hash:
            raise RepositoryError(
                ErrorCode.SCHEMA_INVALID,
                f"stored {kind} definition content does not match the recorded fingerprint",
                path="definition_ref",
                kind=kind,
                id=key,
                version=record.version,
                recorded_hash=record.definition_hash,
            )
        return definition

    @staticmethod
    def _fetch(conn, workflow_id: str, version: int) -> WorkflowRecord | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM workflows WHERE workflow_id = ? AND version = ?",
            (workflow_id, int(version)),
        ).fetchone()
        return None if row is None else WorkflowRegistry._row_to_record(row)

    @staticmethod
    def _fetch_skill(conn, skill_id: str, version: int) -> SkillRecord | None:
        row = conn.execute(
            f"SELECT {_SKILL_COLUMNS} FROM skills WHERE skill_id = ? AND version = ?",
            (skill_id, int(version)),
        ).fetchone()
        return None if row is None else WorkflowRegistry._row_to_skill(row)

    @staticmethod
    def _row_to_record(row) -> WorkflowRecord:
        return WorkflowRecord(
            workflow_id=row["workflow_id"],
            version=int(row["version"]),
            definition_hash=row["definition_hash"],
            definition_ref=row["definition_ref"],
            status=row["status"],
            trust_level=row["trust_level"],
            created_at=row["created_at"],
            activated_at=row["activated_at"],
        )

    @staticmethod
    def _row_to_skill(row) -> SkillRecord:
        return SkillRecord(
            skill_id=row["skill_id"],
            version=int(row["version"]),
            definition_hash=row["definition_hash"],
            definition_ref=row["definition_ref"],
            status=row["status"],
            created_at=row["created_at"],
            activated_at=row["activated_at"],
        )


def _auto_activates(definition: dict[str, Any], creator: dict[str, Any]) -> bool:
    """T3 + `activation: "auto"` 才免人工激活。

    校验层（L3）已经拦掉了"非 T3 却声明 auto"，这里只是照抄结论落状态。
    """
    return (
        definition.get("permissions", {}).get("activation") == "auto"
        and bool(creator.get("can_auto_activate"))
        and definition.get("permissions", {}).get("trust_level") == "T3"
    )


__all__ = [
    "WorkflowRegistry",
    "WorkflowRecord",
    "SkillRecord",
    "SkillSummary",
    "STATUS_PENDING_ACTIVATION",
    "STATUS_ACTIVE",
    "STATUS_DEPRECATED",
    "STATUS_REGISTERED",
    "STATUSES",
]
