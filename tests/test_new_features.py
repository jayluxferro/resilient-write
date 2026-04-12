"""Tests for the three new features: validate, analytics, and chunk_preview."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from resilient_write import chunks
from resilient_write.analytics import analyze_journal
from resilient_write.errors import ResilientWriteError
from resilient_write.validate import validate_content


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helper: write journal entries directly for analytics tests
# ---------------------------------------------------------------------------


def _write_journal_entry(
    workspace: Path,
    path: str,
    sha256: str = "abc123",
    bytes_written: int = 100,
    mode: str = "create",
    ts: str = "2026-04-12T17:00:00Z",
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
# 1. validate.validate_content tests
# ===========================================================================


class TestValidateLatex:
    """LaTeX validation checks."""

    def test_validate_valid_latex(self) -> None:
        content = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Hello world.\n"
            "\\end{document}\n"
        )
        result = validate_content(content, format_hint="latex")
        assert result["ok"] is True
        assert result["valid"] is True
        assert result["format"] == "latex"
        # No errors at all (no warnings either, since all structure present).
        errors = [e for e in result["errors"] if e["severity"] == "error"]
        assert errors == []

    def test_validate_latex_unmatched_brace(self) -> None:
        result = validate_content("{hello", format_hint="latex")
        assert result["valid"] is False
        error_msgs = [e["message"] for e in result["errors"] if e["severity"] == "error"]
        assert any("brace" in m.lower() for m in error_msgs)

    def test_validate_latex_unmatched_env(self) -> None:
        content = "\\begin{itemize}\n\\item hello\n"
        result = validate_content(content, format_hint="latex")
        assert result["valid"] is False
        error_msgs = [e["message"] for e in result["errors"] if e["severity"] == "error"]
        assert any("itemize" in m and "never closed" in m for m in error_msgs)

    def test_validate_latex_mismatched_env(self) -> None:
        content = "\\begin{itemize}\n\\item hello\n\\end{enumerate}\n"
        result = validate_content(content, format_hint="latex")
        assert result["valid"] is False
        error_msgs = [e["message"] for e in result["errors"] if e["severity"] == "error"]
        assert any("mismatches" in m or "mismatch" in m.lower() for m in error_msgs)

    def test_validate_latex_missing_documentclass(self) -> None:
        content = "\\begin{document}\nHello\n\\end{document}\n"
        result = validate_content(content, format_hint="latex")
        warnings = [e for e in result["errors"] if e["severity"] == "warning"]
        warning_msgs = [w["message"] for w in warnings]
        assert any("documentclass" in m for m in warning_msgs)

    def test_validate_latex_begin_typo(self) -> None:
        content = "\\being{document}\nHello\n"
        result = validate_content(content, format_hint="latex")
        warnings = [e for e in result["errors"] if e["severity"] == "warning"]
        warning_msgs = [w["message"] for w in warnings]
        assert any("being" in m.lower() for m in warning_msgs)

    def test_validate_latex_nested_braces(self) -> None:
        content = "{a{b{c}d}e}"
        result = validate_content(content, format_hint="latex")
        brace_errors = [
            e
            for e in result["errors"]
            if e["severity"] == "error" and "brace" in e["message"].lower()
        ]
        assert brace_errors == []


class TestValidateJson:
    """JSON validation checks."""

    def test_validate_valid_json(self) -> None:
        result = validate_content('{"key": "value"}', format_hint="json")
        assert result["ok"] is True
        assert result["valid"] is True
        assert result["format"] == "json"
        assert result["errors"] == []

    def test_validate_invalid_json(self) -> None:
        result = validate_content('{"key":}', format_hint="json")
        assert result["valid"] is False
        errors = result["errors"]
        assert len(errors) == 1
        assert errors[0]["severity"] == "error"
        assert errors[0]["line"] is not None


class TestValidatePython:
    """Python validation checks."""

    def test_validate_valid_python(self) -> None:
        result = validate_content("def foo(): pass", format_hint="python")
        assert result["ok"] is True
        assert result["valid"] is True
        assert result["format"] == "python"
        assert result["errors"] == []

    def test_validate_invalid_python(self) -> None:
        result = validate_content("def foo(", format_hint="python")
        assert result["valid"] is False
        errors = result["errors"]
        assert len(errors) == 1
        assert errors[0]["severity"] == "error"


class TestValidateAutodetect:
    """Format auto-detection tests."""

    def test_validate_autodetect_json(self) -> None:
        result = validate_content('{"auto": true}')
        assert result["format"] == "json"
        assert result["valid"] is True

    def test_validate_autodetect_by_extension(self) -> None:
        result = validate_content("def foo(): pass", target_path="foo.py")
        assert result["format"] == "python"
        assert result["valid"] is True

    def test_validate_unknown_format(self) -> None:
        result = validate_content("just plain text")
        assert result["format"] == "unknown"
        assert result["valid"] is True
        assert result["errors"] == []

    def test_validate_empty_content(self) -> None:
        # Empty string should not crash regardless of format.
        for fmt in ["latex", "json", "python", None]:
            result = validate_content("", format_hint=fmt)
            assert result["ok"] is True


# ===========================================================================
# 2. analytics.analyze_journal tests
# ===========================================================================


class TestAnalytics:
    """Journal analytics tests."""

    def test_analytics_empty_journal(self, tmp_path: Path) -> None:
        # Create an empty journal file.
        jpath = tmp_path / ".resilient_write" / "journal.jsonl"
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.touch()

        result = analyze_journal(tmp_path)
        assert result["ok"] is True
        assert result["total_writes"] == 0
        assert result["unique_paths"] == 0
        assert result["total_bytes_written"] == 0

    def test_analytics_basic_counts(self, tmp_path: Path) -> None:
        for i in range(3):
            _write_journal_entry(
                tmp_path,
                path=f"file{i}.txt",
                ts=f"2026-04-12T17:0{i}:00Z",
            )

        result = analyze_journal(tmp_path)
        assert result["total_writes"] == 3
        assert result["unique_paths"] == 3

    def test_analytics_by_mode(self, tmp_path: Path) -> None:
        _write_journal_entry(tmp_path, path="a.txt", mode="create", ts="2026-04-12T17:00:00Z")
        _write_journal_entry(tmp_path, path="a.txt", mode="overwrite", ts="2026-04-12T17:01:00Z")
        _write_journal_entry(tmp_path, path="b.txt", mode="append", ts="2026-04-12T17:02:00Z")

        result = analyze_journal(tmp_path)
        assert result["by_mode"]["create"] == 1
        assert result["by_mode"]["overwrite"] == 1
        assert result["by_mode"]["append"] == 1

    def test_analytics_hot_paths(self, tmp_path: Path) -> None:
        for i in range(5):
            _write_journal_entry(
                tmp_path,
                path="hot.txt",
                ts=f"2026-04-12T17:0{i}:00Z",
            )

        result = analyze_journal(tmp_path)
        hot = result["hot_paths"]
        assert len(hot) >= 1
        assert hot[0]["path"] == "hot.txt"
        assert hot[0]["write_count"] == 5

    def test_analytics_session_detection(self, tmp_path: Path) -> None:
        for i in range(3):
            _write_journal_entry(
                tmp_path,
                path=f".resilient_write/chunks/my-sess/part-00{i+1}.txt",
                ts=f"2026-04-12T17:0{i}:00Z",
            )

        result = analyze_journal(tmp_path)
        assert "my-sess" in result["sessions"]
        sess = result["sessions"]["my-sess"]
        assert sess["chunk_writes"] == 3

    def test_analytics_since_filter(self, tmp_path: Path) -> None:
        _write_journal_entry(tmp_path, path="old.txt", ts="2026-04-12T16:00:00Z")
        _write_journal_entry(tmp_path, path="new.txt", ts="2026-04-12T18:00:00Z")

        result = analyze_journal(tmp_path, since="2026-04-12T17:00:00Z")
        assert result["total_writes"] == 1
        paths_in_hot = [h["path"] for h in result["hot_paths"]]
        assert "new.txt" in paths_in_hot
        assert "old.txt" not in paths_in_hot

    def test_analytics_write_velocity(self, tmp_path: Path) -> None:
        # 3 writes over 2 minutes = 1.5 writes/min.
        _write_journal_entry(tmp_path, path="a.txt", ts="2026-04-12T17:00:00Z")
        _write_journal_entry(tmp_path, path="b.txt", ts="2026-04-12T17:01:00Z")
        _write_journal_entry(tmp_path, path="c.txt", ts="2026-04-12T17:02:00Z")

        result = analyze_journal(tmp_path)
        velocity = result["write_velocity"]
        assert velocity["writes_per_minute"] == pytest.approx(1.5, abs=0.01)

    def test_analytics_no_journal_file(self, tmp_path: Path) -> None:
        # No .resilient_write dir at all -- should return zeros gracefully.
        result = analyze_journal(tmp_path)
        assert result["ok"] is True
        assert result["total_writes"] == 0

    def test_analytics_period(self, tmp_path: Path) -> None:
        _write_journal_entry(tmp_path, path="first.txt", ts="2026-04-12T17:00:00Z")
        _write_journal_entry(tmp_path, path="last.txt", ts="2026-04-12T17:05:00Z")

        result = analyze_journal(tmp_path)
        period = result["period"]
        assert period["first_entry"] == "2026-04-12T17:00:00Z"
        assert period["last_entry"] == "2026-04-12T17:05:00Z"
        assert period["duration_seconds"] == pytest.approx(300.0)

    def test_analytics_session_filter(self, tmp_path: Path) -> None:
        for i in range(2):
            _write_journal_entry(
                tmp_path,
                path=f".resilient_write/chunks/alpha/part-00{i+1}.txt",
                ts=f"2026-04-12T17:0{i}:00Z",
            )
        for i in range(2):
            _write_journal_entry(
                tmp_path,
                path=f".resilient_write/chunks/beta/part-00{i+1}.txt",
                ts=f"2026-04-12T17:0{i+2}:00Z",
            )

        result = analyze_journal(tmp_path, session_filter="alpha")
        assert "alpha" in result["sessions"]
        assert "beta" not in result["sessions"]


# ===========================================================================
# 3. chunk_preview tests
# ===========================================================================


class TestChunkPreview:
    """Tests for chunks.chunk_preview dry-run compose."""

    def test_chunk_preview_basic(self, tmp_path: Path) -> None:
        chunks.chunk_write(tmp_path, session="prev", index=1, content="AAA", total_expected=3)
        chunks.chunk_write(tmp_path, session="prev", index=2, content="BBB", total_expected=3)
        chunks.chunk_write(tmp_path, session="prev", index=3, content="CCC", total_expected=3)

        result = chunks.chunk_preview(tmp_path, session="prev")
        assert result["ok"] is True
        assert result["preview"] is True
        assert result["content"] == "AAABBBCCC"
        assert result["chunk_count"] == 3

    def test_chunk_preview_contiguity_check(self, tmp_path: Path) -> None:
        chunks.chunk_write(tmp_path, session="gap", index=1, content="A", total_expected=3)
        chunks.chunk_write(tmp_path, session="gap", index=3, content="C", total_expected=3)

        with pytest.raises(ResilientWriteError) as exc:
            chunks.chunk_preview(tmp_path, session="gap")
        assert exc.value.error == "stale_precondition"
        assert exc.value.context["reason"] == "non_contiguous_chunks"
        assert 2 in exc.value.context["missing"]

    def test_chunk_preview_total_expected_mismatch(self, tmp_path: Path) -> None:
        chunks.chunk_write(tmp_path, session="short", index=1, content="A", total_expected=3)
        chunks.chunk_write(tmp_path, session="short", index=2, content="B", total_expected=3)

        with pytest.raises(ResilientWriteError) as exc:
            chunks.chunk_preview(tmp_path, session="short")
        assert exc.value.error == "stale_precondition"
        assert exc.value.context["reason"] == "chunk_count_mismatch"
        assert exc.value.context["have"] == 2
        assert exc.value.context["total_expected"] == 3

    def test_chunk_preview_with_separator(self, tmp_path: Path) -> None:
        chunks.chunk_write(tmp_path, session="sep", index=1, content="X", total_expected=2)
        chunks.chunk_write(tmp_path, session="sep", index=2, content="Y", total_expected=2)

        result = chunks.chunk_preview(tmp_path, session="sep", separator="\n---\n")
        assert result["content"] == "X\n---\nY"

    def test_chunk_preview_does_not_write(self, tmp_path: Path) -> None:
        chunks.chunk_write(tmp_path, session="nw", index=1, content="only", total_expected=1)

        # Snapshot files before preview.
        before = set(tmp_path.rglob("*"))

        chunks.chunk_preview(tmp_path, session="nw")

        # No new files should have been created by the preview.
        after = set(tmp_path.rglob("*"))
        assert before == after
