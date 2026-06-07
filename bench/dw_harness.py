#!/usr/bin/env python3
"""DW harness — runs agent loops against DoubleWord models via MCP.

Unlike the OR harness (which uses OpenAI SDK directly), this harness
uses the dw_chat MCP tool. It sends prompts with tool definitions
embedded as text (since DW doesn't support native function calling),
parses <tool_call> blocks from responses, executes via rr/rw functions,
and loops.

Usage:
  python3 bench/dw_harness.py --task s0_large_file_analysis --model moonshotai/Kimi-K2.6 --trials 1
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from resilient_read.reader import (
    file_stat, make_cursor, read_bytes, read_lines,
    read_next, read_tail, search_then_page,
)

# Add resilient-write to path
RW_PATH = PROJECT_ROOT.parent / "resilient-write" / "src"
if str(RW_PATH) not in sys.path:
    sys.path.insert(0, str(RW_PATH))

from resilient_write.safe_write import safe_write as rw_safe_write
from resilient_write.chunks import chunk_append, chunk_compose, chunk_preview, chunk_status
from resilient_write.handoff import handoff_read, handoff_write
from resilient_write.scratchpad import scratch_get, scratch_put
from resilient_write.risk_score import score_content

from bench.runner import load_task, run_setup, run_judge, TrialResult
from bench.scorer import score_report

# ── Compact tool definitions (embedded in system prompt) ────────────

TOOL_DEFS_TEXT = """Available tools — call one at a time:

rr_stat(path) -> {size, mtime_ns}
rr_read_bytes(path, offset=0, max_bytes=65536) -> {content, next_offset, eof, bytes_read}
rr_make_cursor(path, offset=0, max_bytes=65536) -> {cursor, offset, size}
rr_read_next(cursor) -> {content, next_offset, eof} — fails if file changed (stale_precondition)
rr_search_then_page(path, query, from_line=1, max_matches=5) -> {matches, next_from_line, has_more}

rw_risk_score(content) -> {score, verdict, detected_patterns, suggested_actions}
rw_safe_write(path, content, mode="create") -> {ok, sha256} — may fail with structured error
rw_chunk_append(session, content) -> {ok, index}
rw_chunk_status(session) -> {present_indices, total_expected}
rw_chunk_compose(session, output_path) -> {ok, sha256}
rw_handoff_write(envelope, body="") -> {ok, handoff_path, drift_warnings}
rw_handoff_read() -> {envelope, body, drift_warnings}
rw_scratch_put(content, label="") -> {ok, sha256}
rw_scratch_get(sha256) -> {content}"""

SYSTEM_PROMPT = f"""You are a coding agent. Complete the task by calling tools.
Output ONE tool call per response using this EXACT format:

<tool_call>
{{"name": "rr_stat", "arguments": {{"path": "file.txt"}}}}
</tool_call>

{TOOL_DEFS_TEXT}

Rules:
- One tool call per response. No extra text when making a tool call.
- Read files with rr_* tools. Write with rw_* tools.
- For large files (>100KB), use chunked reads (rr_read_bytes with offset).
- Pay attention to errors in tool results. If a result has "ok": false, adjust.
- When the task is complete, respond WITHOUT a <tool_call> block — just your answer."""


# ── Tool executor ───────────────────────────────────────────────────

def execute_tool(name: str, args: dict, workspace: Path) -> str:
    """Execute a resilient tool and return JSON result string."""
    ws = [workspace]
    sr = workspace
    try:
        if name == "rr_stat":
            r = file_stat(ws, args["path"], include_sha256=args.get("include_sha256", False))
        elif name == "rr_read_bytes":
            r = read_bytes(ws, args["path"], offset=args.get("offset", 0),
                          max_bytes=args.get("max_bytes"),
                          encoding=args.get("encoding", "utf-8"),
                          errors=args.get("errors", "replace"))
        elif name == "rr_make_cursor":
            r = make_cursor(ws, args["path"], offset=args.get("offset", 0),
                           max_bytes=args.get("max_bytes"))
        elif name == "rr_read_next":
            r = read_next(ws, args["cursor"], max_bytes=args.get("max_bytes"))
        elif name == "rr_search_then_page":
            r = search_then_page(ws, args["path"], query=args["query"],
                                from_line=args.get("from_line", 1),
                                max_matches=args.get("max_matches", 5),
                                case_sensitive=args.get("case_sensitive", False))
        elif name == "rw_risk_score":
            r = score_content(args["content"], target_path=args.get("target_path"))
        elif name == "rw_safe_write":
            r = rw_safe_write(ws, path=args["path"], content=args["content"],
                             mode=args.get("mode", "create"))
        elif name == "rw_chunk_append":
            r = chunk_append(sr, session=args["session"], content=args["content"],
                            total_expected=args.get("total_expected"))
        elif name == "rw_chunk_status":
            r = chunk_status(sr, session=args["session"])
        elif name == "rw_chunk_compose":
            r = chunk_compose(sr, session=args["session"], output_path=args["output_path"],
                             cleanup=args.get("cleanup", False), workspaces=ws)
        elif name == "rw_handoff_write":
            r = handoff_write(ws, args["envelope"], body=args.get("body", ""))
        elif name == "rw_handoff_read":
            r = handoff_read(ws, path=args.get("path", "HANDOFF.md"))
        elif name == "rw_scratch_put":
            r = scratch_put(sr, content=args["content"], label=args.get("label"))
        elif name == "rw_scratch_get":
            r = scratch_get(sr, sha256=args["sha256"])
        else:
            r = {"ok": False, "error": f"unknown_tool: {name}", "reason_hint": "unknown"}
    except Exception as exc:
        r = {"ok": False, "error": str(exc), "reason_hint": type(exc).__name__}

    # Truncate large content
    result = json.dumps(r, separators=(",", ":"))
    if len(result) > 4000:
        d = json.loads(result)
        if "content" in d and len(d.get("content", "")) > 2000:
            d["content"] = d["content"][:2000] + f"\n... [{len(d['content'])} total chars, truncated]"
        result = json.dumps(d, separators=(",", ":"))
    return result


# ── DW caller via MCP subprocess ────────────────────────────────────

def call_dw(model: str, messages: list[dict], max_tokens: int = 1024) -> str:
    """Call DoubleWord via the MCP CLI. Returns the model's response text."""
    import subprocess

    # Build the MCP tool call as a JSON-RPC request
    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "dw_chat",
            "arguments": {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
        },
        "id": 1,
    }

    # Find the MCP server command from environment
    # We use anthropic's MCP CLI or direct stdin pipe
    # For now, use a simpler approach: call via the mcp CLI if available
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    # Try calling via the dw_chat wrapper script
    cmd = [
        sys.executable, "-c", f"""
import json, sys
# This runs inside the MCP server context — we can't actually do this
# Fall back to direct API call if key is available
print(json.dumps({{"error": "DW_MCP_DIRECT_CALL_NOT_SUPPORTED"}}))
"""
    ]

    # Actually, we need to access the MCP server directly.
    # The MCP server is already running in this session.
    # We can't call it from a subprocess.
    # Instead, write the request to a file and tell the harness to process it.

    return json.dumps({"error": "DW harness requires interactive MCP session"})


# ── Interactive runner (for use within MCP session) ─────────────────

def run_trial_interactive(
    task_dir: str, model: str, trial: int = 0,
    max_turns: int = 25, workspace_root: Path | None = None
) -> TrialResult:
    """Run one trial. Returns a TrialResult WITHOUT the transcript.
    
    The caller (MCP agent) must:
    1. Call produce_prompts() to get system + user prompts
    2. Feed them to dw_chat MCP tool
    3. Parse <tool_call> from response
    4. Call execute_tool() to run the tool
    5. Feed result back to dw_chat
    6. Repeat until final answer (no <tool_call>)
    7. Call finish_trial() to score
    """
    t0 = time.monotonic()

    task = load_task(Path(f"bench/tasks/{task_dir}"))
    ws = workspace_root or Path(f"/tmp/rab-dw/{task_dir}/trial_{trial}")
    ws.mkdir(parents=True, exist_ok=True)
    run_setup(task, ws)

    return TrialResult(
        task_id=task_dir,
        model_id=model,
        trial=trial,
        duration_s=time.monotonic() - t0,
    )


def produce_prompts(task_dir: str, workspace_root: Path | None = None) -> tuple[str, str, dict, Path]:
    """Produce (system_prompt, user_prompt, task_dict, workspace_path)."""
    task = load_task(Path(f"bench/tasks/{task_dir}"))
    ws = workspace_root or Path(f"/tmp/rab-dw/{task_dir}")
    ws.mkdir(parents=True, exist_ok=True)
    run_setup(task, ws)

    user_prompt = f"## Task: {task_dir}\n\n{task['spec']}\n\nBegin by checking what files exist and using the appropriate tools."

    return SYSTEM_PROMPT, user_prompt, task, ws


def finish_trial(
    task: dict, transcript: list[dict], workspace: Path,
    model_id: str, trial: int = 0
) -> TrialResult:
    """Score a completed transcript and return a TrialResult."""
    judge_result = run_judge(task, transcript, workspace)
    return TrialResult(
        task_id=task["id"],
        model_id=model_id,
        trial=trial,
        transcript=transcript,
        score=judge_result.get("score", 0),
        passed=judge_result.get("passed", False),
        tool_calls=[e.get("name", "") for e in transcript if e.get("role") == "tool_call"],
        errors_encountered=[
            r.get("reason_hint", "")
            for r in [json.loads(e.get("content", "{}")) for e in transcript if e.get("role") == "tool_result"]
            if isinstance(r, dict) and not r.get("ok", True)
        ],
    )


# ── XML parser ──────────────────────────────────────────────────────

def parse_tool_call(text: str) -> tuple[str | None, dict | None]:
    """Parse a <tool_call> block from model response. Returns (name, args) or (None, None)."""
    m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.DOTALL)
    if not m:
        return None, None
    try:
        inner = m.group(1).strip()
        data = json.loads(inner)
        return data.get("name"), data.get("arguments", {})
    except (json.JSONDecodeError, AttributeError):
        return None, None


def has_tool_call(text: str) -> bool:
    return "<tool_call>" in text


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(prog="rab-dw", description="DW harness helper")
    parser.add_argument("--task", required=True)
    parser.add_argument("--model", default="moonshotai/Kimi-K2.6")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--output", default="results_dw.json")
    args = parser.parse_args()

    # Just print the prompts for the MCP agent to use
    sys_prompt, user_prompt, task, ws = produce_prompts(args.task)
    task_id = task["id"]
    model_id = args.model

    print(f"=== DW Harness Ready ===")
    print(f"Task: {task_id}")
    print(f"Model: {model_id}")
    print(f"Workspace: {ws}")
    print(f"Trials: {args.trials}")
    print()
    print("--- SYSTEM PROMPT ---")
    print(sys_prompt[:200] + "...")
    print()
    print("--- USER PROMPT ---")
    print(user_prompt[:500])
    print()
    print("Instructions:")
    print("1. Call dw_chat with these prompts")
    print("2. Parse <tool_call> from response")
    print("3. Execute via bench.dw_harness.execute_tool()")
    print("4. Feed result back to dw_chat")
    print("5. Loop until no <tool_call> in response")
    print("6. Call bench.dw_harness.finish_trial() to score")
    print()
    print("Transcript template saved to transcript_template.json")

    template = {
        "task_id": task_id,
        "model_id": model_id,
        "trial": 0,
        "steps": [
            {"turn": 1, "role": "user", "content": user_prompt},
        ],
    }
    with open("transcript_template.json", "w") as f:
        json.dump(template, f, indent=2)


if __name__ == "__main__":
    main()
