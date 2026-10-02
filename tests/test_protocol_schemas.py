"""附加守护：三个 schema 都要是真的 Draft 2020-12，且样例要能过。

`workflow.schema.json` 已由 test_workflow_validator.py 间接覆盖；
这里补上另外两份（execution-event / artifact-manifest）—— 它们没有校验器，
只有契约，所以至少要保证 schema 自身合法、且样例文档能通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"

SCHEMA_FILES = (
    "workflow.schema.json",
    "execution-event.schema.json",
    "artifact-manifest.schema.json",
)

_HEX64 = "0" * 64


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(_load(name))


def test_all_three_schemas_pin_version_1_0() -> None:
    for name in SCHEMA_FILES:
        assert _load(name)["properties"]["schema_version"]["const"] == "1.0", name


def test_execution_event_sample_validates() -> None:
    schema = _load("execution-event.schema.json")
    sample = {
        "schema_version": "1.0",
        "event_id": "evt_0001",
        "execution_id": "exec_0001",
        "request_id": "req_0001",
        "seq": 7,
        "type": "engine_step_ended",
        "at": "2026-09-29T09:00:00Z",
        "status_before": "ABORT_PENDING",
        "status_after": "ABORTED",
        "step": "generate",
        "payload_ref": "artifacts/exec_0001/step_generate.json",
        "payload_hash": _HEX64,
    }
    assert not list(Draft202012Validator(schema).iter_errors(sample))


def test_execution_event_rejects_unknown_status_and_step() -> None:
    schema = _load("execution-event.schema.json")
    validator = Draft202012Validator(schema)
    base = {
        "schema_version": "1.0",
        "event_id": "evt_0002",
        "execution_id": "exec_0001",
        "seq": 8,
        "type": "engine_started",
        "at": "2026-09-29T09:00:01Z",
        "status_after": "RUNNING",
    }
    assert not list(validator.iter_errors(base))
    assert list(validator.iter_errors(dict(base, status_after="DONE")))
    assert list(validator.iter_errors(dict(base, step="polish")))
    # 未声明的额外字段必须被拒（additionalProperties: false）
    assert list(validator.iter_errors(dict(base, rogue_control="trust_level=T3")))


def test_artifact_manifest_sample_validates() -> None:
    schema = _load("artifact-manifest.schema.json")
    sample = {
        "schema_version": "1.0",
        "artifact_id": "art_0001",
        "execution_id": "exec_0001",
        "workflow_id": "article_generation",
        "workflow_version": 3,
        "step": "generate",
        "kind": "image",
        "mime": "image/png",
        "uri": "media/20260929-090000-abcdef.png",
        "sha256": _HEX64,
        "bytes": 182734,
        "created_at": "2026-09-29T09:00:00Z",
    }
    assert not list(Draft202012Validator(schema).iter_errors(sample))


def test_artifact_manifest_requires_hash_and_size() -> None:
    schema = _load("artifact-manifest.schema.json")
    validator = Draft202012Validator(schema)
    incomplete = {
        "schema_version": "1.0",
        "artifact_id": "art_0002",
        "execution_id": "exec_0001",
        "step": "deliver",
        "kind": "text",
        "uri": "artifacts/exec_0001/final.md",
        "created_at": "2026-09-29T09:00:00Z",
    }
    errors = list(validator.iter_errors(incomplete))
    required_missing: set[str] = set()
    for err in errors:
        if err.validator == "required":
            required_missing.update(err.validator_value)
    assert "sha256" in required_missing
    assert "bytes" in required_missing
