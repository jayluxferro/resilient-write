"""Path resolution: state root vs workspace boundary.

Two independent roots:

- **state root** — where ``.resilient_write/`` lives. Defaults to ``$PWD``,
  overridable via ``$RW_STATE_DIR``.
- **workspace roots** — access boundary for user-supplied paths. Defaults to
  ``$PWD``, overridable via ``$RW_WORKSPACE`` (a ``os.pathsep``-separated
  list). The first workspace is the write target; all are searched for reads.

By default both point to the same directory, so existing setups are unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

from .errors import ResilientWriteError

STATE_DIRNAME = ".resilient_write"

_UNSAFE_ROOTS = frozenset({"/", "/bin", "/sbin", "/usr", "/etc", "/var", "/tmp"})


def _resolve_roots(env_var: str) -> list[Path]:
    """Return workspace roots from env var.

    Accepts two formats:
      - A plain path string (e.g. ``"/Users/jay"``) — backward compatible.
      - A JSON array of paths (e.g. ``["/Users/jay", "/Volumes/Lux/dev/"]``).
    """
    override = os.environ.get(env_var)
    if override:
        stripped = override.strip()
        if stripped.startswith("["):
            try:
                raw = json.loads(stripped)
                if isinstance(raw, list):
                    roots = [Path(p).resolve() for p in raw if p]
                else:
                    roots = [Path(stripped).resolve()]
            except json.JSONDecodeError:
                roots = [Path(stripped).resolve()]
        else:
            roots = [Path(stripped).resolve()]
    else:
        roots = [Path.cwd().resolve()]
    for root in roots:
        if str(root) in _UNSAFE_ROOTS:
            import sys
            print(
                f"resilient-write: refusing to use '{root}' as {env_var}. "
                "Set the variable to your project directory.",
                file=sys.stderr,
            )
            raise SystemExit(1)
    return roots or [Path.cwd().resolve()]


def _resolve_root_single(env_var: str) -> Path:
    """Return a single root from env var (first value if multi)."""
    return _resolve_roots(env_var)[0]


def _resolve_root(env_var: str) -> Path:
    """Backward-compat: return a single root."""
    return _resolve_root_single(env_var)


def state_root() -> Path:
    """Return the directory where ``.resilient_write/`` state lives.

    Uses ``$RW_STATE_DIR`` if set, otherwise the first ``$RW_WORKSPACE`` if set,
    otherwise ``$PWD``. This ensures backward compatibility: when only
    ``$RW_WORKSPACE`` is configured (as in tests and existing setups),
    state remains colocated with the first workspace.
    """
    if os.environ.get("RW_STATE_DIR"):
        return _resolve_root_single("RW_STATE_DIR")
    if os.environ.get("RW_WORKSPACE"):
        return _resolve_root_single("RW_WORKSPACE")
    root = Path.cwd().resolve()
    if str(root) in _UNSAFE_ROOTS:
        import sys
        print(
            f"resilient-write: refusing to use '{root}' as state root. "
            "Set $RW_STATE_DIR to your project directory.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return root


def workspace_roots() -> list[Path]:
    """Return workspace roots for user-supplied write/read paths.

    Uses ``$RW_WORKSPACE`` if set, otherwise ``$PWD``. The first workspace
    is the primary write target.
    """
    return _resolve_roots("RW_WORKSPACE")


def state_dir(state_root: Path) -> Path:
    """Return the state directory (not created). Callers that need it
    persisted should call `ensure_state_dir()`."""
    return state_root / STATE_DIRNAME


def ensure_state_dir(state_root: Path) -> Path:
    d = state_dir(state_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_in_workspace(workspaces: list[Path], rel: str) -> Path:
    """Resolve `rel` against the first workspace and verify the result stays
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

    workspace_abs = workspaces[0].resolve()
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


def find_in_workspaces(workspaces: list[Path], rel: str) -> Path:
    """Search for `rel` across all workspaces (first match wins).

    Absolute paths and empty paths are rejected. Unlike
    `resolve_in_workspace`, this does NOT enforce a workspace boundary —
    it just tries each root until the file is found.
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
    for ws in workspaces:
        target = (ws.resolve() / rel).resolve()
        if target.exists():
            return target
    # Fall back to first workspace for error consistency
    return (workspaces[0].resolve() / rel).resolve()


def relative_to_workspace(workspaces: list[Path], target: Path) -> str:
    """Inverse of `resolve_in_workspace`; returns a forward-slash string
    suitable for journaling and envelope fields. Uses the first workspace
    as the reference root."""
    rel = target.resolve().relative_to(workspaces[0].resolve())
    return str(PurePosixPath(*rel.parts))
