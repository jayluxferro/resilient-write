"""L1 — `rw.safe_write`: transactional write.

Algorithm (see `docs/ARCHITECTURE.md` § Layer 1):

1. Resolve and validate the destination.
2. Enforce mode preconditions (create/overwrite/append) and
   `expected_prev_sha256` if provided.
3. Write the final bytes to `<parent>/<name>.tmp.<rand>` with fsync.
4. Re-read the temp file; verify the SHA-256 matches what we intended.
5. `os.replace(tmp, target)` — atomic on POSIX, stdlib handles Windows.
6. Append a journal row.
7. Return the success envelope.

Any failure path cleans up the temp file and raises `ResilientWriteError`
with an L3 envelope the caller can hand back to the agent.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any, Literal

from . import journal
from .errors import ResilientWriteError
from .paths import relative_to_workspace, resolve_in_workspace
from .policy import load_policy
from .risk_score import score_content

WriteMode = Literal["create", "overwrite", "append"]
_VALID_MODES: tuple[WriteMode, ...] = ("create", "overwrite", "append")
_DEFAULT_CLASSIFY_REJECT_AT = "high"
_VERDICT_RANK = {"safe": 0, "low": 1, "medium": 2, "high": 3}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _tmp_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.tmp.{secrets.token_hex(6)}")


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _classify_guard(
    content: str,
    *,
    path: str,
    state_root: Path,
    classify_reject_at: str,
) -> None:
    """Run the L0 classifier over `content` and raise if it hits the threshold."""
    policy = load_policy(state_root)
    report = score_content(content, policy=policy, target_path=path)
    threshold = _VERDICT_RANK.get(classify_reject_at, 3)
    if _VERDICT_RANK[report["verdict"]] >= threshold:
        hit_families = sorted(
            {p["kind"] for p in report["detected_patterns"]}
        )
        raise ResilientWriteError(
            "blocked",
            "content_filter",
            suggested_action="redact",
            detected_patterns=hit_families,
            retry_budget=policy.retry_budget,
            context={
                "path": path,
                "score": report["score"],
                "verdict": report["verdict"],
                "classify_reject_at": classify_reject_at,
                "detected": report["detected_patterns"],
                "suggested_actions": report["suggested_actions"],
            },
        )


def _atomic_write_bytes(
    target: Path,
    final_bytes: bytes,
    *,
    path_for_error: str,
) -> tuple[str, Path]:
    """Write `final_bytes` to `target` atomically (temp → fsync → hash → rename).

    Returns the SHA-256 hex digest and the resolved absolute path of the
    target. Cleans up the temp file on any failure path.
    """
    expected_hash = _sha256(final_bytes)
    tmp = _tmp_path(target)

    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(final_bytes)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            raise

        actual_hash = _file_sha256(tmp)
        if actual_hash != expected_hash:
            raise ResilientWriteError(
                "write_corruption",
                "unknown",
                context={
                    "path": path_for_error,
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual_hash,
                    "bytes": len(final_bytes),
                },
            )

        os.replace(str(tmp), str(target))
    except ResilientWriteError:
        _unlink_quiet(tmp)
        raise
    except OSError as exc:
        _unlink_quiet(tmp)
        if exc.errno in (13, 1):
            reason = "permission"
            err_kind: str = "policy_violation"
        elif exc.errno in (28,):
            reason = "size_limit"
            err_kind = "quota_exceeded"
        else:
            reason = "unknown"
            err_kind = "policy_violation"
        raise ResilientWriteError(
            err_kind,  # type: ignore[arg-type]
            reason,  # type: ignore[arg-type]
            context={"path": path_for_error, "errno": exc.errno, "strerror": exc.strerror},
        ) from exc

    return expected_hash, target.resolve()


def safe_write(
    workspaces: list[Path],
    *,
    path: str,
    content: str | None = None,
    content_bytes: bytes | None = None,
    mode: WriteMode = "create",
    expected_prev_sha256: str | None = None,
    caller: str | None = None,
    classify: bool = False,
    classify_reject_at: str = _DEFAULT_CLASSIFY_REJECT_AT,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Transactionally write to `path` under the first workspace.

    Accepts either `content` (a UTF-8 string) or `content_bytes` (raw
    bytes). Exactly one must be provided.

    When `classify=True`, the L0 classifier runs first over the string
    form of the content.

    ``state_root`` (optional) is where ``.resilient_write/`` lives —
    used for policy loading and journal. Defaults to the first workspace.
    """
    _state = state_root if state_root is not None else workspaces[0]

    if (content is None) == (content_bytes is None):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={
                "reason": "must_provide_exactly_one_of_content_or_content_bytes"
            },
        )

    if mode not in _VALID_MODES:
        raise ResilientWriteError(
            "policy_violation",
            "unknown",
            context={"mode": mode, "valid_modes": list(_VALID_MODES)},
        )

    if classify:
        if content is None:
            raise ResilientWriteError(
                "policy_violation",
                "encoding",
                context={
                    "reason": "classify_requires_content_str_not_bytes"
                },
            )
        _classify_guard(
            content, path=path, state_root=_state,
            classify_reject_at=classify_reject_at,
        )

    target = resolve_in_workspace(workspaces, path)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ResilientWriteError(
            "policy_violation",
            "permission",
            context={"path": path, "reason": f"mkdir_failed: {exc}"},
        ) from exc

    if content_bytes is None:
        content_bytes = content.encode("utf-8")  # type: ignore[union-attr]

    target_exists = target.exists()
    if mode == "create" and target_exists:
        raise ResilientWriteError(
            "stale_precondition",
            "unknown",
            suggested_action="ask_user",
            context={
                "path": path,
                "reason": "file_already_exists",
                "existing_sha256": _file_sha256(target),
            },
        )

    if expected_prev_sha256 is not None:
        current = _file_sha256(target) if target_exists else ""
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

    if mode == "append" and target_exists:
        existing = target.read_bytes()
        final_bytes = existing + content_bytes
    else:
        final_bytes = content_bytes

    expected_hash, abs_path = _atomic_write_bytes(
        target, final_bytes, path_for_error=path
    )

    rel = relative_to_workspace(workspaces, target)
    entry = journal.append(
        _state,
        path=rel,
        sha256=expected_hash,
        bytes_written=len(final_bytes),
        mode=mode,
        caller=caller,
    )

    return {
        "ok": True,
        "path": rel,
        "abs_path": str(abs_path),
        "sha256": expected_hash,
        "bytes": len(final_bytes),
        "mode_applied": mode,
        "journal_id": entry["journal_id"],
        "wrote_at": entry["ts"],
    }
