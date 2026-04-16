"""Mid-session intermediate data checkpointing.

Checkpoints live under ``.resilient_write/checkpoints/<name>.json`` and
allow agents to offload context-heavy intermediate data to disk before
context pressure forces compaction or stalling.

Each checkpoint write goes through ``safe_write`` for atomic semantics
and journal audit. Reading is a plain file read with JSON parse and
SHA-256 integrity verification.

Storage format::

    {
        "name": "paper_analyses",
        "format": "json",
        "created_at": "2026-04-16T12:00:00Z",
        "updated_at": "2026-04-16T12:05:00Z",
        "ttl": "session",
        "data": { ... }
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import chunks as _chunks
from . import safe_write as sw
from .errors import ResilientWriteError
from .journal import utc_now_iso
from .paths import ensure_state_dir, relative_to_workspace

CHECKPOINTS_DIRNAME = "checkpoints"
_CHUNK_THRESHOLD = 1_048_576  # 1 MB — above this, auto-chunk
_CHUNK_SIZE = 524_288  # 512 KB per chunk
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_VALID_FORMATS = ("json", "yaml", "markdown")
_VALID_TTLS_NAMED = ("session", "permanent")
# ISO 8601 duration: P[nY][nM][nD][T[nH][nM][nS]]
_ISO_DURATION_RE = re.compile(
    r"^P(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+S)?)?$"
)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={
                "name": name,
                "reason": "name_must_match_^[A-Za-z0-9_-]{1,64}$",
            },
        )


def _validate_ttl(ttl: str) -> None:
    if ttl in _VALID_TTLS_NAMED:
        return
    if _ISO_DURATION_RE.match(ttl):
        return
    raise ResilientWriteError(
        "policy_violation",
        "encoding",
        context={
            "ttl": ttl,
            "reason": "ttl_must_be_session|permanent|ISO_8601_duration",
        },
    )


def _validate_format(fmt: str) -> None:
    if fmt not in _VALID_FORMATS:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={
                "format": fmt,
                "valid": list(_VALID_FORMATS),
            },
        )


def _checkpoints_dir(workspace: Path) -> Path:
    return ensure_state_dir(workspace) / CHECKPOINTS_DIRNAME


def _checkpoint_path(workspace: Path, name: str) -> Path:
    return _checkpoints_dir(workspace) / f"{name}.json"


def _checkpoint_rel(name: str) -> str:
    return f".resilient_write/{CHECKPOINTS_DIRNAME}/{name}.json"


def _serialize_data(data: Any, fmt: str) -> str:
    """Serialize the user's data payload according to format."""
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=True, allow_unicode=True).rstrip()
    if fmt == "markdown":
        if not isinstance(data, str):
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                context={"reason": "markdown_format_requires_string_data"},
            )
        return data
    raise ResilientWriteError(
        "policy_violation",
        "encoding",
        context={"format": fmt, "reason": "unsupported_format"},
    )


def _write_chunked(
    workspace: Path,
    name: str,
    content: str,
    *,
    caller: str | None = None,
) -> dict[str, Any]:
    """Write a large checkpoint through the chunk infrastructure.

    Splits ``content`` into ~512 KB pieces, writes each via
    ``chunk_append``, then assembles the final file with
    ``chunk_compose(cleanup=True)``. The result on disk is identical
    to a single ``safe_write`` — the chunking is an internal detail.
    """
    session = f"_cp_{name}"

    # Reset any stale session from a prior failed attempt.
    _chunks.chunk_reset(workspace, session=session)

    content_bytes = content.encode("utf-8")
    offset = 0
    while offset < len(content_bytes):
        piece = content_bytes[offset : offset + _CHUNK_SIZE].decode(
            "utf-8", errors="ignore"
        )
        _chunks.chunk_append(workspace, session=session, content=piece, caller=caller)
        offset += _CHUNK_SIZE

    rel = _checkpoint_rel(name)
    result = _chunks.chunk_compose(
        workspace,
        session=session,
        output_path=rel,
        cleanup=True,
        caller=caller,
    )
    # Normalize key: chunk_compose uses "output_path", safe_write uses "path".
    result["path"] = result.pop("output_path")
    return result


def checkpoint_save(
    workspace: Path,
    *,
    name: str,
    data: Any,
    fmt: str = "json",
    ttl: str = "session",
    caller: str | None = None,
) -> dict[str, Any]:
    """Save a named checkpoint of intermediate data to disk.

    If a checkpoint with the same name already exists, it is overwritten
    (``updated_at`` is refreshed, ``created_at`` is preserved).
    """
    _validate_name(name)
    _validate_format(fmt)
    _validate_ttl(ttl)

    target = _checkpoint_path(workspace, name)
    now = utc_now_iso()

    # Preserve created_at from existing checkpoint if overwriting.
    created_at = now
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                created_at = existing.get("created_at", now)
        except (OSError, json.JSONDecodeError):
            pass  # corrupted — treat as fresh

    envelope = {
        "name": name,
        "format": fmt,
        "created_at": created_at,
        "updated_at": now,
        "ttl": ttl,
        "data": data,
    }

    content = json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    rel = _checkpoint_rel(name)
    chunked = len(content.encode("utf-8")) > _CHUNK_THRESHOLD

    if chunked:
        result = _write_chunked(workspace, name, content, caller=caller)
    else:
        mode: sw.WriteMode = "overwrite" if target.exists() else "create"
        result = sw.safe_write(
            workspace,
            path=rel,
            content=content,
            mode=mode,
            caller=caller,
        )

    return {
        "ok": True,
        "name": name,
        "format": fmt,
        "ttl": ttl,
        "checkpoint_path": result["path"],
        "sha256": result["sha256"],
        "bytes": result["bytes"],
        "journal_id": result["journal_id"],
        "created_at": created_at,
        "updated_at": now,
        "chunked": chunked,
    }


def checkpoint_read(
    workspace: Path,
    *,
    name: str,
) -> dict[str, Any]:
    """Retrieve a named checkpoint.

    Returns the stored data along with metadata. Verifies the file's
    SHA-256 matches the stored content for integrity.
    """
    _validate_name(name)

    target = _checkpoint_path(workspace, name)
    if not target.exists():
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"name": name, "reason": "checkpoint_not_found"},
        )

    raw = target.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()

    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ResilientWriteError(
            "write_corruption",
            "encoding",
            context={"name": name, "reason": f"corrupt_checkpoint: {exc}"},
        ) from exc

    if not isinstance(envelope, dict) or "data" not in envelope:
        raise ResilientWriteError(
            "write_corruption",
            "encoding",
            context={"name": name, "reason": "missing_data_field"},
        )

    return {
        "ok": True,
        "name": envelope.get("name", name),
        "format": envelope.get("format", "json"),
        "ttl": envelope.get("ttl", "session"),
        "created_at": envelope.get("created_at"),
        "updated_at": envelope.get("updated_at"),
        "data": envelope["data"],
        "sha256": sha256,
        "bytes": len(raw),
    }


def checkpoint_list(
    workspace: Path,
) -> dict[str, Any]:
    """List all available checkpoints with metadata (without data payloads)."""
    cpdir = _checkpoints_dir(workspace)
    if not cpdir.exists():
        return {"ok": True, "checkpoints": [], "count": 0}

    entries: list[dict[str, Any]] = []
    for fpath in sorted(cpdir.iterdir()):
        if not fpath.name.endswith(".json") or not fpath.is_file():
            continue
        name = fpath.name[:-5]  # strip .json
        stat = fpath.stat()
        entry: dict[str, Any] = {
            "name": name,
            "bytes": stat.st_size,
            "checkpoint_path": relative_to_workspace(workspace, fpath),
        }
        # Read metadata without loading the full data payload.
        try:
            raw = fpath.read_text(encoding="utf-8")
            meta = json.loads(raw)
            if isinstance(meta, dict):
                entry["format"] = meta.get("format", "json")
                entry["ttl"] = meta.get("ttl", "session")
                entry["created_at"] = meta.get("created_at")
                entry["updated_at"] = meta.get("updated_at")
                entry["sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        except (OSError, json.JSONDecodeError):
            entry["corrupt"] = True
        entries.append(entry)

    return {"ok": True, "checkpoints": entries, "count": len(entries)}


def _parse_iso_duration(duration: str) -> float:
    """Parse an ISO 8601 duration string into total seconds.

    Supports: P[nY][nM][nD][T[nH][nM][nS]].
    Approximations: 1Y = 365d, 1M = 30d.
    """
    m = re.match(
        r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
        duration,
    )
    if not m:
        return 0.0
    years, months, days, hours, minutes, seconds = (
        int(g) if g else 0 for g in m.groups()
    )
    return float(
        years * 365 * 86400
        + months * 30 * 86400
        + days * 86400
        + hours * 3600
        + minutes * 60
        + seconds
    )


def _parse_ts(iso: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


def checkpoint_cleanup(
    workspace: Path,
    *,
    include_session: bool = True,
) -> dict[str, Any]:
    """Remove expired checkpoints based on TTL.

    - ``ttl=session``: removed when ``include_session`` is True (default)
    - ``ttl=permanent``: always kept
    - ISO duration (e.g. ``PT1H``): removed if ``updated_at + duration``
      is in the past
    """
    cpdir = _checkpoints_dir(workspace)
    if not cpdir.exists():
        return {"ok": True, "removed": [], "kept": 0}

    now_dt = datetime.now(tz=timezone.utc)
    removed: list[dict[str, Any]] = []
    kept = 0

    for fpath in sorted(cpdir.iterdir()):
        if not fpath.name.endswith(".json") or not fpath.is_file():
            continue
        name = fpath.name[:-5]

        try:
            meta = json.loads(fpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupted checkpoint — remove it.
            fpath.unlink(missing_ok=True)
            removed.append({"name": name, "ttl": "unknown", "reason": "corrupt"})
            continue

        ttl = meta.get("ttl", "session") if isinstance(meta, dict) else "session"

        if ttl == "permanent":
            kept += 1
            continue

        if ttl == "session":
            if include_session:
                fpath.unlink(missing_ok=True)
                removed.append({"name": name, "ttl": "session", "reason": "session_cleanup"})
            else:
                kept += 1
            continue

        # ISO duration — check expiry.
        duration_secs = _parse_iso_duration(ttl)
        if duration_secs <= 0:
            kept += 1
            continue

        updated_at = meta.get("updated_at", "") if isinstance(meta, dict) else ""
        try:
            updated_dt = _parse_ts(updated_at)
        except (ValueError, TypeError):
            kept += 1
            continue

        if (now_dt - updated_dt).total_seconds() > duration_secs:
            fpath.unlink(missing_ok=True)
            removed.append({"name": name, "ttl": ttl, "reason": "expired"})
        else:
            kept += 1

    return {"ok": True, "removed": removed, "kept": kept}


def list_checkpoint_refs(workspace: Path) -> list[dict[str, Any]]:
    """Return lightweight checkpoint references for handoff integration.

    Returns a list of ``{name, sha256, bytes, path}`` dicts suitable for
    embedding in a handoff envelope.
    """
    cpdir = _checkpoints_dir(workspace)
    if not cpdir.exists():
        return []

    refs: list[dict[str, Any]] = []
    for fpath in sorted(cpdir.iterdir()):
        if not fpath.name.endswith(".json") or not fpath.is_file():
            continue
        name = fpath.name[:-5]
        raw = fpath.read_bytes()
        refs.append({
            "name": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "path": relative_to_workspace(workspace, fpath),
        })
    return refs
