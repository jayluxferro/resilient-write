"""Journal analytics — `rw.analytics`.

Analyses the append-only `.resilient_write/journal.jsonl` to produce
structured metrics about write patterns, timing, and session health.
The entire journal is read in a single pass; all aggregation happens
in-memory so the function stays fast even on large journals.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .journal import _iter_entries, journal_path

# Matches journal paths written by chunk_write:
#   .resilient_write/chunks/<session>/part-NNN.txt
_CHUNK_PATH_RE = re.compile(
    r"^\.resilient_write/chunks/([A-Za-z0-9_\-]{1,64})/part-\d{3}\.txt$"
)

# Matches journal paths written by checkpoint_save:
#   .resilient_write/checkpoints/<name>.json
_CHECKPOINT_PATH_RE = re.compile(
    r"^\.resilient_write/checkpoints/([A-Za-z0-9_\-]{1,64})\.json$"
)


def _parse_ts(iso: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    The journal format uses ``%Y-%m-%dT%H:%M:%SZ`` (no fractional
    seconds), but `fromisoformat()` handles both with and without the
    trailing ``Z`` as of Python 3.11+.
    """
    # Python 3.10 fromisoformat doesn't accept trailing Z.
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


def _truncate_to_minute(iso: str) -> str:
    """Return the ``YYYY-MM-DDTHH:MMZ`` prefix of an ISO timestamp."""
    # The journal format is fixed-width, so slicing is safe.
    return iso[:16] + "Z"


def analyze_journal(
    workspace: Path,
    *,
    since: str | None = None,
    session_filter: str | None = None,
) -> dict[str, Any]:
    """Return a comprehensive analytics dict for the workspace journal.

    Parameters
    ----------
    workspace:
        Workspace root (the directory that contains `.resilient_write/`).
    since:
        Optional ISO-8601 timestamp. Entries with ``ts`` before this
        value are excluded from all aggregations.
    session_filter:
        If set, only chunk paths whose ``<session>`` segment matches
        this string are included in the ``sessions`` breakdown. All
        other counters still include every entry.
    """
    jpath = journal_path(workspace)

    since_dt: datetime | None = None
    if since is not None:
        since_dt = _parse_ts(since)

    # --- accumulators (single pass) ------------------------------------

    total_writes = 0
    total_bytes = 0
    paths_set: set[str] = set()
    by_mode: Counter[str] = Counter()
    by_caller: Counter[str] = Counter()

    # hot-path tracking: per-path write count, byte total, last timestamp
    path_writes: Counter[str] = Counter()
    path_bytes: defaultdict[str, int] = defaultdict(int)
    path_last_ts: dict[str, str] = {}

    # timeline: keep last 50 entries
    timeline: list[dict[str, Any]] = []
    _TIMELINE_LIMIT = 50

    # session tracking
    sessions: dict[str, dict[str, Any]] = {}

    # checkpoint tracking
    cp_total_saves = 0
    cp_overwrites = 0
    cp_by_name: dict[str, dict[str, Any]] = {}

    # velocity: per-minute write counts
    minute_counts: Counter[str] = Counter()

    first_ts: str | None = None
    last_ts: str | None = None

    for entry in _iter_entries(jpath):
        ts = entry.get("ts", "")

        # Apply `since` filter.
        if since_dt is not None:
            try:
                entry_dt = _parse_ts(ts)
            except (ValueError, TypeError):
                continue
            if entry_dt < since_dt:
                continue

        path = entry.get("path", "")
        nbytes = entry.get("bytes", 0)
        mode = entry.get("mode", "unknown")
        caller = entry.get("caller", "unknown")

        total_writes += 1
        total_bytes += nbytes
        paths_set.add(path)
        by_mode[mode] += 1
        by_caller[caller] += 1

        # hot paths
        path_writes[path] += 1
        path_bytes[path] += nbytes
        path_last_ts[path] = ts

        # timeline (sliding window)
        timeline.append({"ts": ts, "path": path, "bytes": nbytes, "mode": mode})
        if len(timeline) > _TIMELINE_LIMIT:
            timeline = timeline[-_TIMELINE_LIMIT:]

        # period bookkeeping
        if first_ts is None:
            first_ts = ts
        last_ts = ts

        # velocity per-minute bucket
        if ts:
            minute_counts[_truncate_to_minute(ts)] += 1

        # checkpoint detection
        cm = _CHECKPOINT_PATH_RE.match(path)
        if cm:
            cp_name = cm.group(1)
            cp_total_saves += 1
            if mode == "overwrite":
                cp_overwrites += 1
            cp_entry = cp_by_name.setdefault(cp_name, {
                "saves": 0,
                "total_bytes": 0,
                "first_write": ts,
                "last_write": ts,
            })
            cp_entry["saves"] += 1
            cp_entry["total_bytes"] += nbytes
            if ts:
                cp_entry["last_write"] = ts

        # chunk session detection
        m = _CHUNK_PATH_RE.match(path)
        if m:
            session_name = m.group(1)
            if session_filter is not None and session_name != session_filter:
                continue  # skip sessions that don't match the filter
            sess = sessions.setdefault(session_name, {
                "chunk_writes": 0,
                "composes": 0,
                "first_write": ts,
                "last_write": ts,
                "_last_chunk_ts": ts,
            })
            sess["chunk_writes"] += 1
            if ts:
                sess["last_write"] = ts
                sess["_last_chunk_ts"] = ts
        else:
            # Non-chunk write: check if it qualifies as a compose for any
            # active session. A "compose" is a write to a non-chunk path
            # that appeared *after* at least one chunk write in that
            # session (i.e., the session has been started).
            for sess in sessions.values():
                if sess["chunk_writes"] > 0:
                    sess["composes"] += 1

    # --- derived metrics -----------------------------------------------

    # hot_paths: top 5 most-written paths
    hot_paths: list[dict[str, Any]] = []
    for hp, wcount in path_writes.most_common(5):
        hot_paths.append({
            "path": hp,
            "write_count": wcount,
            "total_bytes": path_bytes[hp],
            "last_write": path_last_ts.get(hp, ""),
        })

    # session durations
    sessions_out: dict[str, Any] = {}
    for sname, sdata in sessions.items():
        first_w = sdata["first_write"]
        last_w = sdata["last_write"]
        try:
            duration = (_parse_ts(last_w) - _parse_ts(first_w)).total_seconds()
        except (ValueError, TypeError):
            duration = 0.0
        sessions_out[sname] = {
            "chunk_writes": sdata["chunk_writes"],
            "composes": sdata["composes"],
            "first_write": first_w,
            "last_write": last_w,
            "duration_seconds": duration,
        }

    # write velocity
    if first_ts and last_ts:
        try:
            span = (_parse_ts(last_ts) - _parse_ts(first_ts)).total_seconds()
        except (ValueError, TypeError):
            span = 0.0
    else:
        span = 0.0

    writes_per_minute = (total_writes / (span / 60.0)) if span > 0 else 0.0
    avg_bytes = (total_bytes / total_writes) if total_writes > 0 else 0.0

    peak_minute: str | None = None
    if minute_counts:
        peak_minute = minute_counts.most_common(1)[0][0]

    return {
        "ok": True,
        "total_writes": total_writes,
        "unique_paths": len(paths_set),
        "total_bytes_written": total_bytes,
        "by_mode": dict(by_mode),
        "by_caller": dict(by_caller),
        "timeline": timeline,
        "hot_paths": hot_paths,
        "sessions": sessions_out,
        "checkpoints": {
            "total_saves": cp_total_saves,
            "overwrites": cp_overwrites,
            "by_name": cp_by_name,
        },
        "write_velocity": {
            "writes_per_minute": writes_per_minute,
            "avg_bytes_per_write": avg_bytes,
            "peak_minute": peak_minute,
        },
        "period": {
            "first_entry": first_ts,
            "last_entry": last_ts,
            "duration_seconds": span,
        },
    }
