"""Tests for the append-only journal."""

from __future__ import annotations

from pathlib import Path

from resilient_write import journal
from resilient_write.safe_write import safe_write


def test_tail_empty_when_no_writes(tmp_path: Path) -> None:
    assert journal.tail(tmp_path, n=10) == []


def test_tail_respects_n(tmp_path: Path) -> None:
    for i in range(5):
        safe_write(tmp_path, path=f"f{i}.txt", content=f"{i}\n")
    last_two = journal.tail(tmp_path, n=2)
    assert [e["path"] for e in last_two] == ["f3.txt", "f4.txt"]


def test_tail_filter_by_path(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a.txt", content="1\n")
    safe_write(tmp_path, path="b.txt", content="2\n")
    safe_write(tmp_path, path="a.txt", content="3\n", mode="overwrite")
    entries = journal.tail(tmp_path, n=10, filter_path="a.txt")
    assert len(entries) == 2
    assert all(e["path"] == "a.txt" for e in entries)


def test_tail_filter_by_mode(tmp_path: Path) -> None:
    safe_write(tmp_path, path="a.txt", content="1\n")
    safe_write(tmp_path, path="a.txt", content="2\n", mode="overwrite")
    safe_write(tmp_path, path="a.txt", content="3\n", mode="append")
    overwrites = journal.tail(tmp_path, n=10, filter_mode="overwrite")
    assert len(overwrites) == 1
    assert overwrites[0]["mode"] == "overwrite"


def test_journal_ids_are_unique(tmp_path: Path) -> None:
    ids = {
        safe_write(tmp_path, path=f"f{i}.txt", content=f"{i}\n")["journal_id"]
        for i in range(25)
    }
    assert len(ids) == 25
