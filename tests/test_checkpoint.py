"""Tests for checkpoint features: save, read, list, cleanup, auto-chunking,
handoff integration, analytics tracking, and concurrent chunk sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from resilient_write import checkpoint, chunks, handoff
from resilient_write.analytics import analyze_journal
from resilient_write.errors import ResilientWriteError


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_journal_entry(
    workspace: Path,
    path: str,
    sha256: str = "abc123",
    bytes_written: int = 100,
    mode: str = "create",
    ts: str = "2026-04-16T12:00:00Z",
) -> None:
    jpath = workspace / ".resilient_write" / "journal.jsonl"
    jpath.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "journal_id": "wj_test",
        "ts": ts,
        "path": path,
        "sha256": sha256,
        "bytes": bytes_written,
        "mode": mode,
        "caller": "test",
    }
    with jpath.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


# ===========================================================================
# 1. Checkpoint round-trip: save → read → verify data integrity
# ===========================================================================


class TestCheckpointRoundTrip:
    def test_save_and_read(self, tmp_path: Path) -> None:
        data = {"paper_1": {"score": 11.5, "summary": "Great"}, "paper_2": {"score": 8.5}}
        save_result = checkpoint.checkpoint_save(
            tmp_path, name="analyses", data=data, fmt="json", ttl="session"
        )
        assert save_result["ok"] is True
        assert save_result["name"] == "analyses"

        read_result = checkpoint.checkpoint_read(tmp_path, name="analyses")
        assert read_result["ok"] is True
        assert read_result["data"] == data
        assert read_result["format"] == "json"
        assert read_result["ttl"] == "session"

    def test_overwrite_preserves_created_at(self, tmp_path: Path) -> None:
        checkpoint.checkpoint_save(tmp_path, name="ow", data={"v": 1})
        r1 = checkpoint.checkpoint_read(tmp_path, name="ow")

        checkpoint.checkpoint_save(tmp_path, name="ow", data={"v": 2})
        r2 = checkpoint.checkpoint_read(tmp_path, name="ow")

        assert r2["data"]["v"] == 2
        assert r2["created_at"] == r1["created_at"]

    def test_read_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResilientWriteError) as exc:
            checkpoint.checkpoint_read(tmp_path, name="nope")
        assert exc.value.error == "stale_precondition"

    def test_invalid_name_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResilientWriteError) as exc:
            checkpoint.checkpoint_save(tmp_path, name="bad name!", data={})
        assert exc.value.error == "policy_violation"

    def test_invalid_ttl_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResilientWriteError) as exc:
            checkpoint.checkpoint_save(tmp_path, name="ok", data={}, ttl="bogus")
        assert exc.value.error == "policy_violation"

    def test_yaml_format(self, tmp_path: Path) -> None:
        data = {"key": "value", "list": [1, 2, 3]}
        checkpoint.checkpoint_save(tmp_path, name="yml", data=data, fmt="yaml")
        result = checkpoint.checkpoint_read(tmp_path, name="yml")
        assert result["data"] == data
        assert result["format"] == "yaml"

    def test_list_checkpoints(self, tmp_path: Path) -> None:
        checkpoint.checkpoint_save(tmp_path, name="cp1", data={"a": 1})
        checkpoint.checkpoint_save(tmp_path, name="cp2", data={"b": 2})

        result = checkpoint.checkpoint_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 2
        names = {cp["name"] for cp in result["checkpoints"]}
        assert names == {"cp1", "cp2"}

    def test_list_empty(self, tmp_path: Path) -> None:
        result = checkpoint.checkpoint_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 0


# ===========================================================================
# 2. Checkpoint under concurrent chunk sessions
# ===========================================================================


class TestCheckpointConcurrentChunks:
    def test_checkpoint_during_active_chunk_session(self, tmp_path: Path) -> None:
        """Checkpoints and chunk sessions use independent directories."""
        # Start a chunk session.
        chunks.chunk_write(tmp_path, session="report", index=1, content="Part 1\n")
        chunks.chunk_write(tmp_path, session="report", index=2, content="Part 2\n")

        # Save a checkpoint while chunks are in progress.
        cp_result = checkpoint.checkpoint_save(
            tmp_path, name="interim", data={"papers": 9}
        )
        assert cp_result["ok"] is True

        # Chunk session should be unaffected.
        status = chunks.chunk_status(tmp_path, session="report")
        assert status["present_indices"] == [1, 2]

        # Checkpoint should be readable.
        read = checkpoint.checkpoint_read(tmp_path, name="interim")
        assert read["data"]["papers"] == 9

        # Compose should still work.
        compose = chunks.chunk_compose(
            tmp_path, session="report", output_path="report.txt", cleanup=True
        )
        assert compose["ok"] is True
        assert (tmp_path / "report.txt").read_text() == "Part 1\nPart 2\n"

    def test_checkpoint_chunked_session_names_dont_collide(self, tmp_path: Path) -> None:
        """Auto-chunked checkpoints use _cp_<name> sessions, which shouldn't
        collide with user chunk sessions."""
        # Create a user chunk session.
        chunks.chunk_write(tmp_path, session="mysess", index=1, content="A")

        # Create a checkpoint — even if it were chunked, it would use _cp_<name>.
        checkpoint.checkpoint_save(tmp_path, name="mysess", data={"x": 1})

        # Both should be independently accessible.
        assert chunks.chunk_status(tmp_path, session="mysess")["present_indices"] == [1]
        assert checkpoint.checkpoint_read(tmp_path, name="mysess")["data"] == {"x": 1}


# ===========================================================================
# 3. Checkpoint TTL expiration (cleanup)
# ===========================================================================


class TestCheckpointCleanup:
    def test_cleanup_session_checkpoints(self, tmp_path: Path) -> None:
        checkpoint.checkpoint_save(tmp_path, name="ephemeral", data={}, ttl="session")
        checkpoint.checkpoint_save(tmp_path, name="keeper", data={}, ttl="permanent")

        result = checkpoint.checkpoint_cleanup(tmp_path, include_session=True)
        assert result["ok"] is True
        assert len(result["removed"]) == 1
        assert result["removed"][0]["name"] == "ephemeral"
        assert result["removed"][0]["reason"] == "session_cleanup"
        assert result["kept"] == 1

        # Verify ephemeral is gone, keeper remains.
        listing = checkpoint.checkpoint_list(tmp_path)
        assert listing["count"] == 1
        assert listing["checkpoints"][0]["name"] == "keeper"

    def test_cleanup_skip_session_when_disabled(self, tmp_path: Path) -> None:
        checkpoint.checkpoint_save(tmp_path, name="sess", data={}, ttl="session")

        result = checkpoint.checkpoint_cleanup(tmp_path, include_session=False)
        assert result["kept"] == 1
        assert len(result["removed"]) == 0

    def test_cleanup_expired_iso_duration(self, tmp_path: Path) -> None:
        # Manually create a checkpoint with an old updated_at and short TTL.
        cpdir = tmp_path / ".resilient_write" / "checkpoints"
        cpdir.mkdir(parents=True, exist_ok=True)
        envelope = {
            "name": "old",
            "format": "json",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
            "ttl": "PT1H",
            "data": {},
        }
        (cpdir / "old.json").write_text(json.dumps(envelope))

        # Also create one that won't expire (permanent).
        checkpoint.checkpoint_save(tmp_path, name="fresh", data={}, ttl="permanent")

        result = checkpoint.checkpoint_cleanup(tmp_path)
        removed_names = {r["name"] for r in result["removed"]}
        assert "old" in removed_names
        assert "fresh" not in removed_names

    def test_cleanup_keeps_non_expired_duration(self, tmp_path: Path) -> None:
        # Create a checkpoint with very long TTL — should not be cleaned up.
        checkpoint.checkpoint_save(tmp_path, name="longttl", data={}, ttl="P365D")

        result = checkpoint.checkpoint_cleanup(tmp_path)
        assert result["kept"] == 1
        assert len(result["removed"]) == 0

    def test_cleanup_empty_dir(self, tmp_path: Path) -> None:
        result = checkpoint.checkpoint_cleanup(tmp_path)
        assert result["ok"] is True
        assert result["kept"] == 0
        assert result["removed"] == []

    def test_cleanup_corrupt_checkpoint(self, tmp_path: Path) -> None:
        cpdir = tmp_path / ".resilient_write" / "checkpoints"
        cpdir.mkdir(parents=True, exist_ok=True)
        (cpdir / "broken.json").write_text("not valid json{{{")

        result = checkpoint.checkpoint_cleanup(tmp_path)
        assert len(result["removed"]) == 1
        assert result["removed"][0]["reason"] == "corrupt"


# ===========================================================================
# 4. Handoff envelope includes checkpoint references
# ===========================================================================


class TestCheckpointHandoffIntegration:
    def test_handoff_write_includes_refs(self, tmp_path: Path) -> None:
        checkpoint.checkpoint_save(tmp_path, name="analysis", data={"score": 10})

        result = handoff.handoff_write(
            tmp_path,
            {
                "task_id": "t1",
                "status": "partial",
                "agent": "test",
                "summary": "test",
                "next_steps": ["continue"],
                "last_good_state": [],
            },
        )
        assert result["ok"] is True
        assert "checkpoint_refs" in result
        assert result["checkpoint_refs"][0]["name"] == "analysis"

    def test_handoff_read_includes_refs(self, tmp_path: Path) -> None:
        checkpoint.checkpoint_save(tmp_path, name="data", data={"x": 1})
        handoff.handoff_write(
            tmp_path,
            {
                "task_id": "t2",
                "status": "partial",
                "agent": "test",
                "summary": "test",
                "next_steps": [],
                "last_good_state": [],
            },
        )

        result = handoff.handoff_read(tmp_path)
        assert "checkpoint_refs" in result
        assert result["checkpoint_refs"][0]["name"] == "data"

    def test_handoff_no_refs_when_no_checkpoints(self, tmp_path: Path) -> None:
        handoff.handoff_write(
            tmp_path,
            {
                "task_id": "t3",
                "status": "complete",
                "agent": "test",
                "summary": "done",
                "next_steps": [],
                "last_good_state": [],
            },
        )
        result = handoff.handoff_read(tmp_path)
        assert "checkpoint_refs" not in result


# ===========================================================================
# 5. Large checkpoint (>1MB) uses chunked storage internally
# ===========================================================================


class TestLargeCheckpointAutoChunk:
    def test_large_checkpoint_auto_chunks(self, tmp_path: Path) -> None:
        # Create data that serializes to >1MB.
        large_data = {"entries": [{"id": i, "payload": "x" * 1000} for i in range(1200)]}
        result = checkpoint.checkpoint_save(tmp_path, name="big", data=large_data)

        assert result["ok"] is True
        assert result["chunked"] is True

        # The checkpoint file should exist and be readable.
        read = checkpoint.checkpoint_read(tmp_path, name="big")
        assert read["ok"] is True
        assert len(read["data"]["entries"]) == 1200
        assert read["bytes"] > 1_048_576

    def test_small_checkpoint_not_chunked(self, tmp_path: Path) -> None:
        result = checkpoint.checkpoint_save(tmp_path, name="small", data={"a": 1})
        assert result["ok"] is True
        assert result["chunked"] is False

    def test_large_checkpoint_cleanup_after_compose(self, tmp_path: Path) -> None:
        """Chunk session _cp_<name> should be cleaned up after compose."""
        large_data = {"entries": [{"id": i, "payload": "x" * 1000} for i in range(1200)]}
        checkpoint.checkpoint_save(tmp_path, name="clean", data=large_data)

        # The internal chunk session should be gone.
        status = chunks.chunk_status(tmp_path, session="_cp_clean")
        assert status["exists"] is False


# ===========================================================================
# 6. Analytics checkpoint tracking
# ===========================================================================


class TestAnalyticsCheckpointTracking:
    def test_analytics_detects_checkpoint_saves(self, tmp_path: Path) -> None:
        _write_journal_entry(
            tmp_path,
            path=".resilient_write/checkpoints/analysis.json",
            mode="create",
            ts="2026-04-16T12:00:00Z",
            bytes_written=500,
        )
        _write_journal_entry(
            tmp_path,
            path=".resilient_write/checkpoints/analysis.json",
            mode="overwrite",
            ts="2026-04-16T12:05:00Z",
            bytes_written=600,
        )
        _write_journal_entry(
            tmp_path,
            path=".resilient_write/checkpoints/other.json",
            mode="create",
            ts="2026-04-16T12:10:00Z",
            bytes_written=300,
        )

        result = analyze_journal(tmp_path)
        cp = result["checkpoints"]
        assert cp["total_saves"] == 3
        assert cp["overwrites"] == 1
        assert "analysis" in cp["by_name"]
        assert cp["by_name"]["analysis"]["saves"] == 2
        assert cp["by_name"]["analysis"]["total_bytes"] == 1100
        assert "other" in cp["by_name"]
        assert cp["by_name"]["other"]["saves"] == 1

    def test_analytics_no_checkpoints(self, tmp_path: Path) -> None:
        _write_journal_entry(tmp_path, path="regular.txt")

        result = analyze_journal(tmp_path)
        cp = result["checkpoints"]
        assert cp["total_saves"] == 0
        assert cp["overwrites"] == 0
        assert cp["by_name"] == {}

    def test_analytics_checkpoint_with_since_filter(self, tmp_path: Path) -> None:
        _write_journal_entry(
            tmp_path,
            path=".resilient_write/checkpoints/old.json",
            mode="create",
            ts="2026-04-16T10:00:00Z",
        )
        _write_journal_entry(
            tmp_path,
            path=".resilient_write/checkpoints/new.json",
            mode="create",
            ts="2026-04-16T14:00:00Z",
        )

        result = analyze_journal(tmp_path, since="2026-04-16T12:00:00Z")
        cp = result["checkpoints"]
        assert cp["total_saves"] == 1
        assert "new" in cp["by_name"]
        assert "old" not in cp["by_name"]
