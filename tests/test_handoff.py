"""Tests for L5 `rw.handoff_write` and `rw.handoff_read`."""

from __future__ import annotations

from pathlib import Path

import pytest

from resilient_write import handoff
from resilient_write.errors import ResilientWriteError
from resilient_write.safe_write import safe_write


def _minimal_envelope(**overrides):
    env = {
        "task_id": "demo-task",
        "status": "partial",
        "agent": "claude-opus-4-6",
        "summary": "wip",
        "next_steps": ["finish the thing"],
        "last_good_state": [],
    }
    env.update(overrides)
    return env


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    r1 = safe_write(tmp_path, path="report.tex", content="hello\n")
    envelope = _minimal_envelope(
        last_good_state=[{"path": "report.tex", "sha256": r1["sha256"]}],
    )
    result = handoff.handoff_write(tmp_path, envelope, body="# notes\n\nprose\n")
    assert result["ok"] is True
    assert result["handoff_path"] == "HANDOFF.md"
    assert result["drift_warnings"] == []

    assert (tmp_path / "HANDOFF.md").exists()

    loaded = handoff.handoff_read(tmp_path)
    assert loaded["envelope"]["task_id"] == "demo-task"
    assert loaded["envelope"]["status"] == "partial"
    assert loaded["envelope"]["last_good_state"][0]["path"] == "report.tex"
    assert "prose" in loaded["body"]
    assert loaded["drift_warnings"] == []


def test_drift_warning_when_file_hash_changes(tmp_path: Path) -> None:
    r1 = safe_write(tmp_path, path="a.txt", content="one\n")
    envelope = _minimal_envelope(
        last_good_state=[{"path": "a.txt", "sha256": r1["sha256"]}],
    )
    handoff.handoff_write(tmp_path, envelope)
    safe_write(tmp_path, path="a.txt", content="two\n", mode="overwrite")

    loaded = handoff.handoff_read(tmp_path)
    assert len(loaded["drift_warnings"]) == 1
    assert loaded["drift_warnings"][0]["reason"] == "hash_mismatch"
    assert loaded["drift_warnings"][0]["path"] == "a.txt"


def test_drift_warning_when_file_missing(tmp_path: Path) -> None:
    envelope = _minimal_envelope(
        last_good_state=[{"path": "ghost.txt", "sha256": "0" * 64}],
    )
    result = handoff.handoff_write(tmp_path, envelope)
    assert result["drift_warnings"] == [
        {"path": "ghost.txt", "reason": "missing"}
    ]


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    envelope = _minimal_envelope()
    del envelope["task_id"]
    with pytest.raises(ResilientWriteError) as exc:
        handoff.handoff_write(tmp_path, envelope)
    assert exc.value.error == "policy_violation"
    assert "task_id" in exc.value.context["missing_fields"]


def test_invalid_status_rejected(tmp_path: Path) -> None:
    envelope = _minimal_envelope(status="wip")
    with pytest.raises(ResilientWriteError) as exc:
        handoff.handoff_write(tmp_path, envelope)
    assert exc.value.error == "policy_violation"


def test_archive_copies_previous_envelope(tmp_path: Path) -> None:
    env_a = _minimal_envelope(summary="first")
    handoff.handoff_write(tmp_path, env_a)
    env_b = _minimal_envelope(summary="second")
    handoff.handoff_write(tmp_path, env_b, archive=True)

    archives = list((tmp_path / ".resilient_write" / "handoffs").glob("*.md"))
    assert len(archives) == 1
    assert "first" in archives[0].read_text()

    current = handoff.handoff_read(tmp_path)
    assert current["envelope"]["summary"] == "second"


def test_handoff_read_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        handoff.handoff_read(tmp_path)
    assert exc.value.error == "stale_precondition"


def test_updated_at_auto_filled(tmp_path: Path) -> None:
    envelope = _minimal_envelope()
    handoff.handoff_write(tmp_path, envelope)
    loaded = handoff.handoff_read(tmp_path)
    assert "updated_at" in loaded["envelope"]
    # Caller's dict must not be mutated.
    assert "updated_at" not in envelope
