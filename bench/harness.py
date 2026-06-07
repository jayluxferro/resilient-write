#!/usr/bin/env python3
"""Automated benchmark harness — runs the agent loop against OpenRouter.

Usage:
  python bench/harness.py --task s0_large_file_analysis --model deepseek/deepseek-v4-pro --trials 3

Requires OPENROUTER_API_KEY in environment.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tool execution — import resilient-read functions directly
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from resilient_read.reader import (
    file_stat,
    make_cursor,
    read_bytes,
    read_lines,
    read_next,
    read_tail,
    search_then_page,
)
from bench.rw_tools import execute_rw_tool, RW_TOOL_DEFS

# ---------------------------------------------------------------------------
# Model calling — OpenAI SDK pointed at OpenRouter
# ---------------------------------------------------------------------------
try:
    from openai import OpenAI
except ImportError:
    print("pip install openai", file=sys.stderr)
    sys.exit(1)

OR_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_BASE = "https://openrouter.ai/api/v1"

# ---------------------------------------------------------------------------
# Tool definitions the model sees
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rr_stat",
            "description": "Get file size and mtime. Use before chunked reads.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "include_sha256": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rr_read_bytes",
            "description": "Read a byte window from a file. Returns content, next_offset, eof.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "max_bytes": {"type": "integer", "minimum": 1, "default": 65536},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rr_make_cursor",
            "description": "Create a resumable cursor for chunked iteration.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "max_bytes": {"type": "integer", "minimum": 1, "default": 65536},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rr_read_next",
            "description": "Advance a cursor. Rejects if file changed.",
            "parameters": {
                "type": "object",
                "required": ["cursor"],
                "properties": {
                    "cursor": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rr_search_then_page",
            "description": "Search with contextual excerpts and pagination.",
            "parameters": {
                "type": "object",
                "required": ["path", "query"],
                "properties": {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "from_line": {"type": "integer", "minimum": 1, "default": 1},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
                },
            },
        },
    },
]

TOOLS.extend(RW_TOOL_DEFS)

SYSTEM_PROMPT = """You are a coding agent being benchmarked on resilient tool use.

You have access to tools for reading large files efficiently. Use them — do NOT
simulate or describe tool calls, actually call them via function calling.

Rules:
- Check file size with rr_stat before reading large files.
- For files over 100KB, use chunked reads (rr_read_bytes with offset) or cursors.
- For searching, use rr_search_then_page with pagination.
- When you have the complete answer, respond with a final message (no tool calls).

Begin!"""


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------
def execute_tool(name: str, arguments: dict[str, Any], workspace: Path) -> str:
    """Execute a resilient-read tool and return the result as a JSON string."""
    ws_list = [workspace]
    try:
        if name == "rr_stat":
            result = file_stat(ws_list, arguments["path"],
                              include_sha256=arguments.get("include_sha256", False))
        elif name == "rr_read_bytes":
            result = read_bytes(ws_list, arguments["path"],
                               offset=arguments.get("offset", 0),
                               max_bytes=arguments.get("max_bytes"),
                               encoding=arguments.get("encoding", "utf-8"),
                               errors=arguments.get("errors", "replace"))
        elif name == "rr_read_lines":
            result = read_lines(ws_list, arguments["path"],
                               start_line=arguments.get("start_line", 1),
                               max_lines=arguments.get("max_lines"))
        elif name == "rr_read_tail":
            result = read_tail(ws_list, arguments["path"],
                              max_lines=arguments.get("max_lines", 200))
        elif name == "rr_make_cursor":
            result = make_cursor(ws_list, arguments["path"],
                                offset=arguments.get("offset", 0),
                                max_bytes=arguments.get("max_bytes"))
        elif name == "rr_read_next":
            result = read_next(ws_list, arguments["cursor"],
                              max_bytes=arguments.get("max_bytes"))
        elif name == "rr_search_then_page":
            result = search_then_page(ws_list, arguments["path"],
                                     query=arguments["query"],
                                     from_line=arguments.get("from_line", 1),
                                     context_before=arguments.get("context_before", 2),
                                     context_after=arguments.get("context_after", 6),
                                     max_matches=arguments.get("max_matches", 5),
                                     case_sensitive=arguments.get("case_sensitive", False))
        elif name.startswith("rw_"):
            result_str = execute_rw_tool(name, arguments, workspace)
            try:
                result = json.loads(result_str)
            except json.JSONDecodeError:
                result = {"raw": result_str}
        else:
            result = {"ok": False, "error": f"unknown_tool: {name}"}
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "reason_hint": type(exc).__name__}

    # Strip large content fields for context efficiency
    if "content" in result and len(result.get("content", "")) > 2000:
        result["content"] = result["content"][:2000] + f"\n... [truncated, {len(result['content'])} total chars]"

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def _inject_drift(workspace: Path, tool_name: str, arguments: dict[str, Any]) -> None:
    """After a cursor is created on a file, append a line to simulate a
    concurrent write. The next rr_read_next will detect the drift."""
    if tool_name != "rr_make_cursor":
        return
    target = workspace / arguments.get("path", "")
    if target.exists():
        with open(target, "a") as f:
            f.write('{"id": 9999, "value": "injected_by_drift_simulator"}\n')


def run_trial(task_spec: str, workspace: Path, model: str,
              max_turns: int = 30, verbose: bool = True,
              inject_drift: bool = False) -> list[dict[str, Any]]:
    """Run one trial of the agent loop. Returns transcript."""
    client = OpenAI(api_key=OR_API_KEY, base_url=OR_BASE)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_spec},
    ]
    transcript: list[dict[str, Any]] = []
    cursor_created = False

    for turn in range(max_turns):
        if verbose:
            print(f"  Turn {turn + 1}...", end=" ", flush=True)

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.0,
                max_tokens=4096,
            )
        except Exception as exc:
            if verbose:
                print(f"API error: {exc}")
            transcript.append({"role": "error", "content": str(exc)})
            break

        msg = resp.choices[0].message
        if msg.content:
            transcript.append({"role": "assistant", "content": msg.content})

        if not msg.tool_calls:
            if verbose:
                print("done (final answer)")
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if verbose:
                print(f"{name} ", end="", flush=True)

            result = execute_tool(name, args, workspace)

            # Inject drift: modify file after cursor creation (S3 scenario)
            if inject_drift and name == "rr_make_cursor" and not cursor_created:
                cursor_created = True
                _inject_drift(workspace, name, args)

            transcript.append({
                "role": "tool_call",
                "name": name,
                "arguments": args,
            })
            transcript.append({
                "role": "tool_result",
                "name": name,
                "content": result,
            })

            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": name, "arguments": tc.function.arguments},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        if verbose:
            print()

    return transcript


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    from bench.runner import load_task, run_setup, run_judge, TrialResult
    from bench.scorer import score_report

    parser = argparse.ArgumentParser(prog="rab-harness", description="RAB automated harness")
    parser.add_argument("--task", required=True, help="Task directory name")
    parser.add_argument("--model", required=True, help="OpenRouter model ID")
    parser.add_argument("--trials", type=int, default=3, help="Number of trials")
    parser.add_argument("--max-turns", type=int, default=30, help="Max agent turns per trial")
    parser.add_argument("--drift", action="store_true", help="Inject cursor drift mid-trial (for S3)")
    parser.add_argument("--output", default="results.json", help="Output file for results")
    args = parser.parse_args()

    task_dir = Path("bench/tasks") / args.task
    if not task_dir.exists():
        print(f"Task not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    task = load_task(task_dir)
    results: list[TrialResult] = []

    for t in range(args.trials):
        workspace = Path(f"/tmp/rab-pilot/{args.task}/trial_{t}")
        workspace.mkdir(parents=True, exist_ok=True)
        run_setup(task, workspace)

        print(f"\n=== Trial {t + 1}/{args.trials} | {args.task} | {args.model} ===")

        t0 = time.monotonic()
        transcript = run_trial(task["spec"], workspace, args.model,
                               max_turns=args.max_turns, inject_drift=args.drift)
        duration = time.monotonic() - t0

        judge_result = run_judge(task, transcript, workspace)
        tr = TrialResult(
            task_id=args.task,
            model_id=args.model,
            trial=t,
            transcript=transcript,
            score=judge_result.get("score", 0),
            passed=judge_result.get("passed", False),
            duration_s=duration,
        )
        results.append(tr)
        print(f"  Score: {tr.score} | Passed: {tr.passed} | Duration: {duration:.1f}s")
        print(f"  Notes: {judge_result.get('notes', '')}")

    # Save
    report = score_report(results, Path(args.output))
    print(f"\nSaved to {args.output}")
    for row in report["per_model"]:
        print(f"  {row['model']}: {row['overall']} ({row[args.task]})")


if __name__ == "__main__":
    main()
