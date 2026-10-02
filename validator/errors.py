from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    # Layer 1
    SCHEMA_INVALID = "SCHEMA_INVALID"
    # Layer 2
    SEMANTIC_INVALID = "SEMANTIC_INVALID"
    # Layer 3
    INSUFFICIENT_TRUST = "INSUFFICIENT_TRUST"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    # Layer 4
    METADATA_FORBIDDEN = "METADATA_FORBIDDEN"

    # State machine
    INVALID_TRANSITION = "INVALID_TRANSITION"
    EXECUTION_NOT_FOUND = "EXECUTION_NOT_FOUND"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_WAITING_INPUT = "NOT_WAITING_INPUT"

    # Idempotency / retry
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    RETRY_NOT_ALLOWED_FOR_COMPLETED = "RETRY_NOT_ALLOWED_FOR_COMPLETED"
    RETRY_BUDGET_NOT_RAISED = "RETRY_BUDGET_NOT_RAISED"
    RETRY_RACE_LOST = "RETRY_RACE_LOST"
    ORIGINAL_NOT_FOUND = "ORIGINAL_NOT_FOUND"
    NOT_TERMINAL = "NOT_TERMINAL"

    # Workflow
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    WORKFLOW_NOT_ACTIVE = "WORKFLOW_NOT_ACTIVE"
    WORKFLOW_VERSION_IMMUTABLE = "WORKFLOW_VERSION_IMMUTABLE"
    INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"

    # Budget / Matching
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MATCH_TIMEOUT = "MATCH_TIMEOUT"


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()

    @classmethod
    def pass_(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def fail(cls, issues) -> "ValidationResult":
        return cls(ok=False, issues=tuple(issues))
