"""Pass@1 scorer for the Resilient Agent Benchmark.

Produces a table matching DevBench's Table 4 format:
  - Per-model Pass@1 (overall)
  - Per-category breakdown (6 task scenarios)
  - Tier comparisons (frontier vs mid-tier vs compact vs local)
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .runner import TrialResult

# Model tiers (matching DevBench's categorization)
FRONTIER = {
    "gpt-5.5", "gpt-5.5-pro",
    "claude-opus-4.7", "claude-opus-4.8",
    "deepseek-v4-pro", "deepseek-v4-pro-dw",
    "gemini-3.1-pro-preview",
    "kimi-k2.6", "glm-5.1",
}

MID_TIER = {
    "llama-4-maverick", "llama-4-scout",
    "mistral-medium-3.5",
    "claude-sonnet-4.6",
    "gpt-5.4", "gpt-5.4-mini",
    "deepseek-v4-flash",
    "qwen3.5-397b",
}

COMPACT = {
    "gpt-5.4-nano",
    "qwen3.6-27b",
}

LOCAL = {
    "gemma3",
    "apple-fm",
}

PASS_THRESHOLD = 70  # score >= 70 is a pass


def compute_pass_at_1(results: list[TrialResult], n: int = 5) -> float:
    """Pass@1: fraction of trials where score >= threshold.

    Uses the unbiased estimator from Chen et al. (2021):
    pass@k = 1 - C(n-c, k) / C(n, k)  where c = count of correct samples.

    For k=1: pass@1 = c / n  (simplifies to straightforward fraction).
    """
    if not results:
        return 0.0
    passed = sum(1 for r in results if r.passed)
    return passed / len(results)


def per_task_table(results: list[TrialResult]) -> list[dict[str, Any]]:
    """Build a per-task Pass@1 table (like DevBench's Table 4)."""
    # Group by model, then by task
    by_model: dict[str, dict[str, list[TrialResult]]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        by_model[r.model_id][r.task_id].append(r)

    task_ids = sorted({r.task_id for r in results})
    rows = []
    for model_id in sorted(by_model):
        model_results = by_model[model_id]
        row: dict[str, Any] = {"model": model_id, "tier": _model_tier(model_id)}
        scores = []
        for tid in task_ids:
            trials = model_results.get(tid, [])
            p = compute_pass_at_1(trials)
            row[tid] = f"{p:.1%}"
            scores.extend(trials)
        row["overall"] = f"{compute_pass_at_1(scores):.1%}"
        rows.append(row)
    return rows


def tier_summary(results: list[TrialResult]) -> dict[str, Any]:
    """Aggregate Pass@1 by model tier."""
    by_tier: dict[str, list[TrialResult]] = defaultdict(list)
    for r in results:
        by_tier[_model_tier(r.model_id)].append(r)

    summary = {}
    for tier, tier_results in sorted(by_tier.items()):
        summary[tier] = {
            "models": sorted({r.model_id for r in tier_results}),
            "trials": len(tier_results),
            "pass_at_1": f"{compute_pass_at_1(tier_results):.1%}",
        }
    return summary


def score_report(results: list[TrialResult], output_path: Path | None = None) -> dict[str, Any]:
    """Full scoring report."""
    report = {
        "total_trials": len(results),
        "unique_models": len({r.model_id for r in results}),
        "unique_tasks": len({r.task_id for r in results}),
        "pass_threshold": PASS_THRESHOLD,
        "per_model": per_task_table(results),
        "by_tier": tier_summary(results),
    }
    if output_path:
        output_path.write_text(json.dumps(report, indent=2))
    return report


def _model_tier(model_id: str) -> str:
    if model_id in FRONTIER:
        return "frontier"
    if model_id in MID_TIER:
        return "mid_tier"
    if model_id in COMPACT:
        return "compact"
    if model_id in LOCAL:
        return "local"
    return "unknown"
