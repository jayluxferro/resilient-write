"""Tests for L1 `rw.safe_write`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from resilient_write import journal
from resilient_write.errors import ResilientWriteError
from resilient_write.safe_write import safe_write


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_create_writes_file_and_journal(tmp_path: Path) -> None:
    result = safe_write(tmp_path, path="notes.txt", content="hello\n")

    assert result["ok"] is True
    assert result["path"] == "notes.txt"
    assert result["sha256"] == _sha("hello\n")
    assert result["bytes"] == len("hello\n".encode())
    assert result["mode_applied"] == "create"
    assert result["journal_id"].startswith("wj_")

    target = tmp_path / "notes.txt"
    assert target.read_text() == "hello\n"

    entries = journal.tail(tmp_path, n=10)
    assert len(entries) == 1
    assert entries[0]["path"] == "notes.txt"
    assert entries[0]["sha256"] == result["sha256"]


def test_create_refuses_existing_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_write(tmp_path, path="a.txt", content="new\n")
    assert exc.value.error == "stale_precondition"
    assert (tmp_path / "a.txt").read_text() == "old\n"


def test_overwrite_replaces_content(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a.txt", content="v1\n")
    safe_write(tmp_path, path="a.txt", content="v2\n", mode="overwrite")
    assert (tmp_path / "a.txt").read_text() == "v2\n"
    assert len(journal.tail(tmp_path, n=10)) == 2


def test_append_preserves_existing_bytes(tmp_path: Path) -> None:
    safe_write(tmp_path, path="log.txt", content="line1\n")
    result = safe_write(
        tmp_path, path="log.txt", content="line2\n", mode="append"
    )
    assert (tmp_path / "log.txt").read_text() == "line1\nline2\n"
    assert result["sha256"] == _sha("line1\nline2\n")
    assert result["bytes"] == len("line1\nline2\n".encode())


def test_expected_prev_sha256_matches(tmp_path: Path) -> None:
    r1 = safe_write(tmp_path, path="a.txt", content="one\n")
    r2 = safe_write(
        tmp_path,
        path="a.txt",
        content="two\n",
        mode="overwrite",
        expected_prev_sha256=r1["sha256"],
    )
    assert r2["ok"] is True


def test_expected_prev_sha256_mismatch_rejects(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a.txt", content="one\n")
    with pytest.raises(ResilientWriteError) as exc:
        safe_write(
            tmp_path,
            path="a.txt",
            content="two\n",
            mode="overwrite",
            expected_prev_sha256="deadbeef" * 8,
        )
    assert exc.value.error == "stale_precondition"


def test_path_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        safe_write(tmp_path, path="../escape.txt", content="x")
    assert exc.value.error == "policy_violation"


def test_absolute_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        safe_write(tmp_path, path="/etc/passwd", content="x")
    assert exc.value.error == "policy_violation"


def test_nested_directories_created(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a/b/c/deep.txt", content="ok\n")
    assert (tmp_path / "a/b/c/deep.txt").read_text() == "ok\n"


def test_no_temp_files_left_on_success(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a.txt", content="x\n")
    leftovers = list(tmp_path.glob("**/*.tmp.*"))
    assert leftovers == []


def test_no_temp_files_left_on_rejected_create(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old")
    with pytest.raises(ResilientWriteError):
        safe_write(tmp_path, path="a.txt", content="new")
    leftovers = list(tmp_path.glob("**/*.tmp.*"))
    assert leftovers == []


def test_journal_is_jsonlines(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a.txt", content="x\n")
    safe_write(tmp_path, path="b.txt", content="y\n")
    raw = (tmp_path / ".resilient_write" / "journal.jsonl").read_text()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 2
    rows = [json.loads(ln) for ln in lines]
    assert {r["path"] for r in rows} == {"a.txt", "b.txt"}
