"""L2 — `rw.chunk_write` / `rw.chunk_compose` / `rw.chunk_reset`.

Sessions live under `.resilient_write/chunks/<session>/`:

    part-001.txt  part-002.txt  ...  manifest.json

Each `chunk_write` goes through `rw.safe_write` with `mode=overwrite`,
so retrying a failing chunk is safe and re-hashes end up in the L1
journal for audit. `chunk_compose` enumerates the `part-*.txt` files on
disk (the chunk files — not the manifest — are the source of truth),
verifies the index set is contiguous from 1, concatenates them in
order, and writes the final file via `safe_write` again.

The manifest is a small JSON document that records `total_expected` so
`chunk_compose` can detect "caller thinks there are 8 chunks but only 7
exist" before touching `output_path`. It's deliberately tiny; losing it
is recoverable because the chunk files themselves carry the payload.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from . import safe_write as sw
from .errors import ResilientWriteError
from .journal import utc_now_iso
from .paths import ensure_state_dir, relative_to_workspace

CHUNKS_DIRNAME = "chunks"
MANIFEST_FILENAME = "manifest.json"
PART_TEMPLATE = "part-{index:03d}.txt"
_PART_RE = re.compile(r"^part-(\d{3})\.txt$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_MAX_INDEX = 999


def _validate_session(session: str) -> None:
    if not isinstance(session, str) or not _SESSION_RE.match(session):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={
                "session": session,
                "reason": "session_must_match_^[A-Za-z0-9_-]{1,64}$",
            },
        )


def _validate_index(index: int, total_expected: int | None) -> None:
    if not isinstance(index, int) or index < 1 or index > _MAX_INDEX:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={
                "index": index,
                "reason": f"index_must_be_int_in_1..{_MAX_INDEX}",
            },
        )
    if total_expected is not None:
        if (
            not isinstance(total_expected, int)
            or total_expected < 1
            or total_expected > _MAX_INDEX
        ):
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                context={
                    "total_expected": total_expected,
                    "reason": f"total_expected_must_be_int_in_1..{_MAX_INDEX}",
                },
            )
        if index > total_expected:
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                context={
                    "index": index,
                    "total_expected": total_expected,
                    "reason": "index_exceeds_total_expected",
                },
            )


def _session_dir(workspace: Path, session: str) -> Path:
    return ensure_state_dir(workspace) / CHUNKS_DIRNAME / session


def _session_rel(session: str, name: str) -> str:
    # Always forward-slash for MCP and journal consistency.
    return f".resilient_write/{CHUNKS_DIRNAME}/{session}/{name}"


def _manifest_path(workspace: Path, session: str) -> Path:
    return _session_dir(workspace, session) / MANIFEST_FILENAME


def _read_manifest(workspace: Path, session: str) -> dict[str, Any] | None:
    mpath = _manifest_path(workspace, session)
    if not mpath.exists():
        return None
    try:
        data = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResilientWriteError(
            "write_corruption",
            "encoding",
            context={"manifest": str(mpath), "reason": f"bad_manifest: {exc}"},
        ) from exc
    if not isinstance(data, dict):
        raise ResilientWriteError(
            "write_corruption",
            "encoding",
            context={"manifest": str(mpath), "reason": "not_mapping"},
        )
    return data


def _write_manifest_atomic(workspace: Path, session: str, data: dict[str, Any]) -> None:
    """Atomic manifest write without journalling.

    The manifest is internal bookkeeping derived from the chunk files
    themselves. Routing it through `safe_write` would add a row to the
    L1 journal for every chunk, which is audit noise — the chunk files
    already get their own journal rows. A temp-file + `os.replace` is
    enough here.
    """
    mpath = _manifest_path(workspace, session)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = mpath.with_name(f"{mpath.name}.tmp.{secrets.token_hex(6)}")
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(str(tmp), str(mpath))


def _list_chunks(workspace: Path, session: str) -> list[tuple[int, Path]]:
    sdir = _session_dir(workspace, session)
    if not sdir.exists():
        return []
    found: list[tuple[int, Path]] = []
    for entry in sdir.iterdir():
        m = _PART_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry))
    found.sort(key=lambda t: t[0])
    return found


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def chunk_write(
    workspace: Path,
    *,
    session: str,
    index: int,
    content: str,
    total_expected: int | None = None,
    caller: str | None = None,
) -> dict[str, Any]:
    """Write one chunk of a compose session."""
    _validate_session(session)
    _validate_index(index, total_expected)

    # Ensure session dir exists so manifest / part files can land there.
    sdir = _session_dir(workspace, session)
    sdir.mkdir(parents=True, exist_ok=True)

    part_name = PART_TEMPLATE.format(index=index)
    rel = _session_rel(session, part_name)

    result = sw.safe_write(
        workspace,
        path=rel,
        content=content,
        mode="overwrite",
        caller=caller,
    )

    # Update manifest. Preserve created_at if present; overwrite
    # total_expected to the latest caller-supplied value (most recent
    # wins, so an agent can tighten the count as it learns).
    existing = _read_manifest(workspace, session) or {}
    manifest: dict[str, Any] = {
        "session": session,
        "created_at": existing.get("created_at", utc_now_iso()),
        "updated_at": utc_now_iso(),
        "total_expected": (
            total_expected
            if total_expected is not None
            else existing.get("total_expected")
        ),
    }
    _write_manifest_atomic(workspace, session, manifest)

    return {
        "ok": True,
        "session": session,
        "index": index,
        "chunk_path": result["path"],
        "sha256": result["sha256"],
        "bytes": result["bytes"],
        "journal_id": result["journal_id"],
    }


def chunk_compose(
    workspace: Path,
    *,
    session: str,
    output_path: str,
    separator: str = "",
    cleanup: bool = False,
    caller: str | None = None,
) -> dict[str, Any]:
    """Concatenate a session's chunks into `output_path` via safe_write."""
    _validate_session(session)

    sdir = _session_dir(workspace, session)
    if not sdir.exists():
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"session": session, "reason": "session_not_found"},
        )

    chunks = _list_chunks(workspace, session)
    if not chunks:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"session": session, "reason": "no_chunks_found"},
        )

    # Verify contiguity from 1.
    indices = [i for i, _ in chunks]
    expected = list(range(1, len(indices) + 1))
    if indices != expected:
        missing = sorted(set(expected) - set(indices))
        unexpected = sorted(set(indices) - set(expected))
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={
                "session": session,
                "reason": "non_contiguous_chunks",
                "have": indices,
                "missing": missing,
                "unexpected": unexpected,
            },
        )

    # Reconcile with manifest's total_expected, if present.
    manifest = _read_manifest(workspace, session) or {}
    total_expected = manifest.get("total_expected")
    if isinstance(total_expected, int) and len(indices) != total_expected:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={
                "session": session,
                "reason": "chunk_count_mismatch",
                "have": len(indices),
                "total_expected": total_expected,
            },
        )

    # Read chunks, record per-chunk hashes for the response.
    import hashlib

    parts: list[bytes] = []
    chunk_hashes: list[str] = []
    for _index, path in chunks:
        data = path.read_bytes()
        parts.append(data)
        chunk_hashes.append(hashlib.sha256(data).hexdigest())

    sep_bytes = separator.encode("utf-8")
    composed = sep_bytes.join(parts)
    try:
        composed_text = composed.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResilientWriteError(
            "write_corruption",
            "encoding",
            context={
                "session": session,
                "reason": f"composed_not_utf8: {exc}",
            },
        ) from exc

    # Choose mode based on whether output file already exists. The
    # caller shouldn't have to reason about create-vs-overwrite here —
    # compose is a "materialise this session" operation.
    target_abs = (workspace / output_path).resolve()
    mode: sw.WriteMode = "overwrite" if target_abs.exists() else "create"

    result = sw.safe_write(
        workspace,
        path=output_path,
        content=composed_text,
        mode=mode,
        caller=caller,
    )

    if cleanup:
        shutil.rmtree(sdir, ignore_errors=True)

    return {
        "ok": True,
        "session": session,
        "output_path": result["path"],
        "sha256": result["sha256"],
        "bytes": result["bytes"],
        "chunk_count": len(chunks),
        "chunk_hashes": chunk_hashes,
        "journal_id": result["journal_id"],
        "cleaned_up": bool(cleanup),
    }


def chunk_reset(
    workspace: Path,
    *,
    session: str,
) -> dict[str, Any]:
    """Destructively wipe a chunk session."""
    _validate_session(session)
    sdir = _session_dir(workspace, session)
    if not sdir.exists():
        return {"ok": True, "session": session, "removed": 0, "existed": False}
    removed = 0
    for entry in sdir.rglob("*"):
        if entry.is_file():
            removed += 1
    shutil.rmtree(sdir, ignore_errors=True)
    return {"ok": True, "session": session, "removed": removed, "existed": True}


def chunk_append(
    workspace: Path,
    *,
    session: str,
    content: str,
    total_expected: int | None = None,
    caller: str | None = None,
) -> dict[str, Any]:
    """Auto-incrementing chunk write.

    Inspects the session directory to find the highest existing index,
    then writes ``index + 1``.  If the session doesn't exist yet, starts
    at 1.  This removes the need for the caller to track indices — each
    call just appends the next piece.
    """
    _validate_session(session)
    existing = _list_chunks(workspace, session)
    next_index = (existing[-1][0] + 1) if existing else 1
    return chunk_write(
        workspace,
        session=session,
        index=next_index,
        content=content,
        total_expected=total_expected,
        caller=caller,
    )


def chunk_status(
    workspace: Path,
    *,
    session: str,
) -> dict[str, Any]:
    """Inspection helper: report the current state of a session.

    Not part of the original L2 surface but trivial to derive and handy
    for the caller to decide which chunk to retry without touching
    disk themselves.
    """
    _validate_session(session)
    sdir = _session_dir(workspace, session)
    if not sdir.exists():
        return {"ok": True, "session": session, "exists": False}
    manifest = _read_manifest(workspace, session) or {}
    chunks = _list_chunks(workspace, session)
    return {
        "ok": True,
        "session": session,
        "exists": True,
        "total_expected": manifest.get("total_expected"),
        "present_indices": [i for i, _ in chunks],
        "chunk_dir": relative_to_workspace(workspace, sdir),
    }


def chunk_preview(
    workspace: Path,
    *,
    session: str,
    separator: str = "",
) -> dict[str, Any]:
    """Dry-run compose: return concatenated content without writing.

    Performs all the same validation as chunk_compose (contiguity check,
    total_expected reconciliation) but returns the content string instead
    of writing it to disk. Useful for pre-compose validation: the agent
    can run rw.validate on the preview before committing.
    """
    _validate_session(session)

    sdir = _session_dir(workspace, session)
    if not sdir.exists():
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"session": session, "reason": "session_not_found"},
        )

    chunks = _list_chunks(workspace, session)
    if not chunks:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"session": session, "reason": "no_chunks_found"},
        )

    # Verify contiguity from 1.
    indices = [i for i, _ in chunks]
    expected = list(range(1, len(indices) + 1))
    if indices != expected:
        missing = sorted(set(expected) - set(indices))
        unexpected = sorted(set(indices) - set(expected))
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={
                "session": session,
                "reason": "non_contiguous_chunks",
                "have": indices,
                "missing": missing,
                "unexpected": unexpected,
            },
        )

    # Reconcile with manifest's total_expected, if present.
    manifest = _read_manifest(workspace, session) or {}
    total_expected = manifest.get("total_expected")
    if isinstance(total_expected, int) and len(indices) != total_expected:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={
                "session": session,
                "reason": "chunk_count_mismatch",
                "have": len(indices),
                "total_expected": total_expected,
            },
        )

    # Read chunks, record per-chunk hashes for the response.
    import hashlib

    parts: list[bytes] = []
    chunk_hashes: list[str] = []
    for _index, path in chunks:
        data = path.read_bytes()
        parts.append(data)
        chunk_hashes.append(hashlib.sha256(data).hexdigest())

    sep_bytes = separator.encode("utf-8")
    composed = sep_bytes.join(parts)
    try:
        composed_text = composed.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResilientWriteError(
            "write_corruption",
            "encoding",
            context={
                "session": session,
                "reason": f"composed_not_utf8: {exc}",
            },
        ) from exc

    return {
        "ok": True,
        "session": session,
        "content": composed_text,
        "chunk_count": len(chunks),
        "chunk_hashes": chunk_hashes,
        "total_bytes": len(composed),
        "preview": True,
    }
