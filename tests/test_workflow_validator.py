import json
from pathlib import Path

import pytest

from validator.workflow_validator import validate_workflow


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "workflow"


# 每个 fixture 的 creator_context。缺省回退到 DEFAULT_CTX。
CREATOR_CONTEXTS: dict[str, dict] = {
    "valid_t3_auto.json": {
        "identity": "agent",
        "is_system": False,
        "allowed_capabilities": ["llm.call", "file.read", "file.write"],
        "max_trust_level": "T3",
        "can_auto_activate": True,
    },
    "invalid_trust_t0_agent.json": {
        "identity": "agent",
        "is_system": False,
        "allowed_capabilities": ["llm.call"],
        "max_trust_level": "T2",
        "can_auto_activate": False,
    },
    "invalid_capability_denied.json": {
        "identity": "agent",
        "is_system": False,
        # 故意不给 http.call
        "allowed_capabilities": ["llm.call"],
        "max_trust_level": "T2",
        "can_auto_activate": False,
    },
    "invalid_activation_permission.json": {
        "identity": "agent",
        "is_system": False,
        "allowed_capabilities": ["llm.call"],
        "max_trust_level": "T2",
        "can_auto_activate": False,
    },
}

DEFAULT_CTX = {
    "identity": "agent",
    "is_system": False,
    "allowed_capabilities": ["llm.call", "file.read", "file.write"],
    "max_trust_level": "T2",
    "can_auto_activate": False,
}


def _load_manifest() -> list[dict]:
    return json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))["fixtures"]


@pytest.mark.parametrize("entry", _load_manifest(), ids=lambda e: e["file"])
def test_fixture(entry: dict) -> None:
    wf = json.loads((FIXTURE_DIR / entry["file"]).read_text(encoding="utf-8"))
    ctx = CREATOR_CONTEXTS.get(entry["file"], DEFAULT_CTX)
    result = validate_workflow(wf, ctx)
    expected = entry["expected"]

    if expected == "PASS":
        assert result.ok, (
            f"{entry['file']} expected PASS, got issues: "
            f"{[i.code + ':' + i.path for i in result.issues]}"
        )
    else:
        assert not result.ok, f"{entry['file']} expected FAIL with {expected}"
        codes = {i.code for i in result.issues}
        assert expected in codes, (
            f"{entry['file']} expected code {expected}, got {codes}"
        )


def test_invalid_activation_permission_passes_schema_but_fails_policy() -> None:
    """确认三层模型分离：合法 JSON Schema 也可能被 L3 拦截。"""
    wf = json.loads(
        (FIXTURE_DIR / "invalid_activation_permission.json").read_text(encoding="utf-8")
    )
    result = validate_workflow(wf, DEFAULT_CTX)
    assert not result.ok
    codes = {i.code for i in result.issues}
    # 必须只有 L3 错误，没有 L1 schema 错误
    assert "SCHEMA_INVALID" not in codes
    assert "INSUFFICIENT_TRUST" in codes


def test_invalid_metadata_scanned_recursively() -> None:
    wf = json.loads(
        (FIXTURE_DIR / "invalid_metadata_control_field.json").read_text(encoding="utf-8")
    )
    result = validate_workflow(wf, DEFAULT_CTX)
    codes = {i.code for i in result.issues}
    assert "METADATA_FORBIDDEN" in codes


def test_valid_t3_auto_requires_creator_max_t3() -> None:
    """T3 只有在 creator max_trust 达 T3 且 can_auto_activate=true 时才 PASS。"""
    wf = json.loads(
        (FIXTURE_DIR / "valid_t3_auto.json").read_text(encoding="utf-8")
    )
    # 场景一：creator max_trust = T2 → 应失败
    bad_ctx = dict(DEFAULT_CTX, max_trust_level="T2")
    r1 = validate_workflow(wf, bad_ctx)
    assert not r1.ok
    assert any(i.code == "INSUFFICIENT_TRUST" for i in r1.issues)

    # 场景二：creator max_trust = T3，can_auto_activate = True → 应通过
    good_ctx = dict(DEFAULT_CTX, max_trust_level="T3", can_auto_activate=True)
    r2 = validate_workflow(wf, good_ctx)
    assert r2.ok, [i.code + ":" + i.path for i in r2.issues]
