"""L4 — `rw.scratch_put` / `rw.scratch_ref` / `rw.scratch_get`.

Out-of-band storage for raw material (credentials, binaries, PII) that
must exist but does not belong in the main working tree. Files are
content-addressed: `.resilient_write/scratch/<sha256>.bin`. An
append-only `index.jsonl` records each `scratch_put` with its label,
content type, and timestamp, so multiple aliases can point at the same
underlying hash and dedup is free.

Reading scratched material back into the agent context is the one
operation with a real access-policy surface. `rw.scratch_get` honours
`$RW_SCRATCH_DISABLE_GET`: when that variable is set (to any
non-empty value), every read attempt returns a `policy_violation`
envelope so high-sensitivity workspaces can run the scratchpad in
write-only mode.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from . import safe_write as sw
from .errors import ResilientWriteError
from .journal import utc_now_iso
from .paths import ensure_state_dir, resolve_in_workspace

SCRATCH_DIRNAME = "scratch"
INDEX_FILENAME = "index.jsonl"
DISABLE_GET_ENV = "RW_SCRATCH_DISABLE_GET"

_VALID_ENCODINGS = ("utf-8", "base64")
_SHA256_HEX_LEN = 64


def _scratch_dir(workspace: Path) -> Path:
    return ensure_state_dir(workspace) / SCRATCH_DIRNAME


def _index_path(workspace: Path) -> Path:
    return _scratch_dir(workspace) / INDEX_FILENAME


def _scratch_rel(sha256: str) -> str:
    return f".resilient_write/{SCRATCH_DIRNAME}/{sha256}.bin"


def _validate_sha256(sha256: str) -> None:
    if (
        not isinstance(sha256, str)
        or len(sha256) != _SHA256_HEX_LEN
        or any(c not in "0123456789abcdef" for c in sha256)
    ):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"sha256": sha256, "reason": "not_a_lowercase_sha256_hex"},
        )


def _decode_input(content: str, encoding: str) -> bytes:
    if encoding == "utf-8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                context={"reason": f"bad_base64: {exc}"},
            ) from exc
    raise ResilientWriteError(
        "policy_violation",
        "encoding",
        context={
            "encoding": encoding,
            "valid": list(_VALID_ENCODINGS),
            "reason": "unsupported_encoding",
        },
    )


def _encode_output(payload: bytes, encoding: str) -> str:
    if encoding == "base64":
        return base64.b64encode(payload).decode("ascii")
    if encoding == "utf-8":
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                suggested_action="retry_later",
                context={
                    "reason": "not_valid_utf8",
                    "hint": "retry_with_encoding_base64",
                    "errno": exc.reason,
                },
            ) from exc
    raise ResilientWriteError(
        "policy_violation",
        "encoding",
        context={
            "encoding": encoding,
            "valid": list(_VALID_ENCODINGS),
            "reason": "unsupported_encoding",
        },
    )


def _append_index(workspace: Path, entry: dict[str, Any]) -> None:
    ipath = _index_path(workspace)
    ipath.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
    with ipath.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _iter_index(workspace: Path) -> Iterable[dict[str, Any]]:
    ipath = _index_path(workspace)
    if not ipath.exists():
        return
    with ipath.open("r", encoding="utf-8") as f:
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
                    context={"index": str(ipath), "bad_line": line[:200]},
                ) from exc


def _gitignore_covers_state(workspace: Path) -> bool:
    gi = workspace / ".gitignore"
    if not gi.exists():
        return False
    try:
        text = gi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    needles = {
        ".resilient_write",
        ".resilient_write/",
        "/.resilient_write",
        "/.resilient_write/",
        "**/.resilient_write",
        "**/.resilient_write/",
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in needles:
            return True
    return False


def _gitignore_warnings(workspace: Path) -> list[dict[str, Any]]:
    if _gitignore_covers_state(workspace):
        return []
    return [
        {
            "reason": "state_dir_not_gitignored",
            "hint": "add '.resilient_write/' to .gitignore to keep scratched material out of git",
        }
    ]


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def scratch_put(
    workspace: Path,
    *,
    content: str,
    label: str | None = None,
    content_type: str | None = None,
    notes: str | None = None,
    encoding: str = "utf-8",
    caller: str | None = None,
) -> dict[str, Any]:
    """Store raw material keyed by SHA-256.

    Same bytes → same path (deduplication). Every call adds a row to
    `index.jsonl`; callers can attach a label, content type, or free
    note that the index preserves verbatim. Warnings for a missing
    `.gitignore` entry surface in the response but never fail the put.
    """
    payload = _decode_input(content, encoding)
    sha256 = hashlib.sha256(payload).hexdigest()
    rel = _scratch_rel(sha256)

    target = resolve_in_workspace(workspace, rel)
    deduped = target.exists()
    journal_id: str | None = None
    if not deduped:
        result = sw.safe_write(
            workspace,
            path=rel,
            content_bytes=payload,
            mode="create",
            caller=caller,
        )
        journal_id = result["journal_id"]

    entry: dict[str, Any] = {
        "sha256": sha256,
        "label": label,
        "content_type": content_type,
        "bytes": len(payload),
        "encoding": encoding,
        "notes": notes,
        "created_at": utc_now_iso(),
    }
    _append_index(workspace, entry)

    return {
        "ok": True,
        "sha256": sha256,
        "scratch_path": rel,
        "bytes": len(payload),
        "deduped": deduped,
        "journal_id": journal_id,
        "warnings": _gitignore_warnings(workspace),
    }


def scratch_ref(
    workspace: Path,
    *,
    sha256: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Look up an index entry without returning content.

    Accepts either `sha256` or `label`. If both are provided, `sha256`
    takes precedence. If multiple entries match, the most recent one
    wins.
    """
    if sha256 is None and label is None:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": "must_provide_sha256_or_label"},
        )
    if sha256 is not None:
        _validate_sha256(sha256)

    matches: list[dict[str, Any]] = []
    for entry in _iter_index(workspace):
        if sha256 is not None and entry.get("sha256") != sha256:
            continue
        if label is not None and entry.get("label") != label:
            continue
        matches.append(entry)

    if not matches:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"sha256": sha256, "label": label, "reason": "not_found"},
        )

    entry = matches[-1]
    # Confirm the underlying .bin still exists — drift can happen if a
    # user manually rm'd the scratch directory.
    target = resolve_in_workspace(workspace, _scratch_rel(entry["sha256"]))
    return {
        "ok": True,
        "entry": entry,
        "scratch_path": _scratch_rel(entry["sha256"]),
        "bin_exists": target.exists(),
        "alias_count": len(matches),
    }


def scratch_get(
    workspace: Path,
    *,
    sha256: str,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Return raw content by hash.

    Respects `$RW_SCRATCH_DISABLE_GET`. When that variable is set to a
    non-empty value, reads are refused with a `policy_violation` /
    `permission` envelope so write-only high-sensitivity setups don't
    accidentally resurface the material through the agent.
    """
    if os.environ.get(DISABLE_GET_ENV):
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            suggested_action="abort",
            context={
                "reason": "scratch_get_disabled",
                "env": DISABLE_GET_ENV,
            },
        )

    _validate_sha256(sha256)
    if encoding not in _VALID_ENCODINGS:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={
                "encoding": encoding,
                "valid": list(_VALID_ENCODINGS),
                "reason": "unsupported_encoding",
            },
        )

    rel = _scratch_rel(sha256)
    target = resolve_in_workspace(workspace, rel)
    if not target.exists():
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"sha256": sha256, "reason": "not_found"},
        )

    payload = target.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != sha256:
        raise ResilientWriteError(
            "write_corruption",
            "unknown",
            context={
                "sha256": sha256,
                "actual_sha256": actual,
                "reason": "hash_drift_on_read",
            },
        )

    # Attach the most-recent index entry's metadata if present (purely
    # informational; missing index rows are not an error).
    metadata: dict[str, Any] | None = None
    for entry in _iter_index(workspace):
        if entry.get("sha256") == sha256:
            metadata = entry

    return {
        "ok": True,
        "sha256": sha256,
        "content": _encode_output(payload, encoding),
        "encoding": encoding,
        "bytes": len(payload),
        "content_type": (metadata or {}).get("content_type"),
        "label": (metadata or {}).get("label"),
    }
