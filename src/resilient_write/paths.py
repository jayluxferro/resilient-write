"""Path resolution: state root, workspace boundary, and CWD anchoring.

Two independent concerns:

- **state root** — where ``.resilient_write/`` (journal, chunks, checkpoints,
  scratchpad) lives. ``$RW_STATE_DIR`` if set, otherwise the current working
  directory.
- **workspace roots** — the *access boundary* for user-supplied paths.
  Parsed from ``$RW_WORKSPACE`` (a single path, an ``os.pathsep``-separated
  list, or a JSON array). The current working directory is auto-added if
  not already inside one of the configured roots — so the agent's CWD is
  always inside its own access boundary.

Relative paths anchor at the CWD by default. Absolute paths are accepted
if they fall inside any workspace root. Boundary checks succeed when the
resolved target is contained in *any* workspace root.

The unsafe-root blocklist (``_UNSAFE_ROOTS``) refuses ``/`` and OS-critical
directories — in both their raw and ``Path.resolve()``-canonicalized forms
(``/etc``, ``/var`` and ``/tmp`` are symlinks into ``/private/...`` on
macOS). Setting ``$RW_ALLOW_UNSAFE_ROOTS`` to a truthy value turns the
refusal into a one-time stderr warning — the boundary stays explicit for
anyone who has not opted in.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath

from .errors import ResilientWriteError

STATE_DIRNAME = ".resilient_write"

_UNSAFE_ROOTS = frozenset({"/", "/bin", "/sbin", "/usr", "/etc", "/var", "/tmp"})

# On macOS /etc, /var and /tmp are symlinks into /private/..., so after
# Path.resolve() canonicalization the raw strings above would never match.
# Check both forms so the blocklist means what it says on every platform.
_UNSAFE_ROOTS_RESOLVED = _UNSAFE_ROOTS | frozenset(
    str(Path(raw).resolve()) for raw in _UNSAFE_ROOTS
)

# Escape hatch: a truthy $RW_ALLOW_UNSAFE_ROOTS turns the refusal below into
# a one-time warning per (env_var, root). Deliberately loud, never silent —
# the guard keeps protecting anyone who has not opted in.
ALLOW_UNSAFE_VAR = "RW_ALLOW_UNSAFE_ROOTS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_warned_unsafe: set[tuple[str, str]] = set()


def _env_flag(name: str) -> bool:
    """True when the env var holds a truthy value ("1", "true", "yes", "on")."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _warn_unsafe_bypass(env_var: str, root: Path) -> None:
    """Warn once per (env_var, root) that the unsafe-root guard is bypassed.

    ``_guard_unsafe`` runs on every tool call, so the warning must fire once
    per process — not once per call — to avoid stderr spam.
    """
    key = (env_var, str(root))
    if key in _warned_unsafe:
        return
    _warned_unsafe.add(key)
    print(
        f"resilient-write: WARNING: {ALLOW_UNSAFE_VAR} is set — bypassing "
        f"the unsafe-root guard for '{root}' as {env_var}. The workspace "
        "boundary no longer constrains writes; paths can reach OS-critical "
        "directories. Only use this if you accept that risk.",
        file=sys.stderr,
    )


def _parse_root_list(raw: str) -> list[Path]:
    """Parse env-var content into a list of resolved Paths.

    Accepts:
      - JSON array: ``["/a", "/b"]``
      - ``os.pathsep``-separated list: ``"/a:/b"``
      - Plain path: ``"/a"``
    """
    stripped = raw.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [Path(p).resolve() for p in parsed if p]
        except json.JSONDecodeError:
            pass
    if os.pathsep in stripped:
        return [Path(p).resolve() for p in stripped.split(os.pathsep) if p]
    return [Path(stripped).resolve()]


def _guard_unsafe(roots: list[Path], env_var: str) -> None:
    """Refuse roots in ``_UNSAFE_ROOTS`` unless ``$RW_ALLOW_UNSAFE_ROOTS`` is set."""
    blocked = [root for root in roots if str(root) in _UNSAFE_ROOTS_RESOLVED]
    if not blocked:
        return
    if _env_flag(ALLOW_UNSAFE_VAR):
        for root in blocked:
            _warn_unsafe_bypass(env_var, root)
        return
    print(
        f"resilient-write: refusing to use '{blocked[0]}' as {env_var}. "
        "Set the variable to your project directory.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _resolve_roots(env_var: str) -> list[Path]:
    """Return roots from env var, falling back to CWD."""
    override = os.environ.get(env_var)
    roots = _parse_root_list(override) if override else []
    if not roots:
        roots = [Path.cwd().resolve()]
    _guard_unsafe(roots, env_var)
    return roots


def _resolve_root_single(env_var: str) -> Path:
    return _resolve_roots(env_var)[0]


def state_root() -> Path:
    """Return the directory where ``.resilient_write/`` state lives.

    Uses ``$RW_STATE_DIR`` if set, otherwise the current working directory.
    State is intentionally decoupled from ``$RW_WORKSPACE`` — workspaces
    are an access boundary, not a state location.
    """
    if os.environ.get("RW_STATE_DIR"):
        return _resolve_root_single("RW_STATE_DIR")
    root = Path.cwd().resolve()
    if str(root) in _UNSAFE_ROOTS_RESOLVED:
        if _env_flag(ALLOW_UNSAFE_VAR):
            _warn_unsafe_bypass("state root (cwd)", root)
        else:
            print(
                f"resilient-write: refusing to use '{root}' as state root. "
                "Set $RW_STATE_DIR to your project directory.",
                file=sys.stderr,
            )
            raise SystemExit(1)
    return root


def workspace_roots() -> list[Path]:
    """Return workspace roots (the access boundary).

    Parsed from ``$RW_WORKSPACE``; if unset, falls back to CWD. The current
    working directory is always included — auto-added at the front if it
    isn't already inside one of the configured roots. This guarantees that
    relative paths anchored at CWD always satisfy the workspace boundary
    check.
    """
    override = os.environ.get("RW_WORKSPACE")
    configured = _parse_root_list(override) if override else []
    cwd = Path.cwd().resolve()

    if not configured:
        roots = [cwd]
    else:
        cwd_inside = any(_path_inside(cwd, ws) for ws in configured)
        roots = configured if cwd_inside else [cwd, *configured]

    _guard_unsafe(roots, "RW_WORKSPACE")
    return roots


def state_dir(state_root: Path) -> Path:
    """Return the state directory (not created)."""
    return state_root / STATE_DIRNAME


def ensure_state_dir(state_root: Path) -> Path:
    d = state_dir(state_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_inside(target: Path, root: Path) -> bool:
    """Return True if ``target`` is equal to or nested under ``root``."""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _containing_workspace(workspaces: list[Path], target: Path) -> Path | None:
    """Return the workspace root that contains ``target``, or None."""
    for ws in workspaces:
        if _path_inside(target, ws):
            return ws
    return None


def resolve_in_workspace(workspaces: list[Path], rel: str) -> Path:
    """Resolve a user-supplied path with CWD anchoring + workspace boundary.

    Rules:
      - Empty path → policy violation.
      - Relative path → anchored at CWD (NOT ``workspaces[0]``).
      - Absolute path → accepted only if it falls inside some workspace root.
      - Final target must be inside some workspace root.

    The CWD is guaranteed to be inside ``workspaces`` (see ``workspace_roots``),
    so plain relative paths always resolve safely.
    """
    if not rel:
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={"path": rel, "reason": "empty_path"},
        )

    candidate = PurePosixPath(rel)
    if candidate.is_absolute():
        target = Path(rel).resolve()
    else:
        target = (Path.cwd() / rel).resolve()

    if _containing_workspace(workspaces, target) is None:
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={
                "path": rel,
                "reason": "escapes_workspace",
                "resolved": str(target),
                "workspaces": [str(ws) for ws in workspaces],
            },
        )
    return target


def find_in_workspaces(workspaces: list[Path], rel: str) -> Path:
    """Locate ``rel`` for read-style operations.

    Search order: CWD first, then each workspace root (first match wins).
    Absolute paths are accepted if they fall inside any workspace root.
    If no match exists, falls back to a CWD-relative path so the caller
    can surface a consistent ``not_found`` error.
    """
    if not rel:
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={"path": rel, "reason": "empty_path"},
        )

    candidate = PurePosixPath(rel)
    if candidate.is_absolute():
        target = Path(rel).resolve()
        if _containing_workspace(workspaces, target) is None:
            raise ResilientWriteError(
                "policy_violation",
                "permission",
                context={
                    "path": rel,
                    "reason": "escapes_workspace",
                    "resolved": str(target),
                },
            )
        return target

    cwd = Path.cwd().resolve()
    cwd_candidate = (cwd / rel).resolve()
    if _path_inside(cwd_candidate, cwd) and cwd_candidate.exists():
        return cwd_candidate

    for ws in workspaces:
        ws_candidate = (ws / rel).resolve()
        if _path_inside(ws_candidate, ws) and ws_candidate.exists():
            return ws_candidate

    return cwd_candidate


def relative_to_workspace(workspaces: list[Path], target: Path) -> str:
    """Return a stable string identifier for ``target`` for journaling.

    Prefers a CWD-relative form when ``target`` is under CWD (the common
    case). Otherwise returns a path relative to the containing workspace
    root. Falls back to the absolute path if neither relativization works.
    """
    abs_target = target.resolve()
    cwd = Path.cwd().resolve()
    if _path_inside(abs_target, cwd):
        rel = abs_target.relative_to(cwd)
        return str(PurePosixPath(*rel.parts)) if rel.parts else "."

    ws = _containing_workspace(workspaces, abs_target)
    if ws is not None:
        rel = abs_target.relative_to(ws)
        return str(PurePosixPath(*rel.parts)) if rel.parts else "."

    return str(PurePosixPath(*abs_target.parts))
