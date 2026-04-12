"""Tests for L2 chunk_write / chunk_compose / chunk_reset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from resilient_write import chunks, journal
from resilient_write.errors import ResilientWriteError


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# chunk_write
# ---------------------------------------------------------------------------


def test_chunk_write_creates_part_file(tmp_path: Path) -> None:
    out = chunks.chunk_write(
        tmp_path, session="s1", index=1, content="alpha\n", total_expected=3
    )
    assert out["ok"] is True
    assert out["chunk_path"] == ".resilient_write/chunks/s1/part-001.txt"
    assert out["sha256"] == _sha("alpha\n")
    part = tmp_path / ".resilient_write/chunks/s1/part-001.txt"
    assert part.read_text() == "alpha\n"


def test_chunk_write_creates_manifest(tmp_path: Path) -> None:
    chunks.chunk_write(
        tmp_path, session="s1", index=1, content="a", total_expected=2
    )
    manifest_path = tmp_path / ".resilient_write/chunks/s1/manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["session"] == "s1"
    assert data["total_expected"] == 2
    assert "created_at" in data
    assert "updated_at" in data


def test_chunk_write_retry_overwrites(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s1", index=2, content="v1\n")
    out = chunks.chunk_write(tmp_path, session="s1", index=2, content="v2\n")
    assert out["sha256"] == _sha("v2\n")
    part = tmp_path / ".resilient_write/chunks/s1/part-002.txt"
    assert part.read_text() == "v2\n"


def test_chunk_write_invalid_session_name(tmp_path: Path) -> None:
    for bad in ["", "has space", "with/slash", "..", "a" * 65]:
        with pytest.raises(ResilientWriteError) as exc:
            chunks.chunk_write(tmp_path, session=bad, index=1, content="x")
        assert exc.value.error == "policy_violation"


def test_chunk_write_invalid_index(tmp_path: Path) -> None:
    for bad in [0, -1, 1000]:
        with pytest.raises(ResilientWriteError) as exc:
            chunks.chunk_write(tmp_path, session="s", index=bad, content="x")
        assert exc.value.error == "policy_violation"


def test_chunk_write_index_exceeds_total(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        chunks.chunk_write(
            tmp_path, session="s", index=5, content="x", total_expected=3
        )
    assert exc.value.error == "policy_violation"
    assert exc.value.context["reason"] == "index_exceeds_total_expected"


def test_chunk_write_total_expected_sticky(tmp_path: Path) -> None:
    chunks.chunk_write(
        tmp_path, session="s", index=1, content="a", total_expected=3
    )
    # Second call omits total_expected → manifest retains the earlier value.
    chunks.chunk_write(tmp_path, session="s", index=2, content="b")
    data = json.loads(
        (tmp_path / ".resilient_write/chunks/s/manifest.json").read_text()
    )
    assert data["total_expected"] == 3


def test_chunk_write_journal_entries(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=2)
    chunks.chunk_write(tmp_path, session="s", index=2, content="b", total_expected=2)
    entries = journal.tail(tmp_path, n=10)
    paths = {e["path"] for e in entries}
    assert ".resilient_write/chunks/s/part-001.txt" in paths
    assert ".resilient_write/chunks/s/part-002.txt" in paths


# ---------------------------------------------------------------------------
# chunk_compose
# ---------------------------------------------------------------------------


def test_chunk_compose_concatenates_in_order(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="first", total_expected=3)
    chunks.chunk_write(tmp_path, session="s", index=2, content="second", total_expected=3)
    chunks.chunk_write(tmp_path, session="s", index=3, content="third", total_expected=3)

    result = chunks.chunk_compose(
        tmp_path, session="s", output_path="out.txt"
    )
    assert result["ok"] is True
    assert result["chunk_count"] == 3
    assert (tmp_path / "out.txt").read_text() == "firstsecondthird"
    assert result["sha256"] == _sha("firstsecondthird")
    assert len(result["chunk_hashes"]) == 3


def test_chunk_compose_with_separator(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=2)
    chunks.chunk_write(tmp_path, session="s", index=2, content="b", total_expected=2)
    result = chunks.chunk_compose(
        tmp_path, session="s", output_path="out.txt", separator="\n\n"
    )
    assert (tmp_path / "out.txt").read_text() == "a\n\nb"
    assert result["sha256"] == _sha("a\n\nb")


def test_chunk_compose_overwrites_existing_output(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("old\n")
    chunks.chunk_write(tmp_path, session="s", index=1, content="new\n", total_expected=1)
    chunks.chunk_compose(tmp_path, session="s", output_path="out.txt")
    assert (tmp_path / "out.txt").read_text() == "new\n"


def test_chunk_compose_missing_chunk_rejected(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=3)
    chunks.chunk_write(tmp_path, session="s", index=3, content="c", total_expected=3)
    with pytest.raises(ResilientWriteError) as exc:
        chunks.chunk_compose(tmp_path, session="s", output_path="out.txt")
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "non_contiguous_chunks"
    assert exc.value.context["missing"] == [2]


def test_chunk_compose_total_mismatch_rejected(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=3)
    chunks.chunk_write(tmp_path, session="s", index=2, content="b", total_expected=3)
    with pytest.raises(ResilientWriteError) as exc:
        chunks.chunk_compose(tmp_path, session="s", output_path="out.txt")
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "chunk_count_mismatch"
    assert exc.value.context["have"] == 2
    assert exc.value.context["total_expected"] == 3


def test_chunk_compose_missing_session_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        chunks.chunk_compose(tmp_path, session="ghost", output_path="out.txt")
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "session_not_found"


def test_chunk_compose_cleanup_wipes_session(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=1)
    chunks.chunk_compose(
        tmp_path, session="s", output_path="out.txt", cleanup=True
    )
    sdir = tmp_path / ".resilient_write/chunks/s"
    assert not sdir.exists()
    assert (tmp_path / "out.txt").read_text() == "a"


def test_chunk_compose_without_cleanup_preserves_session(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=1)
    chunks.chunk_compose(tmp_path, session="s", output_path="out.txt")
    sdir = tmp_path / ".resilient_write/chunks/s"
    assert sdir.exists()
    assert (sdir / "part-001.txt").read_text() == "a"


# ---------------------------------------------------------------------------
# chunk_reset / chunk_status
# ---------------------------------------------------------------------------


def test_chunk_reset_wipes_session(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=2)
    chunks.chunk_write(tmp_path, session="s", index=2, content="b", total_expected=2)
    result = chunks.chunk_reset(tmp_path, session="s")
    assert result["ok"] is True
    assert result["existed"] is True
    assert result["removed"] >= 2  # parts + manifest
    assert not (tmp_path / ".resilient_write/chunks/s").exists()


def test_chunk_reset_missing_session(tmp_path: Path) -> None:
    result = chunks.chunk_reset(tmp_path, session="ghost")
    assert result["ok"] is True
    assert result["existed"] is False
    assert result["removed"] == 0


def test_chunk_status_reports_present_indices(tmp_path: Path) -> None:
    chunks.chunk_write(tmp_path, session="s", index=1, content="a", total_expected=3)
    chunks.chunk_write(tmp_path, session="s", index=3, content="c", total_expected=3)
    st = chunks.chunk_status(tmp_path, session="s")
    assert st["exists"] is True
    assert st["total_expected"] == 3
    assert st["present_indices"] == [1, 3]


def test_chunk_status_missing_session(tmp_path: Path) -> None:
    st = chunks.chunk_status(tmp_path, session="ghost")
    assert st["exists"] is False


# ---------------------------------------------------------------------------
# chunk_append (auto-incrementing)
# ---------------------------------------------------------------------------


def test_chunk_append_starts_at_one(tmp_path: Path) -> None:
    out = chunks.chunk_append(tmp_path, session="s", content="first\n")
    assert out["ok"] is True
    assert out["index"] == 1
    assert out["chunk_path"] == ".resilient_write/chunks/s/part-001.txt"
    assert (tmp_path / ".resilient_write/chunks/s/part-001.txt").read_text() == "first\n"


def test_chunk_append_auto_increments(tmp_path: Path) -> None:
    chunks.chunk_append(tmp_path, session="s", content="A")
    chunks.chunk_append(tmp_path, session="s", content="B")
    out = chunks.chunk_append(tmp_path, session="s", content="C")
    assert out["index"] == 3
    assert out["sha256"] == _sha("C")


def test_chunk_append_composes_correctly(tmp_path: Path) -> None:
    chunks.chunk_append(tmp_path, session="doc", content="\\section{Intro}\n")
    chunks.chunk_append(tmp_path, session="doc", content="\\section{Body}\n")
    chunks.chunk_append(tmp_path, session="doc", content="\\section{End}\n")
    result = chunks.chunk_compose(
        tmp_path, session="doc", output_path="doc.tex"
    )
    assert result["chunk_count"] == 3
    assert (tmp_path / "doc.tex").read_text() == (
        "\\section{Intro}\n\\section{Body}\n\\section{End}\n"
    )


def test_chunk_append_with_total_expected(tmp_path: Path) -> None:
    chunks.chunk_append(tmp_path, session="s", content="a", total_expected=2)
    chunks.chunk_append(tmp_path, session="s", content="b", total_expected=2)
    result = chunks.chunk_compose(tmp_path, session="s", output_path="out.txt")
    assert result["chunk_count"] == 2


def test_chunk_append_mixed_with_chunk_write(tmp_path: Path) -> None:
    """chunk_append picks up after manually-indexed chunk_write calls."""
    chunks.chunk_write(tmp_path, session="s", index=1, content="manual")
    out = chunks.chunk_append(tmp_path, session="s", content="auto")
    assert out["index"] == 2


def test_chunk_append_survives_gap_from_prior_write(tmp_path: Path) -> None:
    """If chunk_write left a gap (1, 3), append continues after the highest."""
    chunks.chunk_write(tmp_path, session="s", index=1, content="a")
    chunks.chunk_write(tmp_path, session="s", index=3, content="c")
    out = chunks.chunk_append(tmp_path, session="s", content="d")
    assert out["index"] == 4


# ---------------------------------------------------------------------------
# Resumability scenario (next-steps.md task 3.4)
# ---------------------------------------------------------------------------


def test_resumability_after_simulated_chunk_failure(tmp_path: Path) -> None:
    """Write chunk 1 and 2, simulate chunk 3 failing (never written),
    verify chunks 1 and 2 survive, retry chunk 3, verify compose."""
    chunks.chunk_write(tmp_path, session="report", index=1, content="A", total_expected=3)
    chunks.chunk_write(tmp_path, session="report", index=2, content="B", total_expected=3)

    # Compose refuses before chunk 3 arrives.
    with pytest.raises(ResilientWriteError):
        chunks.chunk_compose(tmp_path, session="report", output_path="r.txt")

    # Chunks 1 and 2 are still on disk.
    sdir = tmp_path / ".resilient_write/chunks/report"
    assert (sdir / "part-001.txt").read_text() == "A"
    assert (sdir / "part-002.txt").read_text() == "B"

    # Retry chunk 3.
    chunks.chunk_write(tmp_path, session="report", index=3, content="C", total_expected=3)

    # Compose now succeeds.
    result = chunks.chunk_compose(
        tmp_path, session="report", output_path="r.txt", cleanup=True
    )
    assert (tmp_path / "r.txt").read_text() == "ABC"
    assert result["chunk_count"] == 3
    assert not sdir.exists()
