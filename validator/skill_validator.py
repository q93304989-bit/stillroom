"""技能（原子能力）定义的校验。

契约 `contracts/mcp-tools.md` §二.3：入参
`{skill_id, version, description, required_capabilities, io_contract}`；
约束 `required_capabilities ⊆ creator_context.allowed_capabilities`，
否则 `CAPABILITY_DENIED`（L3）。

## 为什么没有 `skill.schema.json`

工作流有 schema 文件，技能没有 —— 这是**有意的**，不是漏了。

技能的字段少且稳定（5 个），为它造第四份**冻结** schema 的收益不如把校验写在代码里：
可读、可测、改的时候不用同步两份。技能形状一旦开始演化（版本化 `io_contract`、
参数多态），再提升为 schema 文件，那时"冻结"才有意义。

## 按"层"分级，与 `workflow_validator` 一致

L1 形状 → L3 信任/能力。技能没有 L2（语义）与 L4（metadata）的对应物：
它没有 pipeline、也没有 metadata 这种"能影响执行的描述性载荷"。
**不为了对称而造空层** —— 空层会让人以为"技能也有 metadata 限制"。
"""

from __future__ import annotations

from typing import Any

from .errors import ErrorCode, ValidationIssue, ValidationResult
from .workflow_validator import KNOWN_CAPABILITIES

_FIELDS = ("skill_id", "version", "description", "required_capabilities", "io_contract")


def validate_skill(
    definition: dict[str, Any],
    creator_context: dict[str, Any],
) -> ValidationResult:
    """校验一个技能定义。`creator_context` 的形状同 `validate_workflow`。"""
    issues: list[ValidationIssue] = []

    # ---------- L1 形状 ----------
    if not isinstance(definition, dict):
        return ValidationResult.fail([
            ValidationIssue("<root>", ErrorCode.SCHEMA_INVALID.value, "skill must be an object")
        ])

    for key in definition:
        if key not in _FIELDS:
            issues.append(ValidationIssue(
                "<root>", ErrorCode.SCHEMA_INVALID.value,
                f"unexpected field {key!r}; a skill has exactly {list(_FIELDS)}",
            ))

    _require_text(definition, "skill_id", issues)
    _require_version(definition, issues)
    _require_text(definition, "description", issues)
    _require_capability_list(definition, issues)

    if "io_contract" in definition and not isinstance(definition["io_contract"], dict):
        issues.append(ValidationIssue(
            "io_contract", ErrorCode.SCHEMA_INVALID.value,
            f"io_contract must be an object, got {type(definition['io_contract']).__name__}",
        ))

    if issues:
        return ValidationResult.fail(issues)

    # ---------- L3 信任 / 能力 ----------
    _validate_capabilities(definition, creator_context, issues)

    if issues:
        return ValidationResult.fail(issues)
    return ValidationResult.pass_()


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------

def _require_text(definition: dict, key: str, issues: list[ValidationIssue]) -> None:
    value = definition.get(key)
    if not isinstance(value, str) or not value.strip():
        issues.append(ValidationIssue(
            key, ErrorCode.SCHEMA_INVALID.value,
            f"{key} must be a non-empty string, got {value!r}",
        ))


def _require_version(definition: dict, issues: list[ValidationIssue]) -> None:
    value = definition.get("version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        issues.append(ValidationIssue(
            "version", ErrorCode.SCHEMA_INVALID.value,
            f"version must be a positive integer, got {value!r}",
        ))


def _require_capability_list(definition: dict, issues: list[ValidationIssue]) -> None:
    value = definition.get("required_capabilities")
    if not isinstance(value, list):
        issues.append(ValidationIssue(
            "required_capabilities", ErrorCode.SCHEMA_INVALID.value,
            f"required_capabilities must be an array, got {type(value).__name__}",
        ))
        return
    for item in value:
        if item not in KNOWN_CAPABILITIES:
            issues.append(ValidationIssue(
                "required_capabilities", ErrorCode.SCHEMA_INVALID.value,
                f"unknown capability {item!r}; known: {sorted(KNOWN_CAPABILITIES)}",
            ))
    if len(set(map(str, value))) != len(value):
        issues.append(ValidationIssue(
            "required_capabilities", ErrorCode.SCHEMA_INVALID.value,
            "required_capabilities must not contain duplicates",
        ))


# ---------------------------------------------------------------------------
# L3
# ---------------------------------------------------------------------------

def _validate_capabilities(
    definition: dict,
    creator: dict[str, Any],
    issues: list[ValidationIssue],
) -> None:
    """技能声明的能力必须 ⊆ creator 被授予的能力 —— 与工作流同一条规则。

    **不默认放行**：`allowed_capabilities` 缺失时按空集处理。
    若按"缺失 = 全部允许"，那"忘了配能力清单"就会静默变成"什么都能干",
    与 L3 存在的理由正好相反。
    """
    allowed = set(creator.get("allowed_capabilities") or ())
    requested = definition["required_capabilities"]
    denied = sorted(set(requested) - allowed)
    if denied:
        issues.append(ValidationIssue(
            "required_capabilities", ErrorCode.CAPABILITY_DENIED.value,
            f"creator is not allowed to declare {denied}; "
            f"allowed: {sorted(allowed)}",
        ))


__all__ = ["validate_skill"]
