"""无头运行时（P1）。

依赖方向：`mcp_server/ → runtime/ → validator/ → schemas/`。
本包不得 import PySide6 或 `app.*`，由 `tests/test_headless_boundary.py` 强制。

一个物理库（`protocol.db`）、多个逻辑域，各域一个类：

| 域 | 类 | 模块 |
|---|---|---|
| 协议（executions / events） | `ExecutionRepository` | `runtime/repository.py` |
| 注册（workflows / skills） | `WorkflowRegistry` | `runtime/registry.py` |
| 幂等锚点（request_bindings） | 由 `ExecutionRepository` 负责 | `runtime/repository.py` |

各域**共用 `db_path` 但各开连接**，所以域之间没有跨域事务 —— 这是有意的
（注册成功而这次执行失败，正是期望结果）。共用的事务纪律在 `runtime/base.py`。
"""

from .artifacts import ArtifactStore, StoredArtifact
from .base import DomainDatabase
from .errors import KernelError, RepositoryError, StillroomRuntimeError, error_code
from .hashing import canonical_json, sha256_of_text
from .registry import (
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_PENDING_ACTIVATION,
    STATUS_REGISTERED,
    STATUSES,
    SkillRecord,
    SkillSummary,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowSummary,
)
from .repository import (
    DEFAULT_DB_FILENAME,
    SCHEMA_VERSION,
    BindResult,
    EventRecord,
    ExecutionRecord,
    ExecutionRepository,
    ReplayOutcome,
)
from .schema import (
    BINDINGS_VERSION,
    PROTOCOL_VERSION,
    REGISTRY_VERSION,
    SCOPE_BINDINGS,
    SCOPE_PROTOCOL,
    SCOPE_REGISTRY,
)
from .router import MatchFn, stub_match
from .stub_kernel import StepOutcome, StubKernel

__all__ = [
    # 基础设施
    "DomainDatabase",
    "ArtifactStore",
    "StoredArtifact",
    "PROTOCOL_VERSION",
    "REGISTRY_VERSION",
    "BINDINGS_VERSION",
    "SCOPE_PROTOCOL",
    "SCOPE_REGISTRY",
    "SCOPE_BINDINGS",
    # 错误
    "StillroomRuntimeError",
    "RepositoryError",
    "KernelError",
    "error_code",
    # 协议域
    "ExecutionRepository",
    "ExecutionRecord",
    "EventRecord",
    "BindResult",
    "ReplayOutcome",
    "SCHEMA_VERSION",
    "DEFAULT_DB_FILENAME",
    # 注册域
    "WorkflowRegistry",
    "WorkflowRecord",
    "WorkflowSummary",
    "SkillRecord",
    "SkillSummary",
    "STATUS_PENDING_ACTIVATION",
    "STATUS_ACTIVE",
    "STATUS_DEPRECATED",
    "STATUS_REGISTERED",
    "STATUSES",
    # 路由
    "MatchFn",
    "stub_match",
    # 内核
    "StubKernel",
    "StepOutcome",
    # 工具
    "canonical_json",
    "sha256_of_text",
]
