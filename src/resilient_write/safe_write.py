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


def safe_write(
    workspace: Path,
    *,
    path: str,
    content: str | None = None,
    content_bytes: bytes | None = None,
    mode: WriteMode = "create",
    expected_prev_sha256: str | None = None,
    caller: str | None = None,
    classify: bool = False,
    classify_reject_at: str = _DEFAULT_CLASSIFY_REJECT_AT,
) -> dict[str, Any]:
    """Transactionally write to `path` under `workspace`.

    Accepts either `content` (a UTF-8 string) or `content_bytes` (raw
    bytes). Exactly one must be provided. The bytes form exists for L4
    scratchpad callers that store non-text material and don't want to
    round-trip through base64 inside `safe_write` itself.

    When `classify=True`, the L0 classifier runs first over the string
    form of the content. Classify requires `content`; calling with
    `content_bytes` and `classify=True` raises a `policy_violation`
    (the caller should decode to a string themselves if they want to
    classify binary material).
    """
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
        policy = load_policy(workspace)
        report = score_content(
            content, policy=policy, target_path=path
        )
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

    target = resolve_in_workspace(workspace, path)
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
        # `content` is guaranteed non-None by the earlier guard.
        content_bytes = content.encode("utf-8")  # type: ignore[union-attr]

    # Mode preconditions.
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

    # Optional optimistic-concurrency guard.
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

    # Determine the final bytes on disk after the write.
    if mode == "append" and target_exists:
        existing = target.read_bytes()
        final_bytes = existing + content_bytes
    else:
        final_bytes = content_bytes

    expected_hash = _sha256(final_bytes)
    tmp = _tmp_path(target)

    try:
        # O_CREAT | O_EXCL — refuse to clobber a stray tmp file.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(final_bytes)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # fdopen took ownership; if the with-block re-raised, the
            # fd is already closed. Nothing further to do here.
            raise

        # Read-back verification.
        actual_hash = _file_sha256(tmp)
        if actual_hash != expected_hash:
            raise ResilientWriteError(
                "write_corruption",
                "unknown",
                context={
                    "path": path,
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
        # Classify the OS error to a reason_hint.
        if exc.errno in (13, 1):  # EACCES, EPERM
            reason = "permission"
            err_kind: str = "policy_violation"
        elif exc.errno in (28,):  # ENOSPC
            reason = "size_limit"
            err_kind = "quota_exceeded"
        else:
            reason = "unknown"
            err_kind = "policy_violation"
        raise ResilientWriteError(
            err_kind,  # type: ignore[arg-type]
            reason,  # type: ignore[arg-type]
            context={"path": path, "errno": exc.errno, "strerror": exc.strerror},
        ) from exc

    # Journal the success.
    rel = relative_to_workspace(workspace, target)
    entry = journal.append(
        workspace,
        path=rel,
        sha256=expected_hash,
        bytes_written=len(final_bytes),
        mode=mode,
        caller=caller,
    )

    return {
        "ok": True,
        "path": rel,
        "sha256": expected_hash,
        "bytes": len(final_bytes),
        "mode_applied": mode,
        "journal_id": entry["journal_id"],
        "wrote_at": entry["ts"],
    }


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
