"""Append-only write journal.

Every successful `rw.safe_write` emits one JSON line to
`.resilient_write/journal.jsonl`.
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


def journal_path(state_root: Path) -> Path:
    return ensure_state_dir(state_root) / JOURNAL_FILENAME


def new_journal_id() -> str:
    ts_ns = time.time_ns() & 0xFFFFFFFFFFFF
    rnd = secrets.token_hex(4)
    return f"wj_{ts_ns:012x}{rnd}"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(
    state_root: Path, *, path: str, sha256: str,
    bytes_written: int, mode: str, caller: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "journal_id": new_journal_id(), "ts": utc_now_iso(),
        "path": path, "sha256": sha256, "bytes": bytes_written,
        "mode": mode, "caller": caller or "unknown",
    }
    jpath = journal_path(state_root)
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
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
                    "write_corruption", "encoding",
                    context={"journal": str(jpath), "bad_line": line[:200]},
                ) from exc


def tail(
    state_root: Path, *, n: int = 20,
    filter_path: str | None = None, filter_mode: str | None = None,
) -> list[dict[str, Any]]:
    if n <= 0:
        return []
    jpath = journal_path(state_root)
    entries: list[dict[str, Any]] = []
    for entry in _iter_entries(jpath):
        if filter_path is not None and entry.get("path") != filter_path:
            continue
        if filter_mode is not None and entry.get("mode") != filter_mode:
            continue
        entries.append(entry)
    return entries[-n:]
