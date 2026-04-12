"""Append-only write journal.

Every successful `rw.safe_write` emits one JSON line to
`.resilient_write/journal.jsonl`. The journal is deliberately plain text
so it is trivially diffable, greppable, and auditable with nothing more
than `cat` or `git log` — per the decision recorded in
`.agent/memory/decisions.md` ("SQLite rejected").
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import ResilientWriteError
from .paths import ensure_state_dir

JOURNAL_FILENAME = "journal.jsonl"


def journal_path(workspace: Path) -> Path:
    return ensure_state_dir(workspace) / JOURNAL_FILENAME


def new_journal_id() -> str:
    """Return a monotonic-ish, 20-char, lexicographically-sortable id.

    Format: `wj_` + 12 hex chars of ns-timestamp + 8 hex of randomness.
    Not a ULID, but the sort order under `ls` is still time-ordered,
    which is all the journal really needs.
    """
    ts_ns = time.time_ns() & 0xFFFFFFFFFFFF
    rnd = secrets.token_hex(4)
    return f"wj_{ts_ns:012x}{rnd}"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(
    workspace: Path,
    *,
    path: str,
    sha256: str,
    bytes_written: int,
    mode: str,
    caller: str | None = None,
) -> dict[str, Any]:
    """Append one row to the journal and return the full entry."""
    entry: dict[str, Any] = {
        "journal_id": new_journal_id(),
        "ts": utc_now_iso(),
        "path": path,
        "sha256": sha256,
        "bytes": bytes_written,
        "mode": mode,
        "caller": caller or "unknown",
    }
    jpath = journal_path(workspace)
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
    # Append atomically at the OS level — O_APPEND guarantees a single
    # write() is not interleaved with another appender. We use a plain
    # text-mode append rather than temp+rename because the journal is
    # the one file in this project that is explicitly append-only.
    with jpath.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return entry


def _iter_entries(jpath: Path) -> Iterator[dict[str, Any]]:
    if not jpath.exists():
        return
    with jpath.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResilientWriteError(
                    "write_corruption",
                    "encoding",
                    context={"journal": str(jpath), "bad_line": line[:200]},
                ) from exc


def tail(
    workspace: Path,
    *,
    n: int = 20,
    filter_path: str | None = None,
    filter_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Return the last `n` journal entries, optionally filtered.

    Filtering happens after reading the full journal. The journal is
    small in practice (one row per write), so a streaming tail isn't
    worth the complexity yet.
    """
    if n <= 0:
        return []
    jpath = journal_path(workspace)
    entries: list[dict[str, Any]] = []
    for entry in _iter_entries(jpath):
        if filter_path is not None and entry.get("path") != filter_path:
            continue
        if filter_mode is not None and entry.get("mode") != filter_mode:
            continue
        entries.append(entry)
    return entries[-n:]
