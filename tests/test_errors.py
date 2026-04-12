"""Tests for the formalised L3 error envelope.

Every envelope produced anywhere in the codebase must validate against
`spec/errors.schema.json`. To enforce that, the tests here:

1. Drive each layer into a failure state and validate the envelope
   returned by the MCP dispatch adapter against the schema.
2. Cover the ResilientWriteError factory classmethods and
   is_retriable() helper directly.
3. Catch drift between the in-code SCHEMA_VERSION and the on-disk
   schema's declared version.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from resilient_write import scratchpad, server
from resilient_write.errors import (
    ALL_ERROR_KINDS,
    ALL_REASON_HINTS,
    ALL_SUGGESTED_ACTIONS,
    SCHEMA_VERSION,
    ResilientWriteError,
    load_envelope_schema,
    validate_envelope,
)


# ---------------------------------------------------------------------------
# Schema + SCHEMA_VERSION coherence
# ---------------------------------------------------------------------------


def test_schema_file_declared_version_matches_code() -> None:
    schema = load_envelope_schema()
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION


def test_schema_enumerates_all_error_kinds() -> None:
    schema = load_envelope_schema()
    assert set(schema["properties"]["error"]["enum"]) == set(ALL_ERROR_KINDS)


def test_schema_enumerates_all_reason_hints() -> None:
    schema = load_envelope_schema()
    assert set(schema["properties"]["reason_hint"]["enum"]) == set(
        ALL_REASON_HINTS
    )


def test_schema_enumerates_all_suggested_actions() -> None:
    schema = load_envelope_schema()
    assert set(schema["properties"]["suggested_action"]["enum"]) == set(
        ALL_SUGGESTED_ACTIONS
    )


# ---------------------------------------------------------------------------
# Direct to_envelope() + validation
# ---------------------------------------------------------------------------


def test_minimal_envelope_validates() -> None:
    err = ResilientWriteError("policy_violation", "permission")
    env = err.to_envelope()
    validate_envelope(env)
    assert env["ok"] is False
    assert env["schema_version"] == SCHEMA_VERSION
    assert env["detected_patterns"] == []
    assert env["retry_budget"] == 0
    assert env["context"] == {}


def test_full_envelope_validates() -> None:
    err = ResilientWriteError(
        "blocked",
        "content_filter",
        suggested_action="redact",
        detected_patterns=["api_key", "github_pat"],
        retry_budget=2,
        context={"path": "x.tex", "score": 0.82, "verdict": "high"},
    )
    validate_envelope(err.to_envelope())


def test_envelope_round_trips_through_json() -> None:
    err = ResilientWriteError(
        "stale_precondition",
        "unknown",
        context={"path": "a.txt", "expected_prev_sha256": "deadbeef"},
    )
    payload = json.dumps(err.to_envelope())
    reloaded = json.loads(payload)
    validate_envelope(reloaded)
    assert reloaded["error"] == "stale_precondition"


# ---------------------------------------------------------------------------
# Envelope rejection: schema catches malformed payloads
# ---------------------------------------------------------------------------


def test_schema_rejects_unknown_error_kind() -> None:
    bad = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "error": "not_a_real_kind",
        "reason_hint": "unknown",
        "detected_patterns": [],
        "suggested_action": "abort",
        "retry_budget": 0,
        "context": {},
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_envelope(bad)


def test_schema_rejects_ok_true() -> None:
    bad = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "error": "blocked",
        "reason_hint": "content_filter",
        "detected_patterns": [],
        "suggested_action": "redact",
        "retry_budget": 0,
        "context": {},
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_envelope(bad)


def test_schema_rejects_missing_required_field() -> None:
    bad = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "error": "blocked",
        # reason_hint missing
        "detected_patterns": [],
        "suggested_action": "redact",
        "retry_budget": 0,
        "context": {},
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_envelope(bad)


# ---------------------------------------------------------------------------
# Factory classmethods
# ---------------------------------------------------------------------------


def test_factory_blocked() -> None:
    err = ResilientWriteError.blocked(
        detected_patterns=["api_key"],
        retry_budget=3,
        context={"path": "x"},
    )
    env = err.to_envelope()
    validate_envelope(env)
    assert env["error"] == "blocked"
    assert env["reason_hint"] == "content_filter"
    assert env["suggested_action"] == "redact"
    assert env["detected_patterns"] == ["api_key"]
    assert env["retry_budget"] == 3


def test_factory_stale_precondition() -> None:
    err = ResilientWriteError.stale_precondition(context={"path": "a.txt"})
    env = err.to_envelope()
    validate_envelope(env)
    assert env["error"] == "stale_precondition"
    assert env["suggested_action"] == "ask_user"


def test_factory_write_corruption() -> None:
    err = ResilientWriteError.write_corruption(context={"path": "a.txt"})
    env = err.to_envelope()
    validate_envelope(env)
    assert env["error"] == "write_corruption"
    assert env["suggested_action"] == "abort"


def test_factory_policy_violation() -> None:
    err = ResilientWriteError.policy_violation(
        reason_hint="encoding", context={"reason": "bad_input"}
    )
    env = err.to_envelope()
    validate_envelope(env)
    assert env["error"] == "policy_violation"
    assert env["reason_hint"] == "encoding"


def test_factory_quota_exceeded() -> None:
    err = ResilientWriteError.quota_exceeded(context={"errno": 28})
    env = err.to_envelope()
    validate_envelope(env)
    assert env["error"] == "quota_exceeded"
    assert env["suggested_action"] == "split"


# ---------------------------------------------------------------------------
# is_retriable heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason_hint,expected",
    [
        ("content_filter", False),
        ("permission", False),
        ("unknown", False),
        ("encoding", False),
        ("network", True),
        ("size_limit", True),
    ],
)
def test_is_retriable(reason_hint: str, expected: bool) -> None:
    err = ResilientWriteError("policy_violation", reason_hint)  # type: ignore[arg-type]
    assert err.is_retriable() is expected


# ---------------------------------------------------------------------------
# Every layer's failure envelope validates end-to-end via the dispatcher
# ---------------------------------------------------------------------------


def _fail(tool: str, arguments: dict, tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    env = server._envelope_or_error(tool, arguments)
    assert env["ok"] is False, f"{tool} unexpectedly succeeded: {env}"
    validate_envelope(env)
    return env


def test_l1_path_traversal_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail(
        "rw.safe_write",
        {"path": "../escape.txt", "content": "x"},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "policy_violation"
    assert env["reason_hint"] == "permission"


def test_l1_create_over_existing_envelope(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("old")
    env = _fail(
        "rw.safe_write",
        {"path": "a.txt", "content": "new"},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "stale_precondition"


def test_l0_classify_block_envelope(tmp_path: Path, monkeypatch) -> None:
    draft = (
        "sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "gho_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD\n"
    )
    env = _fail(
        "rw.safe_write",
        {"path": "appendix.txt", "content": draft, "classify": True},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "blocked"
    assert env["reason_hint"] == "content_filter"
    assert "api_key" in env["detected_patterns"]


def test_l2_non_contiguous_envelope(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    server._envelope_or_error(
        "rw.chunk_write",
        {"session": "s", "index": 1, "content": "a", "total_expected": 3},
    )
    server._envelope_or_error(
        "rw.chunk_write",
        {"session": "s", "index": 3, "content": "c", "total_expected": 3},
    )
    env = _fail(
        "rw.chunk_compose",
        {"session": "s", "output_path": "out.txt"},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "stale_precondition"
    assert env["context"]["reason"] == "non_contiguous_chunks"


def test_l2_invalid_session_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail(
        "rw.chunk_write",
        {"session": "bad name", "index": 1, "content": "x"},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "policy_violation"


def test_l4_missing_scratch_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail(
        "rw.scratch_get",
        {"sha256": "a" * 64},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "stale_precondition"


def test_l4_get_disabled_envelope(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    put = server._envelope_or_error("rw.scratch_put", {"content": "x\n"})
    monkeypatch.setenv(scratchpad.DISABLE_GET_ENV, "1")
    env = _fail(
        "rw.scratch_get",
        {"sha256": put["sha256"]},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "policy_violation"
    assert env["reason_hint"] == "permission"


def test_l4_bad_base64_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail(
        "rw.scratch_put",
        {"content": "not valid base64 !!", "encoding": "base64"},
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "policy_violation"
    assert env["reason_hint"] == "encoding"


def test_l5_missing_required_field_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail(
        "rw.handoff_write",
        {"envelope": {"task_id": "x"}},  # missing status, agent, ...
        tmp_path,
        monkeypatch,
    )
    assert env["error"] == "policy_violation"
    assert env["reason_hint"] == "encoding"
    assert "missing_fields" in env["context"]


def test_l5_read_missing_file_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail("rw.handoff_read", {}, tmp_path, monkeypatch)
    assert env["error"] == "stale_precondition"


def test_unknown_tool_envelope(tmp_path: Path, monkeypatch) -> None:
    env = _fail("rw.does_not_exist", {}, tmp_path, monkeypatch)
    assert env["error"] == "policy_violation"
    assert env["context"]["unknown_tool"] == "rw.does_not_exist"
