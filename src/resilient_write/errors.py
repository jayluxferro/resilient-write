"""L3 — typed error envelope.

Every failure across L1/L2/L4/L5 raises a `ResilientWriteError` which
serialises to the envelope documented in `docs/ARCHITECTURE.md#layer-3`
and formally specified in `spec/errors.schema.json`. MCP tool adapters
catch the exception and turn `to_envelope()` into the tool's failure
payload so the calling agent can branch on structured fields rather
than parsing free text.

The envelope shape is versioned via `schema_version`. Every envelope
carries that field; consumers should read the version before
dispatching on the rest of the payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal

SCHEMA_VERSION = "1"
"""Envelope schema version. Bump on any breaking change to the shape."""

ErrorKind = Literal[
    "blocked",
    "stale_precondition",
    "write_corruption",
    "quota_exceeded",
    "policy_violation",
]

ReasonHint = Literal[
    "content_filter",
    "size_limit",
    "encoding",
    "permission",
    "network",
    "unknown",
]

SuggestedAction = Literal[
    "redact",
    "split",
    "escape",
    "ask_user",
    "retry_later",
    "abort",
]

ALL_ERROR_KINDS: tuple[ErrorKind, ...] = (
    "blocked",
    "stale_precondition",
    "write_corruption",
    "quota_exceeded",
    "policy_violation",
)

ALL_REASON_HINTS: tuple[ReasonHint, ...] = (
    "content_filter",
    "size_limit",
    "encoding",
    "permission",
    "network",
    "unknown",
)

ALL_SUGGESTED_ACTIONS: tuple[SuggestedAction, ...] = (
    "redact",
    "split",
    "escape",
    "ask_user",
    "retry_later",
    "abort",
)

# Reason hints that are worth retrying without operator intervention.
# `content_filter` is intentionally *not* retriable — retrying the same
# rejected content would thrash, and that was the original failure mode
# this project exists to stop. `unknown` is conservative: assume the
# agent should not loop on it without the user's call.
_RETRIABLE_REASONS: frozenset[str] = frozenset(
    {"network", "size_limit"}
)


class ResilientWriteError(Exception):
    """Typed error raised by any resilient-write layer.

    The envelope returned by `to_envelope()` is the public contract; see
    `spec/errors.schema.json` for the formal schema.
    """

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __init__(
        self,
        error: ErrorKind,
        reason_hint: ReasonHint,
        *,
        suggested_action: SuggestedAction = "abort",
        detected_patterns: list[str] | None = None,
        retry_budget: int = 0,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{error}: {reason_hint}")
        self.error: ErrorKind = error
        self.reason_hint: ReasonHint = reason_hint
        self.suggested_action: SuggestedAction = suggested_action
        self.detected_patterns: list[str] = list(detected_patterns or [])
        self.retry_budget: int = retry_budget
        self.context: dict[str, Any] = dict(context or {})

    def to_envelope(self) -> dict[str, Any]:
        """Serialise to the L3 envelope dict.

        The resulting dict is JSON-serialisable and conforms to
        `spec/errors.schema.json`.
        """
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": self.error,
            "reason_hint": self.reason_hint,
            "detected_patterns": list(self.detected_patterns),
            "suggested_action": self.suggested_action,
            "retry_budget": self.retry_budget,
            "context": dict(self.context),
        }

    def is_retriable(self) -> bool:
        """Heuristic: is this the kind of failure the agent can retry?

        See `docs/ERRORS.md` for the per-reason_hint rationale.
        """
        return self.reason_hint in _RETRIABLE_REASONS

    # ------------------------------------------------------------------
    # Factory classmethods — optional convenience for the common cases.
    # Direct construction still works and is used throughout the layer
    # modules; these exist so new call sites can express intent more
    # crisply without importing the enum literal lists.
    # ------------------------------------------------------------------

    @classmethod
    def blocked(
        cls,
        *,
        reason_hint: ReasonHint = "content_filter",
        detected_patterns: list[str] | None = None,
        retry_budget: int = 0,
        context: dict[str, Any] | None = None,
    ) -> "ResilientWriteError":
        return cls(
            "blocked",
            reason_hint,
            suggested_action="redact",
            detected_patterns=detected_patterns,
            retry_budget=retry_budget,
            context=context,
        )

    @classmethod
    def stale_precondition(
        cls,
        *,
        reason_hint: ReasonHint = "unknown",
        suggested_action: SuggestedAction = "ask_user",
        context: dict[str, Any] | None = None,
    ) -> "ResilientWriteError":
        return cls(
            "stale_precondition",
            reason_hint,
            suggested_action=suggested_action,
            context=context,
        )

    @classmethod
    def write_corruption(
        cls,
        *,
        reason_hint: ReasonHint = "unknown",
        context: dict[str, Any] | None = None,
    ) -> "ResilientWriteError":
        return cls(
            "write_corruption",
            reason_hint,
            suggested_action="abort",
            context=context,
        )

    @classmethod
    def policy_violation(
        cls,
        *,
        reason_hint: ReasonHint = "permission",
        suggested_action: SuggestedAction = "abort",
        context: dict[str, Any] | None = None,
    ) -> "ResilientWriteError":
        return cls(
            "policy_violation",
            reason_hint,
            suggested_action=suggested_action,
            context=context,
        )

    @classmethod
    def quota_exceeded(
        cls,
        *,
        reason_hint: ReasonHint = "size_limit",
        context: dict[str, Any] | None = None,
    ) -> "ResilientWriteError":
        return cls(
            "quota_exceeded",
            reason_hint,
            suggested_action="split",
            context=context,
        )


# ---------------------------------------------------------------------------
# Schema loading + validation
# ---------------------------------------------------------------------------


_ENVELOPE_SCHEMA_CACHE: dict[str, Any] | None = None


def _schema_candidate_paths() -> list[Path]:
    """Locations to try when reading the schema from disk.

    In a source checkout the canonical file lives at repo-root
    `spec/errors.schema.json`. In an installed wheel hatchling
    force-includes the same file under `resilient_write/_spec/`
    (see `pyproject.toml`), so `importlib.resources` finds it relative
    to the package. We try both so the loader works in either layout.
    """
    here = Path(__file__).resolve()
    return [
        here.parents[2] / "spec" / "errors.schema.json",  # source checkout
        here.parent / "_spec" / "errors.schema.json",  # installed wheel
    ]


def load_envelope_schema() -> dict[str, Any]:
    """Return the JSON Schema for the L3 envelope (cached).

    Tries the source-tree `spec/` location first, then the packaged
    `resilient_write/_spec/` location that hatchling force-includes
    into the wheel. Raises `FileNotFoundError` if neither exists, so
    callers that need to work in a stripped-down environment can pass
    their own schema dict to `validate_envelope()`.
    """
    global _ENVELOPE_SCHEMA_CACHE
    if _ENVELOPE_SCHEMA_CACHE is not None:
        return _ENVELOPE_SCHEMA_CACHE
    import json

    for path in _schema_candidate_paths():
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                _ENVELOPE_SCHEMA_CACHE = json.load(f)
            return _ENVELOPE_SCHEMA_CACHE
    raise FileNotFoundError(
        "errors.schema.json not found in any known location: "
        + ", ".join(str(p) for p in _schema_candidate_paths())
    )


def validate_envelope(
    envelope: dict[str, Any], *, schema: dict[str, Any] | None = None
) -> None:
    """Validate an envelope dict against the published schema.

    Raises `jsonschema.ValidationError` on mismatch. Uses the copy
    shipped under `spec/` by default; pass `schema` to override (e.g.
    in a packaged install without the spec directory).
    """
    import jsonschema

    jsonschema.validate(instance=envelope, schema=schema or load_envelope_schema())
