"""L0 — `rw.risk_score`: pre-flight content classifier.

Pure function. No LLM, no network, no mutable state. Deterministic and
fast enough to run on every write (< 50 ms for 100 KB in practice).

Scoring:
- Each pattern family contributes its base weight once per file.
- Additional hits in the same family raise the contribution by 25% of
  the weight per extra hit, saturating at 1.5× the weight.
- Size heuristics add their fixed contributions.
- Final score is clamped to [0, 1].

Output shape matches `docs/API.md#rwrisk_score`. Match snippets are
truncated to 16 characters so the agent can locate a hit without the
response itself carrying the full secret — that would defeat the whole
purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .policy import CompiledPattern, Policy, SizeRule, default_policy, load_policy

_MATCH_SNIPPET_LEN = 16

# action-hint per family for suggested_actions aggregation
_FAMILY_ACTION: dict[str, str] = {
    "api_key": "redact",
    "github_pat": "redact",
    "jwt": "redact",
    "pem_block": "redact",
    "aws_secret": "redact",
    "pii": "redact",
    "binary_hint": "split",
}


@dataclass(frozen=True)
class _Hit:
    family: str
    name: str
    snippet: str
    line: int


def _truncate(text: str) -> str:
    text = text.replace("\n", "\\n")
    if len(text) <= _MATCH_SNIPPET_LEN:
        return text
    return text[:_MATCH_SNIPPET_LEN] + "…"


def _line_offsets(content: str) -> list[int]:
    """Byte offsets (char offsets, really) of the start of each line."""
    offsets = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _line_of(offsets: list[int], pos: int) -> int:
    # Binary search would be cleaner; linear is fine at these sizes.
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1  # 1-based


def _sweep_patterns(
    content: str, patterns: Iterable[CompiledPattern], disabled: frozenset[str]
) -> list[_Hit]:
    offsets = _line_offsets(content)
    hits: list[_Hit] = []
    for p in patterns:
        if p.family in disabled:
            continue
        for m in p.regex.finditer(content):
            hits.append(
                _Hit(
                    family=p.family,
                    name=p.name,
                    snippet=_truncate(m.group(0)),
                    line=_line_of(offsets, m.start()),
                )
            )
    return hits


def _family_contribution(weight: float, hit_count: int) -> float:
    if hit_count <= 0:
        return 0.0
    bump = 1.0 + 0.25 * (hit_count - 1)
    return weight * min(1.5, bump)


def _size_metrics(content: str) -> dict[str, int]:
    line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
    if content == "":
        line_count = 0
    # Longest line by character count.
    max_line_len = 0
    start = 0
    for i, ch in enumerate(content):
        if ch == "\n":
            ln = i - start
            if ln > max_line_len:
                max_line_len = ln
            start = i + 1
    tail = len(content) - start
    if tail > max_line_len:
        max_line_len = tail
    total_bytes = len(content.encode("utf-8"))
    return {
        "total_bytes": total_bytes,
        "max_line_len": max_line_len,
        "line_count": line_count,
    }


def _apply_size_rules(
    metrics: dict[str, int], rules: tuple[SizeRule, ...]
) -> list[tuple[SizeRule, int]]:
    triggered: list[tuple[SizeRule, int]] = []
    for rule in rules:
        value = metrics.get(rule.key, 0)
        if value > rule.gt:
            triggered.append((rule, value))
    return triggered


def _build_actions(
    hit_families: set[str], triggered: list[tuple[SizeRule, int]]
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    redact_targets = sorted(
        f for f in hit_families if _FAMILY_ACTION.get(f) == "redact"
    )
    if redact_targets:
        actions.append({"action": "redact", "targets": redact_targets})
    if "binary_hint" in hit_families:
        actions.append({"action": "split", "reason": "binary_content_detected"})
    for rule, value in triggered:
        reason = f"{rule.key}_{value}_exceeds_{rule.gt}"
        actions.append({"action": rule.suggested_action, "reason": reason})
    return actions


def score_content(
    content: str,
    *,
    policy: Policy | None = None,
    language_hint: str | None = None,
    target_path: str | None = None,
) -> dict[str, Any]:
    """Classify draft content and return the structured verdict envelope.

    `policy` defaults to the shipped defaults. Pass a workspace-loaded
    policy to honour `.resilient_write/policy.yaml` overrides.
    """
    pol = policy or default_policy()
    metrics = _size_metrics(content)

    hits = _sweep_patterns(content, pol.patterns, pol.disabled_families)
    by_family: dict[str, list[_Hit]] = {}
    for h in hits:
        by_family.setdefault(h.family, []).append(h)

    # Pattern score contribution.
    score = 0.0
    for family, family_hits in by_family.items():
        weight = pol.family_weights.get(family, 0.0)
        score += _family_contribution(weight, len(family_hits))

    # Size score contribution.
    triggered_size = _apply_size_rules(metrics, pol.size_rules)
    for rule, _value in triggered_size:
        score += rule.score

    score = max(0.0, min(1.0, score))
    verdict = pol.verdict(score)

    detected: list[dict[str, Any]] = []
    for h in hits:
        detected.append(
            {
                "kind": h.family,
                "pattern": h.name,
                "match": h.snippet,
                "line": h.line,
            }
        )
    for rule, value in triggered_size:
        detected.append(
            {
                "kind": "size",
                "pattern": rule.name,
                "match": None,
                "line": None,
                "value": value,
                "threshold": rule.gt,
            }
        )

    suggested = _build_actions(set(by_family.keys()), triggered_size)

    return {
        "ok": True,
        "score": round(score, 4),
        "verdict": verdict,
        "bytes": metrics["total_bytes"],
        "line_count": metrics["line_count"],
        "max_line_len": metrics["max_line_len"],
        "detected_patterns": detected,
        "suggested_actions": suggested,
        "language_hint": language_hint,
        "target_path": target_path,
    }


def score_for_workspace(
    workspace: Path,
    content: str,
    *,
    language_hint: str | None = None,
    target_path: str | None = None,
) -> dict[str, Any]:
    return score_content(
        content,
        policy=load_policy(workspace),
        language_hint=language_hint,
        target_path=target_path,
    )
