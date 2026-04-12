"""Workspace path resolution and validation.

All user-supplied paths are resolved relative to the workspace root and
refused if they escape it or point at an absolute location. The state
directory `.resilient_write/` lives at the workspace root.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import ResilientWriteError

STATE_DIRNAME = ".resilient_write"


def state_dir(workspace: Path) -> Path:
    """Return the state directory (not created). Callers that need it
    persisted should call `ensure_state_dir()`."""
    return workspace / STATE_DIRNAME


def ensure_state_dir(workspace: Path) -> Path:
    d = state_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_in_workspace(workspace: Path, rel: str) -> Path:
    """Resolve `rel` against `workspace` and verify the result stays
    inside it.

    Absolute paths, empty paths, and paths that traverse outside the
    workspace raise a `policy_violation`.
    """
    if not rel:
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={"path": rel, "reason": "empty_path"},
        )

    candidate = PurePosixPath(rel)
    if candidate.is_absolute():
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={"path": rel, "reason": "absolute_path_rejected"},
        )

    workspace_abs = workspace.resolve()
    target = (workspace_abs / rel).resolve()
    try:
        target.relative_to(workspace_abs)
    except ValueError as exc:
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={"path": rel, "reason": "escapes_workspace"},
        ) from exc
    return target


def relative_to_workspace(workspace: Path, target: Path) -> str:
    """Inverse of `resolve_in_workspace`; returns a forward-slash string
    suitable for journaling and envelope fields."""
    rel = target.resolve().relative_to(workspace.resolve())
    return str(PurePosixPath(*rel.parts))
