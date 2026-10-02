import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ErrorCode, ValidationIssue, ValidationResult
from .pipeline import PIPELINE_STEPS


_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "workflow.schema.json"
_WORKFLOW_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_DRAFT_VALIDATOR = Draft202012Validator(_WORKFLOW_SCHEMA)

# 六步的权威顺序见 `validator/pipeline.py`（runtime 侧推游标用的是同一个元组）。
KNOWN_STEPS = PIPELINE_STEPS

# 能力清单的权威来源就是 `workflow.schema.json` 里那个 enum —— **从 schema 取，不复制**。
# 复制一份的代价是"加了一个 capability，schema 收、Python 不收"，
# 而且要等到有人真去用才现形。技能校验（`skill_validator.py`）用的是同一份词汇表。
KNOWN_CAPABILITIES = frozenset(
    _WORKFLOW_SCHEMA["properties"]["permissions"]["properties"]
    ["required_capabilities"]["items"]["enum"]
)

_TRUST_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}

# T0/T1 只有 system 身份可创建
_SYSTEM_ONLY_TRUST = {"T0", "T1"}

# L4 metadata 禁止的控制字段
_FORBIDDEN_METADATA_KEYS = frozenset({
    "workflow_override",
    "execution_command",
    "capability_grant",
    "trust_level",
    "budget_override",
    "pipeline",
    "permissions",
})


def validate_workflow(workflow: dict[str, Any], creator_context: dict[str, Any]) -> ValidationResult:
    """
    creator_context = {
      "identity": "system" | "human" | "agent",
      "is_system": bool,                     # 冗余但兼容旧调用
      "allowed_capabilities": ["llm.call", ...],
      "max_trust_level": "T0"|"T1"|"T2"|"T3",
      "can_auto_activate": bool
    }
    """
    issues: list[ValidationIssue] = []

    # ---------- Layer 1: Schema ----------
    for err in sorted(_DRAFT_VALIDATOR.iter_errors(workflow), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "<root>"
        issues.append(ValidationIssue(path, ErrorCode.SCHEMA_INVALID.value, err.message))
    if issues:
        return ValidationResult.fail(issues)

    # ---------- Layer 2: Semantic ----------
    _validate_semantic(workflow, issues)

    # ---------- Layer 3: Trust / Policy ----------
    _validate_trust_policy(workflow, creator_context, issues)

    # ---------- Layer 4: Metadata ----------
    _validate_metadata(workflow, issues)

    if issues:
        return ValidationResult.fail(issues)
    return ValidationResult.pass_()


# ---------------------------------------------------------------------------
# Layer 2
# ---------------------------------------------------------------------------

def _validate_semantic(wf: dict, issues: list[ValidationIssue]) -> None:
    pipeline = wf["pipeline"]

    # 2.1 六步显式声明（schema 已保证，显式再检查便于诊断）
    for step in KNOWN_STEPS:
        if step not in pipeline:
            issues.append(ValidationIssue(
                f"pipeline/{step}", ErrorCode.SEMANTIC_INVALID.value,
                f"pipeline.{step} must be explicitly declared",
            ))

    # 2.2 understand / deliver 必须 required
    if pipeline.get("understand") != "required":
        issues.append(ValidationIssue(
            "pipeline/understand", ErrorCode.SEMANTIC_INVALID.value,
            "understand must be 'required'",
        ))
    if pipeline.get("deliver") != "required":
        issues.append(ValidationIssue(
            "pipeline/deliver", ErrorCode.SEMANTIC_INVALID.value,
            "deliver must be 'required'",
        ))

    # 2.3 required 步骤 >= 3
    required_steps = [s for s in KNOWN_STEPS if pipeline.get(s) == "required"]
    if len(required_steps) < 3:
        issues.append(ValidationIssue(
            "pipeline", ErrorCode.SEMANTIC_INVALID.value,
            f"at least 3 required steps needed, got {len(required_steps)}",
        ))

    # 2.4 evaluate required → refine != disabled
    if pipeline.get("evaluate") == "required" and pipeline.get("refine") == "disabled":
        issues.append(ValidationIssue(
            "pipeline/refine", ErrorCode.SEMANTIC_INVALID.value,
            "refine cannot be 'disabled' when evaluate is 'required'",
        ))

    # 2.5 step_overrides 每步必须声明 cancellable
    overrides = wf.get("step_overrides", {})
    for step in KNOWN_STEPS:
        if step not in overrides:
            issues.append(ValidationIssue(
                f"step_overrides/{step}", ErrorCode.SEMANTIC_INVALID.value,
                f"step_overrides.{step} must be declared",
            ))
        elif "cancellable" not in overrides[step]:
            issues.append(ValidationIssue(
                f"step_overrides/{step}/cancellable", ErrorCode.SEMANTIC_INVALID.value,
                "cancellable must be declared for every step",
            ))


# ---------------------------------------------------------------------------
# Layer 3 — 身份、权限、信任、策略分离
# ---------------------------------------------------------------------------

def _validate_trust_policy(wf: dict, creator: dict, issues: list[ValidationIssue]) -> None:
    identity = creator.get("identity", "agent")
    is_system = bool(creator.get("is_system", False)) or identity == "system"
    creator_max_trust = creator.get("max_trust_level", "T2")
    can_auto = bool(creator.get("can_auto_activate", False))
    allowed_caps = set(creator.get("allowed_capabilities", []))

    declared_trust = wf["permissions"]["trust_level"]
    activation = wf["permissions"]["activation"]
    needed_caps = set(wf["permissions"]["required_capabilities"])

    # (a) T0/T1 只有 system 身份可声明
    if not is_system and declared_trust in _SYSTEM_ONLY_TRUST:
        issues.append(ValidationIssue(
            "permissions/trust_level", ErrorCode.INSUFFICIENT_TRUST.value,
            f"non-system creator cannot declare {declared_trust}",
        ))

    # (b) declared_trust 不得超过 creator 的 max_trust_level
    if _TRUST_RANK[declared_trust] > _TRUST_RANK[creator_max_trust]:
        issues.append(ValidationIssue(
            "permissions/trust_level", ErrorCode.INSUFFICIENT_TRUST.value,
            f"declared {declared_trust} exceeds creator max {creator_max_trust}",
        ))

    # (c) required_capabilities ⊆ creator.allowed_capabilities
    missing = needed_caps - allowed_caps
    if missing:
        issues.append(ValidationIssue(
            "permissions/required_capabilities", ErrorCode.CAPABILITY_DENIED.value,
            f"creator lacks capabilities: {sorted(missing)}",
        ))

    # (d) activation 策略：auto 必须 T3 + can_auto + creator 达到 T3
    if activation == "auto":
        if declared_trust != "T3":
            issues.append(ValidationIssue(
                "permissions/activation", ErrorCode.INSUFFICIENT_TRUST.value,
                f"activation='auto' requires trust_level='T3', got {declared_trust}",
            ))
        if not can_auto:
            issues.append(ValidationIssue(
                "permissions/activation", ErrorCode.INSUFFICIENT_TRUST.value,
                "creator does not have auto-activation permission",
            ))
        if _TRUST_RANK[creator_max_trust] < _TRUST_RANK["T3"]:
            issues.append(ValidationIssue(
                "permissions/activation", ErrorCode.INSUFFICIENT_TRUST.value,
                f"creator max trust {creator_max_trust} < T3 for auto activation",
            ))

    # (e) T1 必须 human_required
    if declared_trust == "T1" and activation != "human_required":
        issues.append(ValidationIssue(
            "permissions/activation", ErrorCode.INSUFFICIENT_TRUST.value,
            "T1 workflows must use human_required activation",
        ))


# ---------------------------------------------------------------------------
# Layer 4 — Metadata 禁止控制字段
# ---------------------------------------------------------------------------

def _validate_metadata(wf: dict, issues: list[ValidationIssue]) -> None:
    metadata = wf.get("metadata") or {}
    _scan_metadata(metadata, "", issues)


def validate_metadata(metadata: Any, *, prefix: str = "metadata") -> ValidationResult:
    """扫描**任意**一段 metadata 载荷（不限于工作流定义里的那份）。

    `execute_workflow` 的 `metadata` 入参走的就是这一条：契约 §三 把
    "metadata 只能描述、不能改变执行"列为对 Agent 的普遍禁令，
    而那是**每次调用**都要成立的性质，不是只有登记定义时才成立 ——
    只在登记时扫，等于允许"定义干净、调用时塞 `permissions`"。

    `prefix` 只影响错误里的 `path`，默认 `metadata` 与工作流定义里的叫法一致。
    """
    issues: list[ValidationIssue] = []
    _scan_metadata(metadata, "", issues, prefix=prefix)
    return ValidationResult.fail(issues) if issues else ValidationResult.pass_()


def _scan_metadata(
    node: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    prefix: str = "metadata",
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_METADATA_KEYS:
                full = f"{prefix}{path}/{key}" if path else f"{prefix}/{key}"
                issues.append(ValidationIssue(
                    full, ErrorCode.METADATA_FORBIDDEN.value,
                    f"metadata key '{key}' is a forbidden control field",
                ))
            _scan_metadata(value, f"{path}/{key}" if path else f"/{key}", issues, prefix=prefix)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _scan_metadata(item, f"{path}[{i}]", issues, prefix=prefix)
