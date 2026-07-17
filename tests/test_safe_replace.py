"""Tests for L1 `rw.safe_replace`."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from resilient_write import journal
from resilient_write.errors import ResilientWriteError
from resilient_write.safe_replace import safe_replace
from resilient_write.safe_write import safe_write


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_replace_single_occurrence_default_count(tmp_path: Path) -> None:
    safe_write([tmp_path], path="notes.txt", content="hello world\n")
    result = safe_replace(
        [tmp_path], path="notes.txt", old_string="world", new_string="there"
    )

    assert result["ok"] is True
    assert result["path"] == "notes.txt"
    assert result["mode_applied"] == "replace"
    assert result["replacements"] == 1
    assert result["sha256"] == _sha("hello there\n")
    assert (tmp_path / "notes.txt").read_text() == "hello there\n"

    entries = journal.tail(tmp_path, n=10)
    assert len(entries) == 2
    assert entries[-1]["mode"] == "replace"


def test_replace_all_occurrences(tmp_path: Path) -> None:
    safe_write([tmp_path], path="items.txt", content="a, a, a\n")
    result = safe_replace(
        [tmp_path], path="items.txt", old_string="a", new_string="b", count=-1
    )

    assert result["replacements"] == 3
    assert (tmp_path / "items.txt").read_text() == "b, b, b\n"


def test_replace_specific_count_exact(tmp_path: Path) -> None:
    safe_write([tmp_path], path="items.txt", content="a, a\n")
    result = safe_replace(
        [tmp_path], path="items.txt", old_string="a", new_string="b", count=2
    )

    assert result["replacements"] == 2
    assert (tmp_path / "items.txt").read_text() == "b, b\n"


def test_replace_default_requires_exactly_one_match(tmp_path: Path) -> None:
    safe_write([tmp_path], path="items.txt", content="a, a\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="items.txt", old_string="a", new_string="b"
        )
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "ambiguous_match"
    assert (tmp_path / "items.txt").read_text() == "a, a\n"


def test_replace_zero_matches_rejected(tmp_path: Path) -> None:
    safe_write([tmp_path], path="items.txt", content="hello\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="items.txt", old_string="missing", new_string="x"
        )
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "old_string_not_found"


def test_replace_insufficient_matches(tmp_path: Path) -> None:
    safe_write([tmp_path], path="items.txt", content="a\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="items.txt", old_string="a", new_string="b", count=3
        )
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "insufficient_matches"
    assert exc.value.context["expected_count"] == 3
    assert exc.value.context["actual_count"] == 1


def test_replace_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="missing.txt", old_string="a", new_string="b"
        )
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["reason"] == "file_not_found"


def test_replace_expected_prev_sha256_matches(tmp_path: Path) -> None:
    r1 = safe_write([tmp_path], path="a.txt", content="one two\n")
    r2 = safe_replace(
        [tmp_path],
        path="a.txt",
        old_string="two",
        new_string="three",
        expected_prev_sha256=r1["sha256"],
    )
    assert r2["ok"] is True
    assert (tmp_path / "a.txt").read_text() == "one three\n"


def test_replace_expected_prev_sha256_mismatch_rejects(tmp_path: Path) -> None:
    safe_write([tmp_path], path="a.txt", content="one two\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path],
            path="a.txt",
            old_string="two",
            new_string="three",
            expected_prev_sha256="deadbeef" * 8,
        )
    assert exc.value.error == "stale_precondition"
    assert exc.value.context["expected_prev_sha256"] == "deadbeef" * 8
    assert "actual_prev_sha256" in exc.value.context


def test_replace_classify_blocks_risky_new_string(tmp_path: Path) -> None:
    safe_write([tmp_path], path="cfg.txt", content="key = old\n")
    risky_new = (
        "authorization: Bearer sk-ant-oat01-"
        + "A" * 48
        + "\nx-github-token: gho_"
        + "D" * 36
        + "\n"
    )
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path],
            path="cfg.txt",
            old_string="old",
            new_string=risky_new,
            classify=True,
        )
    assert exc.value.error == "blocked"
    assert exc.value.reason_hint == "content_filter"
    assert "api_key" in exc.value.detected_patterns
    assert "github_pat" in exc.value.detected_patterns
    assert (tmp_path / "cfg.txt").read_text() == "key = old\n"


def test_replace_empty_old_string_rejected(tmp_path: Path) -> None:
    safe_write([tmp_path], path="a.txt", content="x\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="a.txt", old_string="", new_string="y"
        )
    assert exc.value.error == "policy_violation"
    assert exc.value.context["reason"] == "empty_old_string"


def test_replace_count_zero_rejected(tmp_path: Path) -> None:
    safe_write([tmp_path], path="a.txt", content="x\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="a.txt", old_string="x", new_string="y", count=0
        )
    assert exc.value.error == "policy_violation"
    assert exc.value.context["reason"] == "count_cannot_be_zero"


def test_replace_not_utf8_rejected(tmp_path: Path) -> None:
    safe_write(
        [tmp_path], path="bin.dat", content_bytes=b"\xff\xfe"
    )
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="bin.dat", old_string="x", new_string="y"
        )
    assert exc.value.error == "policy_violation"
    assert exc.value.reason_hint == "encoding"


def test_replace_no_temp_files_left_on_success(tmp_path: Path) -> None:
    safe_write([tmp_path], path="a.txt", content="hello\n")
    safe_replace([tmp_path], path="a.txt", old_string="hello", new_string="hi")
    leftovers = list(tmp_path.glob("**/*.tmp.*"))
    assert leftovers == []


def test_replace_no_temp_files_left_on_rejected_match(tmp_path: Path) -> None:
    safe_write([tmp_path], path="a.txt", content="hello\n")
    with pytest.raises(ResilientWriteError):
        safe_replace(
            [tmp_path], path="a.txt", old_string="missing", new_string="x"
        )
    leftovers = list(tmp_path.glob("**/*.tmp.*"))
    assert leftovers == []


def test_replace_path_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        safe_replace(
            [tmp_path], path="../escape.txt", old_string="a", new_string="b"
        )
    assert exc.value.error == "policy_violation"


def test_replace_preserves_surrounding_content(tmp_path: Path) -> None:
    safe_write(
        [tmp_path],
        path="big.txt",
        content="line1\nbox art divider\nline3\nplain divider\nline5\n",
    )
    result = safe_replace(
        [tmp_path],
        path="big.txt",
        old_string="box art divider",
        new_string="fancy divider",
        count=1,
    )
    assert result["replacements"] == 1
    text = (tmp_path / "big.txt").read_text()
    assert text.count("fancy divider") == 1
    assert text.count("box art divider") == 0
    assert "plain divider" in text
    assert "line1\n" in text
    assert "line5\n" in text
