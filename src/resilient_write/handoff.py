"""L5 — `rw.handoff`: task-continuity envelope.

The envelope is a Markdown file with a YAML front-matter header (see
`docs/HANDOFF_SCHEMA.md`). `handoff_write` serialises the structured
fields into the front-matter and writes the whole file through
`safe_write`, so the envelope is either fully written or not at all.

`last_good_state` is content-addressed: a resuming agent can compare the
recorded SHA-256s to the current on-disk hashes and detect drift. We
surface drift as a warning in the response, not an error, because a
fresh agent may still have useful work to do even if some files have
moved.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import safe_write as sw
from .errors import ResilientWriteError
from .journal import utc_now_iso
from .paths import ensure_state_dir, relative_to_workspace, resolve_in_workspace
from .safe_write import _file_sha256  # local helper, reused verbatim

# Lazy import to avoid circular dependency — checkpoint imports safe_write
# which is fine, but we don't want checkpoint at module level here.
_checkpoint_mod = None


def _get_checkpoint_mod():
    global _checkpoint_mod
    if _checkpoint_mod is None:
        from . import checkpoint as _cp
        _checkpoint_mod = _cp
    return _checkpoint_mod

DEFAULT_HANDOFF_FILENAME = "HANDOFF.md"
VALID_STATUSES = {"complete", "partial", "blocked", "handed_off"}

REQUIRED_FIELDS = (
    "task_id",
    "status",
    "agent",
    "summary",
    "next_steps",
    "last_good_state",
)


def _validate(envelope: dict[str, Any]) -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in envelope]
    if missing:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"missing_fields": missing},
        )
    status = envelope["status"]
    if status not in VALID_STATUSES:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"status": status, "valid": sorted(VALID_STATUSES)},
        )
    if not isinstance(envelope["next_steps"], list):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"field": "next_steps", "reason": "must_be_list"},
        )
    if not isinstance(envelope["last_good_state"], list):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"field": "last_good_state", "reason": "must_be_list"},
        )
    for i, entry in enumerate(envelope["last_good_state"]):
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                context={
                    "field": "last_good_state",
                    "index": i,
                    "reason": "each_entry_needs_path_and_sha256",
                },
            )


def _render(envelope: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(envelope, sort_keys=False, allow_unicode=True).rstrip()
    body = body.rstrip() + "\n" if body else ""
    parts = ["---\n", front, "\n---\n"]
    if body:
        parts.append("\n")
        parts.append(body)
    return "".join(parts)


def _parse(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": "missing_front_matter_delimiter"},
        )
    # Strip the leading '---\n'.
    rest = text[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": "unterminated_front_matter"},
        )
    front_text = rest[:end]
    try:
        data = yaml.safe_load(front_text) or {}
    except yaml.YAMLError as exc:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": f"yaml_error: {exc}"},
        ) from exc
    if not isinstance(data, dict):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": "front_matter_not_mapping"},
        )
    body = rest[end + 4 :].lstrip("\n")
    return data, body


def _check_drift(
    workspace: Path, last_good_state: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for entry in last_good_state:
        rel = entry["path"]
        expected = entry["sha256"]
        try:
            target = resolve_in_workspace(workspace, rel)
        except ResilientWriteError:
            warnings.append({"path": rel, "reason": "invalid_path"})
            continue
        if not target.exists():
            warnings.append({"path": rel, "reason": "missing"})
            continue
        actual = _file_sha256(target)
        if actual != expected:
            warnings.append(
                {
                    "path": rel,
                    "reason": "hash_mismatch",
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                }
            )
    return warnings


def handoff_write(
    workspace: Path,
    envelope: dict[str, Any],
    *,
    body: str = "",
    path: str = DEFAULT_HANDOFF_FILENAME,
    archive: bool = False,
    caller: str | None = None,
) -> dict[str, Any]:
    """Write a handoff envelope.

    - Validates required fields and enum values.
    - Fills in `updated_at` if absent.
    - Optionally archives the current envelope (if any) under
      `.resilient_write/handoffs/<ts>_<task_id>.md` before overwriting.
    - Emits the envelope atomically via `safe_write` (mode=overwrite).
    - Reports drift warnings for any `last_good_state` file whose
      current hash disagrees with the recorded one.
    """
    _validate(envelope)
    envelope = dict(envelope)  # don't mutate caller's dict
    envelope.setdefault("updated_at", utc_now_iso())

    target = resolve_in_workspace(workspace, path)

    if archive and target.exists():
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_dir = ensure_state_dir(workspace) / "handoffs"
        archive_dir.mkdir(parents=True, exist_ok=True)
        task_id = envelope["task_id"]
        archive_name = f"{ts}_{task_id}.md"
        shutil.copy2(target, archive_dir / archive_name)

    text = _render(envelope, body)
    mode: sw.WriteMode = "overwrite" if target.exists() else "create"
    result = sw.safe_write(
        workspace,
        path=path,
        content=text,
        mode=mode,
        caller=caller,
    )

    warnings = _check_drift(workspace, envelope["last_good_state"])

    # Auto-include checkpoint references so the next agent knows what
    # intermediate data is available on disk.
    cp_refs = _get_checkpoint_mod().list_checkpoint_refs(workspace)

    result_envelope: dict[str, Any] = {
        "ok": True,
        "handoff_path": result["path"],
        "sha256": result["sha256"],
        "bytes": result["bytes"],
        "journal_id": result["journal_id"],
        "drift_warnings": warnings,
    }
    if cp_refs:
        result_envelope["checkpoint_refs"] = cp_refs
    return result_envelope


def handoff_read(
    workspace: Path,
    *,
    path: str = DEFAULT_HANDOFF_FILENAME,
) -> dict[str, Any]:
    target = resolve_in_workspace(workspace, path)
    if not target.exists():
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            context={"path": path, "reason": "not_found"},
        )
    text = target.read_text(encoding="utf-8")
    envelope, body = _parse(text)
    _validate(envelope)
    warnings = _check_drift(workspace, envelope["last_good_state"])

    # Include available checkpoints so the resuming agent can see what
    # intermediate data persists from the prior session.
    cp_refs = _get_checkpoint_mod().list_checkpoint_refs(workspace)

    result_envelope: dict[str, Any] = {
        "ok": True,
        "handoff_path": relative_to_workspace(workspace, target),
        "envelope": envelope,
        "body": body,
        "drift_warnings": warnings,
    }
    if cp_refs:
        result_envelope["checkpoint_refs"] = cp_refs
    return result_envelope
