#!/usr/bin/env python3
"""Batch runner — runs all tasks against all models.

Usage:
  python3 bench/batch.py --wave devbench     # 9 original DevBench models
  python3 bench/batch.py --wave additional   # extra frontier models
  python3 bench/batch.py --wave local        # gemma3, apple-fm
  python3 bench/batch.py --wave all          # everything
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Task list (skip S3 without drift for now — it needs --drift flag)
TASKS = [
    "s0_large_file_analysis",
    "s1_content_filter_recovery",
    "s2_chunked_report",
    "s3_cursor_drift",
    "s4_cross_session",
    "s5_sensitive_data",
]

TASKS_NEEDING_DRIFT = {"s3_cursor_drift"}

# ── Model registry ──────────────────────────────────────────────────
# Format: (model_id, provider, tier, display_name)

OR_MODELS = [
    # DevBench originals
    ("openai/gpt-5.5", "or", "frontier", "GPT-5.5"),
    ("anthropic/claude-opus-4.7", "or", "frontier", "Claude Opus 4.7"),
    ("deepseek/deepseek-v4-pro", "or", "frontier", "DeepSeek V4 Pro (OR)"),
    ("meta-llama/llama-4-maverick", "or", "mid_tier", "Llama 4 Maverick"),
    ("anthropic/claude-sonnet-4.6", "or", "mid_tier", "Claude Sonnet 4.6"),
    ("openai/gpt-5.4-mini", "or", "compact", "GPT-5.4 Mini"),
    ("openai/gpt-5.4-nano", "or", "compact", "GPT-5.4 Nano"),
    # Additional frontier
    ("anthropic/claude-opus-4.8", "or", "frontier", "Claude Opus 4.8"),
    ("openai/gpt-5.5-pro", "or", "frontier", "GPT-5.5 Pro"),
    ("openai/gpt-5.4", "or", "mid_tier", "GPT-5.4"),
    ("google/gemini-3.1-pro-preview", "or", "frontier", "Gemini 3.1 Pro"),
    ("deepseek/deepseek-v4-flash", "or", "mid_tier", "DeepSeek V4 Flash"),
    ("meta-llama/llama-4-scout", "or", "mid_tier", "Llama 4 Scout"),
]

DW_MODELS = [
    ("moonshotai/Kimi-K2.6", "dw", "frontier", "Kimi K2.6"),
    ("zai-org/GLM-5.1-FP8", "dw", "frontier", "GLM 5.1"),
    ("deepseek-ai/DeepSeek-V4-Pro", "dw", "frontier", "DeepSeek V4 Pro (DW)"),
    ("Qwen/Qwen3.5-397B-A17B", "dw", "mid_tier", "Qwen3.5 397B"),
]

LOCAL_MODELS = [
    # gemma3 via ollama — run separately
]

# ── Batch runner ─────────────────────────────────────────────────────

def run_one(task: str, model_id: str, provider: str, trials: int = 3,
            skip_existing: bool = True) -> dict[str, Any]:
    """Run one task-model combination."""
    extra = ["--drift"] if task in TASKS_NEEDING_DRIFT else []
    output = PROJECT_ROOT / f"results/batch_{task}_{model_id.replace('/', '-')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    if skip_existing and output.exists():
        try:
            d = json.loads(output.read_text())
            if d.get("total_trials", 0) >= trials:
                return {"task": task, "model": model_id, "provider": provider,
                        "exit_code": 0, "duration_s": 0, "output_file": str(output),
                        "stderr_tail": "", "skipped": True}
        except (json.JSONDecodeError, KeyError):
            pass

    cmd = [
        sys.executable, str(PROJECT_ROOT / "bench/harness.py"),
        "--task", task,
        "--model", model_id,
        "--trials", str(trials),
        "--max-turns", "25",
        "--output", str(output),
    ] + extra

    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT),
                           env={**__import__('os').environ, "PYTHONPATH": str(PROJECT_ROOT)},
                           capture_output=True, text=True, timeout=900)
    duration = time.monotonic() - t0

    return {
        "task": task,
        "model": model_id,
        "provider": provider,
        "exit_code": result.returncode,
        "duration_s": duration,
        "output_file": str(output),
        "stderr_tail": result.stderr[-200:] if result.stderr else "",
    }


def run_wave(models: list[tuple], tasks: list[str], trials: int, label: str,
             skip_existing: bool = True) -> list[dict]:
    """Run all task-model combinations in a wave."""
    results = []
    total = len(models) * len(tasks)
    n = 0
    print(f"\n{'='*60}")
    print(f"Wave: {label} — {len(models)} models × {len(tasks)} tasks = {total} combos")
    print(f"{'='*60}\n")

    for model_id, provider, tier, display in models:
        for task in tasks:
            n += 1
            print(f"[{n}/{total}] {display} × {task} ... ", end="", flush=True)
            r = run_one(task, model_id, provider, trials, skip_existing)
            results.append(r)
            status = "SKIP" if r.get("skipped") else ("PASS" if r["exit_code"] == 0 else f"FAIL({r['exit_code']})")
            print(f"{status} ({r['duration_s']:.0f}s)")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="rab-batch")
    parser.add_argument("--wave", choices=["devbench", "additional", "dw", "local", "all", "or-all", "dw-all"],
                        default="all")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--tasks", nargs="*", default=None,
                        help="Specific tasks (default: all 6)")
    parser.add_argument("--no-skip", action="store_true",
                        help="Re-run even if results already exist")
    args = parser.parse_args()

    tasks = args.tasks if args.tasks else TASKS
    all_results = []

    if args.wave in ("devbench", "all", "or-all"):
        devbench_or = [m for m in OR_MODELS if m[0] in {
            "openai/gpt-5.5", "anthropic/claude-opus-4.7", "deepseek/deepseek-v4-pro",
            "meta-llama/llama-4-maverick", "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4-mini", "openai/gpt-5.4-nano",
        }]
        all_results += run_wave(devbench_or, tasks, args.trials, "DevBench (OR)", not args.no_skip)

    if args.wave in ("additional", "all", "or-all"):
        additional_or = [m for m in OR_MODELS if m[0] not in {
            "openai/gpt-5.5", "anthropic/claude-opus-4.7", "deepseek/deepseek-v4-pro",
            "meta-llama/llama-4-maverick", "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4-mini", "openai/gpt-5.4-nano",
        }]
        all_results += run_wave(additional_or, tasks, args.trials, "Additional (OR)", not args.no_skip)

    if args.wave in ("dw", "all", "dw-all"):
        all_results += run_wave(DW_MODELS, tasks, args.trials, "DoubleWord", not args.no_skip)

    # Save manifest
    manifest = PROJECT_ROOT / "results/batch_manifest.json"
    manifest.write_text(json.dumps(all_results, indent=2))
    print(f"\nManifest saved to {manifest}")
    print(f"Total: {len(all_results)} runs, "
          f"{sum(1 for r in all_results if r['exit_code']==0)} passed, "
          f"{sum(1 for r in all_results if r['exit_code']!=0)} failed")


if __name__ == "__main__":
    main()
