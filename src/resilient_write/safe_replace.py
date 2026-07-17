"""L1 — `rw.safe_replace`: surgical transactional find-and-replace.

Reads an existing file, replaces exact occurrences of `old_string` with
`new_string`, and writes the result through the same atomic path as
`rw.safe_write` (temp file → fsync → SHA-256 verify → rename). The key
safety properties are:

- The caller does not have to provide (or risk corrupting) the full file
  content for a small edit.
- `expected_prev_sha256` guards against concurrent or stale edits.
- Default `count=1` requires exactly one match, preventing accidental
  multi-substitution.
- Every success is journaled with `mode="replace"` so the audit trail
  distinguishes surgical edits from overwrites.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import journal
from .errors import ResilientWriteError
from .paths import relative_to_workspace, resolve_in_workspace
from .safe_write import (
    _atomic_write_bytes,
    _classify_guard,
    _DEFAULT_CLASSIFY_REJECT_AT,
    _file_sha256,
    _sha256,
)


def safe_replace(
    workspaces: list[Path],
    *,
    path: str,
    old_string: str,
    new_string: str,
    count: int = 1,
    expected_prev_sha256: str | None = None,
    caller: str | None = None,
    classify: bool = False,
    classify_reject_at: str = _DEFAULT_CLASSIFY_REJECT_AT,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Surgically replace occurrences of `old_string` with `new_string`.

    Parameters
    ----------
    workspaces:
        Workspace roots used for path resolution and relativization.
    path:
        Existing file path (workspace-relative).
    old_string:
        Exact substring to locate.
    new_string:
        Replacement substring.
    count:
        ``1`` (default) requires exactly one match. ``-1`` replaces all
        matches. Any positive integer requires at least that many matches
        and replaces exactly that many.
    expected_prev_sha256:
        Optimistic-concurrency guard; must match the file's current hash.
    classify:
        Run the L0 classifier over `new_string` before writing.
    classify_reject_at:
        Minimum verdict that triggers rejection (``low``, ``medium``,
        ``high``).
    state_root:
        Where ``.resilient_write/`` lives. Defaults to ``workspaces[0]``.

    Returns
    -------
    Success dict with ``ok``, ``path``, ``abs_path``, ``sha256``,
    ``bytes``, ``mode_applied="replace"``, ``replacements``, ``journal_id``,
    and ``wrote_at``.
    """
    _state = state_root if state_root is not None else workspaces[0]

    if not old_string:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"path": path, "reason": "empty_old_string"},
        )

    if count == 0:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"path": path, "reason": "count_cannot_be_zero"},
        )

    target = resolve_in_workspace(workspaces, path)

    if not target.exists():
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            suggested_action="ask_user",
            context={"path": path, "reason": "file_not_found"},
        )

    if classify:
        _classify_guard(
            new_string,
            path=path,
            state_root=_state,
            classify_reject_at=classify_reject_at,
        )

    original_bytes = target.read_bytes()

    if expected_prev_sha256 is not None:
        current = _sha256(original_bytes)
        if current != expected_prev_sha256:
            raise ResilientWriteError(
                "stale_precondition",
                "unknown",
                suggested_action="ask_user",
                context={
                    "path": path,
                    "expected_prev_sha256": expected_prev_sha256,
                    "actual_prev_sha256": current,
                },
            )

    try:
        original_text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"path": path, "reason": f"not_utf8: {exc}"},
        ) from exc

    occurrences = original_text.count(old_string)
    if occurrences == 0:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            suggested_action="ask_user",
            context={
                "path": path,
                "reason": "old_string_not_found",
                "old_string": old_string,
            },
        )

    max_replacements = None if count == -1 else count
    if max_replacements is not None:
        if occurrences < max_replacements:
            raise ResilientWriteError(
                "stale_precondition",
                "unknown",
                suggested_action="ask_user",
                context={
                    "path": path,
                    "reason": "insufficient_matches",
                    "expected_count": max_replacements,
                    "actual_count": occurrences,
                    "old_string": old_string,
                },
            )
        if occurrences > max_replacements:
            raise ResilientWriteError(
                "stale_precondition",
                "unknown",
                suggested_action="ask_user",
                context={
                    "path": path,
                    "reason": "ambiguous_match",
                    "expected_count": max_replacements,
                    "actual_count": occurrences,
                    "old_string": old_string,
                },
            )

    new_text = original_text.replace(old_string, new_string, count)
    new_bytes = new_text.encode("utf-8")

    expected_hash, abs_path = _atomic_write_bytes(
        target, new_bytes, path_for_error=path
    )

    rel = relative_to_workspace(workspaces, target)
    entry = journal.append(
        _state,
        path=rel,
        sha256=expected_hash,
        bytes_written=len(new_bytes),
        mode="replace",
        caller=caller,
    )

    return {
        "ok": True,
        "path": rel,
        "abs_path": str(abs_path),
        "sha256": expected_hash,
        "bytes": len(new_bytes),
        "mode_applied": "replace",
        "replacements": occurrences if count == -1 else count,
        "journal_id": entry["journal_id"],
        "wrote_at": entry["ts"],
    }
