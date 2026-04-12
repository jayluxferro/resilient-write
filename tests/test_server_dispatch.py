"""Tests for the MCP tool dispatch adapter.

Exercises `_envelope_or_error` directly — the function the MCP
`call_tool` handler delegates to — so we don't need a live stdio
session. This keeps the test synchronous while still covering the
"typed error -> envelope payload" path.
"""

from __future__ import annotations

from pathlib import Path

from resilient_write import server


def test_dispatch_safe_write_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    out = server._envelope_or_error(
        "rw.safe_write", {"path": "a.txt", "content": "hi\n"}
    )
    assert out["ok"] is True
    assert out["path"] == "a.txt"
    assert (tmp_path / "a.txt").read_text() == "hi\n"


def test_dispatch_returns_typed_envelope_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    out = server._envelope_or_error(
        "rw.safe_write", {"path": "../escape.txt", "content": "x"}
    )
    assert out["ok"] is False
    assert out["error"] == "policy_violation"
    assert out["reason_hint"] == "permission"
    assert out["context"]["tool"] == "rw.safe_write"


def test_dispatch_unknown_tool_returns_envelope(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    out = server._envelope_or_error("rw.nope", {})
    assert out["ok"] is False
    assert out["error"] == "policy_violation"
    assert out["context"]["unknown_tool"] == "rw.nope"


def test_dispatch_journal_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    server._envelope_or_error(
        "rw.safe_write", {"path": "a.txt", "content": "1\n"}
    )
    server._envelope_or_error(
        "rw.safe_write", {"path": "b.txt", "content": "2\n"}
    )
    out = server._envelope_or_error("rw.journal_tail", {"n": 5})
    assert out["ok"] is True
    assert len(out["entries"]) == 2
    assert {e["path"] for e in out["entries"]} == {"a.txt", "b.txt"}


def test_dispatch_chunk_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    for i, piece in enumerate(["one ", "two ", "three"], start=1):
        out = server._envelope_or_error(
            "rw.chunk_write",
            {
                "session": "demo",
                "index": i,
                "content": piece,
                "total_expected": 3,
            },
        )
        assert out["ok"] is True

    status = server._envelope_or_error(
        "rw.chunk_status", {"session": "demo"}
    )
    assert status["present_indices"] == [1, 2, 3]

    compose = server._envelope_or_error(
        "rw.chunk_compose",
        {"session": "demo", "output_path": "out.txt", "cleanup": True},
    )
    assert compose["ok"] is True
    assert compose["chunk_count"] == 3
    assert (tmp_path / "out.txt").read_text() == "one two three"

    reset = server._envelope_or_error("rw.chunk_reset", {"session": "demo"})
    assert reset["ok"] is True
    assert reset["existed"] is False  # compose cleanup already wiped it


def test_dispatch_chunk_invalid_session_returns_envelope(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    out = server._envelope_or_error(
        "rw.chunk_write",
        {"session": "has space", "index": 1, "content": "x"},
    )
    assert out["ok"] is False
    assert out["error"] == "policy_violation"


def test_dispatch_chunk_append(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    out1 = server._envelope_or_error(
        "rw.chunk_append", {"session": "draft", "content": "section 1\n"}
    )
    assert out1["ok"] is True
    assert out1["index"] == 1

    out2 = server._envelope_or_error(
        "rw.chunk_append", {"session": "draft", "content": "section 2\n"}
    )
    assert out2["index"] == 2

    compose = server._envelope_or_error(
        "rw.chunk_compose",
        {"session": "draft", "output_path": "draft.txt"},
    )
    assert compose["ok"] is True
    assert (tmp_path / "draft.txt").read_text() == "section 1\nsection 2\n"


def test_dispatch_risk_score(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    out = server._envelope_or_error(
        "rw.risk_score",
        {"content": "hello, just a plain note\n"},
    )
    assert out["ok"] is True
    assert out["verdict"] == "safe"
    assert out["detected_patterns"] == []


def test_dispatch_scratchpad_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    put = server._envelope_or_error(
        "rw.scratch_put",
        {"content": "raw material\n", "label": "alpha"},
    )
    assert put["ok"] is True
    sha = put["sha256"]

    ref = server._envelope_or_error("rw.scratch_ref", {"sha256": sha})
    assert ref["ok"] is True
    assert ref["entry"]["label"] == "alpha"

    got = server._envelope_or_error("rw.scratch_get", {"sha256": sha})
    assert got["ok"] is True
    assert got["content"] == "raw material\n"


def test_dispatch_scratchpad_get_disabled_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    put = server._envelope_or_error(
        "rw.scratch_put", {"content": "x\n"}
    )
    monkeypatch.setenv("RW_SCRATCH_DISABLE_GET", "yes")
    out = server._envelope_or_error(
        "rw.scratch_get", {"sha256": put["sha256"]}
    )
    assert out["ok"] is False
    assert out["error"] == "policy_violation"
    assert out["reason_hint"] == "permission"


def test_dispatch_handoff_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    envelope = {
        "task_id": "t1",
        "status": "complete",
        "agent": "claude-opus-4-6",
        "summary": "done",
        "next_steps": [],
        "last_good_state": [],
    }
    w = server._envelope_or_error("rw.handoff_write", {"envelope": envelope})
    assert w["ok"] is True
    r = server._envelope_or_error("rw.handoff_read", {})
    assert r["ok"] is True
    assert r["envelope"]["task_id"] == "t1"
