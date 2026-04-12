"""L0 classifier policy: defaults + workspace override merge.

The defaults in this module are the authoritative source for what
`docs/POLICY.md` documents. If you change one, update the other.

Workspace override file: `.resilient_write/policy.yaml`. Schema:

    version: 1
    extend_patterns:
      api_key:
        - name: internal_vendor_key
          regex: 'VN-[0-9A-F]{24}'
          weight: 0.40            # optional; unused today
    disable_families: []
    thresholds:
      high: 0.65
      medium: 0.35
      low: 0.10
    retry_budget:
      default: 3

Unknown keys in the override are ignored rather than rejected so the
file stays forward-compatible.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ResilientWriteError

POLICY_FILENAME = "policy.yaml"
POLICY_FILE_ENV = "RW_POLICY_FILE"

# ---------------------------------------------------------------------------
# Shipped defaults
# ---------------------------------------------------------------------------

DEFAULT_FAMILY_WEIGHTS: dict[str, float] = {
    "api_key": 0.35,
    "github_pat": 0.35,
    "jwt": 0.25,
    "pem_block": 0.50,
    "aws_secret": 0.40,
    "pii": 0.15,
    "binary_hint": 0.20,
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "high": 0.70,
    "medium": 0.40,
    "low": 0.10,
}

DEFAULT_RETRY_BUDGET: int = 3

# (family, name, regex_source)
DEFAULT_PATTERNS: list[tuple[str, str, str]] = [
    # api_key
    ("api_key", "anthropic_oat", r"sk-ant-oat\d+-[A-Za-z0-9_\-]{40,}"),
    ("api_key", "anthropic_api", r"sk-ant-api\d+-[A-Za-z0-9_\-]{40,}"),
    ("api_key", "openai_project_key", r"sk-proj-[A-Za-z0-9_\-]{40,}"),
    # Negative lookahead keeps this regex from double-matching Anthropic
    # and OpenAI-project tokens that already have their own dedicated
    # patterns above — otherwise an `sk-ant-oat01-...` string would hit
    # both `anthropic_oat` and `openai_key`, producing noisier reports
    # (scoring is damped, so it doesn't change verdicts materially).
    ("api_key", "openai_key", r"sk-(?!ant-|proj-)[A-Za-z0-9]{30,}"),
    ("api_key", "datadog_client_key", r"pub[a-f0-9]{32}"),
    ("api_key", "statsig_client_key", r"client-[A-Za-z0-9]{30,}"),
    ("api_key", "aws_access_key_id", r"AKIA[0-9A-Z]{16}"),
    ("api_key", "bearer_token", r"(?i)bearer\s+[A-Za-z0-9\-_\.=]{20,}"),
    # github_pat
    ("github_pat", "gho", r"gho_[A-Za-z0-9]{36}"),
    ("github_pat", "ghp", r"ghp_[A-Za-z0-9]{36}"),
    ("github_pat", "ghu", r"ghu_[A-Za-z0-9]{36}"),
    ("github_pat", "ghs", r"ghs_[A-Za-z0-9]{36}"),
    ("github_pat", "ghr", r"ghr_[A-Za-z0-9]{36}"),
    # jwt
    (
        "jwt",
        "jwt_triplet",
        r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",
    ),
    # pem_block
    ("pem_block", "pem_private_key", r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
    # aws_secret (context-sensitive on the key name)
    (
        "aws_secret",
        "aws_secret_access_key",
        r"(?i)aws_secret_access_key[\"\'\s:=]+[A-Za-z0-9/+=]{40}",
    ),
    # pii (conservative)
    ("pii", "email", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    ("pii", "ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    # binary_hint
    ("binary_hint", "long_base64_blob", r"[A-Za-z0-9+/=]{200,}"),
]


# ---------------------------------------------------------------------------
# Compiled policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledPattern:
    family: str
    name: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class SizeRule:
    name: str
    key: str  # one of: total_bytes, max_line_len, line_count
    gt: int
    score: float
    suggested_action: str  # 'split' | 'escape'


DEFAULT_SIZE_RULES: tuple[SizeRule, ...] = (
    SizeRule("big_100kb", "total_bytes", 100_000, 0.15, "split"),
    SizeRule("big_500kb", "total_bytes", 500_000, 0.30, "split"),
    SizeRule("long_line", "max_line_len", 2_000, 0.20, "escape"),
    SizeRule("many_lines", "line_count", 5_000, 0.10, "split"),
)


@dataclass(frozen=True)
class Policy:
    patterns: tuple[CompiledPattern, ...]
    family_weights: dict[str, float]
    thresholds: dict[str, float]
    size_rules: tuple[SizeRule, ...]
    retry_budget: int
    disabled_families: frozenset[str] = field(default_factory=frozenset)

    def verdict(self, score: float) -> str:
        if score >= self.thresholds["high"]:
            return "high"
        if score >= self.thresholds["medium"]:
            return "medium"
        if score >= self.thresholds["low"]:
            return "low"
        return "safe"


def _compile(raw: list[tuple[str, str, str]]) -> list[CompiledPattern]:
    return [CompiledPattern(f, n, re.compile(src)) for f, n, src in raw]


def default_policy() -> Policy:
    return Policy(
        patterns=tuple(_compile(DEFAULT_PATTERNS)),
        family_weights=dict(DEFAULT_FAMILY_WEIGHTS),
        thresholds=dict(DEFAULT_THRESHOLDS),
        size_rules=DEFAULT_SIZE_RULES,
        retry_budget=DEFAULT_RETRY_BUDGET,
    )


def _merge_overrides(base: Policy, overrides: dict[str, Any]) -> Policy:
    if not isinstance(overrides, dict):
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": "policy_yaml_not_mapping"},
        )

    # Thresholds
    thresholds = dict(base.thresholds)
    for k, v in (overrides.get("thresholds") or {}).items():
        if k in thresholds and isinstance(v, (int, float)):
            thresholds[k] = float(v)

    # Retry budget
    retry_budget = base.retry_budget
    rb = overrides.get("retry_budget")
    if isinstance(rb, dict) and isinstance(rb.get("default"), int):
        retry_budget = int(rb["default"])

    # Disabled families
    disabled = frozenset(overrides.get("disable_families") or [])

    # Extend patterns
    patterns = list(base.patterns)
    extend = overrides.get("extend_patterns") or {}
    if isinstance(extend, dict):
        for family, entries in extend.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                src = entry.get("regex")
                if not name or not src:
                    continue
                try:
                    compiled = re.compile(src)
                except re.error as exc:
                    raise ResilientWriteError(
                        "policy_violation",
                        "encoding",
                        context={
                            "reason": f"bad_regex: {exc}",
                            "family": family,
                            "name": name,
                        },
                    ) from exc
                patterns.append(CompiledPattern(str(family), str(name), compiled))

    # Family weights: currently not overridable beyond defaults; entries
    # in extend_patterns that declare a weight are ignored for scoring so
    # we don't secretly amplify a workspace-injected family.
    family_weights = dict(base.family_weights)

    return Policy(
        patterns=tuple(patterns),
        family_weights=family_weights,
        thresholds=thresholds,
        size_rules=base.size_rules,
        retry_budget=retry_budget,
        disabled_families=disabled,
    )


def _resolve_policy_path(workspace: Path) -> Path:
    """Pick the policy YAML to load.

    Precedence:
    1. `$RW_POLICY_FILE` if set. Absolute paths are honoured as-is;
       relative paths resolve against the workspace root.
    2. `<workspace>/.resilient_write/policy.yaml` otherwise.
    """
    override = os.environ.get(POLICY_FILE_ENV)
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = workspace / p
        return p
    return workspace / ".resilient_write" / POLICY_FILENAME


def load_policy(workspace: Path) -> Policy:
    base = default_policy()
    override_path = _resolve_policy_path(workspace)
    if not override_path.exists():
        return base
    try:
        data = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ResilientWriteError(
            "policy_violation",
            "encoding",
            context={"reason": f"yaml_error: {exc}", "path": str(override_path)},
        ) from exc
    return _merge_overrides(base, data)
