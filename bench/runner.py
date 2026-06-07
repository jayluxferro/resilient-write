"""Benchmark runner — orchestrates a single task trial.

Architecture:
  setup.py     → creates workspace state (files, secrets, etc.)
  spec.md      → the task prompt (markdown, given to the model)
  judge.py     → scores the model's transcript (0-100)
  runner.py    → orchestrates one trial end-to-end

The runner does NOT call models directly. Instead it produces a
prompt envelope that the harness feeds to a model via OR/DW/ollama.
The model's tool calls are executed via MCP, and the full transcript
is collected for scoring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# MCP tool definitions (the model sees these)
# ---------------------------------------------------------------------------

RR_TOOLS = [
    {
        "name": "rr.stat",
        "description": "Get file size, mtime, and optional SHA-256. Use before chunked reads.",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string"},
                "include_sha256": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "rr.read_bytes",
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
    {
        "name": "rr.make_cursor",
        "description": "Create a resumable cursor for chunked iteration. Detects file drift.",
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
    {
        "name": "rr.read_next",
        "description": "Advance a cursor and return the next chunk. Rejects if file changed.",
        "parameters": {
            "type": "object",
            "required": ["cursor"],
            "properties": {
                "cursor": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "rr.search_then_page",
        "description": "Search for a query in a file with contextual excerpts and pagination.",
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
]

RW_TOOLS = [
    {
        "name": "rw.risk_score",
        "description": "Pre-flight check: scan content for secrets/PII before writing. Returns verdict (safe/low/medium/high) with suggested actions.",
        "parameters": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {"type": "string"},
                "target_path": {"type": "string"},
            },
        },
    },
    {
        "name": "rw.safe_write",
        "description": "Atomic write with SHA-256 verification and audit journal. Rejects if file exists (mode=create). Returns structured error envelope on failure.",
        "parameters": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
            },
        },
    },
    {
        "name": "rw.chunk_append",
        "description": "Append a section to a chunked session. Auto-increments index.",
        "parameters": {
            "type": "object",
            "required": ["session", "content"],
            "properties": {
                "session": {"type": "string"},
                "content": {"type": "string"},
                "total_expected": {"type": "integer", "minimum": 1, "maximum": 999},
            },
        },
    },
    {
        "name": "rw.chunk_status",
        "description": "Check which chunks are present in a session. Use before compose.",
        "parameters": {
            "type": "object",
            "required": ["session"],
            "properties": {"session": {"type": "string"}},
        },
    },
    {
        "name": "rw.chunk_compose",
        "description": "Concatenate all chunks in a session and write the final file via safe_write.",
        "parameters": {
            "type": "object",
            "required": ["session", "output_path"],
            "properties": {
                "session": {"type": "string"},
                "output_path": {"type": "string"},
                "cleanup": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "rw.handoff_write",
        "description": "Save task state (envelope + body) for a future agent session. Reports drift warnings.",
        "parameters": {
            "type": "object",
            "required": ["envelope"],
            "properties": {
                "envelope": {"type": "object"},
                "body": {"type": "string", "default": ""},
            },
        },
    },
    {
        "name": "rw.handoff_read",
        "description": "Read a HANDOFF.md envelope from a prior session. Returns structured front-matter + body + drift warnings.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "HANDOFF.md"}},
        },
    },
    {
        "name": "rw.scratch_put",
        "description": "Store sensitive content out-of-band (content-addressed by SHA-256). Use for credentials/PII.",
        "parameters": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {"type": "string"},
                "label": {"type": "string"},
            },
        },
    },
    {
        "name": "rw.scratch_get",
        "description": "Retrieve scratchpad content by SHA-256 hash.",
        "parameters": {
            "type": "object",
            "required": ["sha256"],
            "properties": {"sha256": {"type": "string"}},
        },
    },
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TrialResult:
    task_id: str
    model_id: str
    trial: int
    transcript: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0
    passed: bool = False
    tool_calls: list[str] = field(default_factory=list)
    errors_encountered: list[str] = field(default_factory=list)
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Task loader
# ---------------------------------------------------------------------------

def load_task(task_dir: Path) -> dict[str, Any]:
    """Load a task's setup, spec, and judge from its directory."""
    if not task_dir.exists():
        raise FileNotFoundError(f"Task directory not found: {task_dir}")

    spec_path = task_dir / "spec.md"
    setup_path = task_dir / "setup.py"
    judge_path = task_dir / "judge.py"

    spec = spec_path.read_text() if spec_path.exists() else ""
    setup_mod = _load_module("task_setup", setup_path) if setup_path.exists() else None
    judge_mod = _load_module("task_judge", judge_path) if judge_path.exists() else None

    return {
        "id": task_dir.name,
        "spec": spec,
        "setup": setup_mod,
        "judge": judge_mod,
    }


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Build the system prompt with tool definitions."""
    tool_list = json.dumps(RR_TOOLS + RW_TOOLS, indent=2)
    return f"""You are a coding agent being benchmarked on resilient tool use.

You have access to the following MCP tools. You MUST use them to complete
the task — do NOT simulate or describe tool calls, actually call them.

Available tools:
{tool_list}

Rules:
- Read files with rr.* tools, not cat/head/tail.
- Write files with rw.* tools, not shell redirection.
- Pay attention to error envelopes. They contain `reason_hint` and
  `suggested_action` fields. Follow the suggested action.
- When a tool returns `ok: false`, do NOT retry the same call. Read the
  error and adjust.
- For large files, use cursors (rr.make_cursor → rr.read_next loop).
- For sensitive content, use rw.scratch_put instead of writing to disk.

Respond with tool calls in this format:
<tool_call>
{{"name": "rr.read_bytes", "arguments": {{"path": "file.txt", "offset": 0}}}}
</tool_call>

When the task is complete, respond with a FINAL_ANSWER block:
<final_answer>
Your answer here — the output the task requested.
</final_answer>
"""


def build_task_prompt(task: dict[str, Any]) -> str:
    """Build the user prompt for a task."""
    return f"""## Task: {task['id']}

{task['spec']}

Begin by reading the task carefully, then use the available tools to complete it.
"""


# ---------------------------------------------------------------------------
# Setup runner
# ---------------------------------------------------------------------------

def run_setup(task: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Execute the task's setup module to create initial state."""
    workspace.mkdir(parents=True, exist_ok=True)
    setup_mod = task.get("setup")
    if setup_mod and hasattr(setup_mod, "setup"):
        return setup_mod.setup(workspace)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Judge runner
# ---------------------------------------------------------------------------

def run_judge(task: dict[str, Any], transcript: list[dict[str, Any]], workspace: Path) -> dict[str, Any]:
    """Score a transcript using the task's judge module."""
    judge_mod = task.get("judge")
    if judge_mod and hasattr(judge_mod, "judge"):
        return judge_mod.judge(transcript, workspace)
    return {"score": 0, "passed": False, "breakdown": {}, "notes": "no judge configured"}


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------

def extract_final_answer(transcript: list[dict[str, Any]]) -> str | None:
    """Extract the final answer from a transcript."""
    for entry in reversed(transcript):
        content = entry.get("content", "")
        if "<final_answer>" in content:
            start = content.index("<final_answer>") + len("<final_answer>")
            end = content.index("</final_answer>") if "</final_answer>" in content else len(content)
            return content[start:end].strip()
    return None


def count_tool_calls(transcript: list[dict[str, Any]], prefix: str) -> int:
    """Count how many times a tool prefix was called."""
    count = 0
    for entry in transcript:
        if entry.get("role") == "tool_call":
            name = entry.get("name", "")
            if name.startswith(prefix):
                count += 1
    return count


def list_errors(transcript: list[dict[str, Any]]) -> list[str]:
    """Extract error messages from a transcript."""
    errors: list[str] = []
    for entry in transcript:
        if entry.get("role") == "tool_result":
            try:
                result = json.loads(entry.get("content", "{}"))
                if not result.get("ok", True):
                    errors.append(result.get("reason_hint", "unknown_error"))
            except (json.JSONDecodeError, TypeError):
                pass
    return errors


# ---------------------------------------------------------------------------
# Trial runner (call this from the harness)
# ---------------------------------------------------------------------------

def run_trial(
    task_dir: Path,
    workspace: Path,
    *,
    trial: int = 0,
) -> TrialResult:
    """Run one trial of a task.

    Returns a TrialResult with the transcript and score. The caller
    is responsible for feeding the prompt to a model and executing
    tool calls via MCP.
    """
    t0 = time.monotonic()

    task = load_task(task_dir)
    run_setup(task, workspace)

    result = TrialResult(
        task_id=task["id"],
        model_id="pending",
        trial=trial,
    )

    result.duration_s = time.monotonic() - t0
    return result


def produce_prompt(task_dir: Path, workspace: Path) -> tuple[str, str, dict[str, Any]]:
    """Produce the system + user prompts for a task trial.

    Returns (system_prompt, user_prompt, task_dict).
    Call this, feed prompts to a model, collect the transcript,
    then call score_transcript().
    """
    task = load_task(task_dir)
    run_setup(task, workspace)
    return build_system_prompt(), build_task_prompt(task), task


def score_transcript(
    task: dict[str, Any],
    transcript: list[dict[str, Any]],
    workspace: Path,
    model_id: str = "unknown",
    trial: int = 0,
) -> TrialResult:
    """Score a completed transcript."""
    judge_result = run_judge(task, transcript, workspace)
    return TrialResult(
        task_id=task["id"],
        model_id=model_id,
        trial=trial,
        transcript=transcript,
        score=judge_result.get("score", 0),
        passed=judge_result.get("passed", False),
        tool_calls=[e.get("name", "") for e in transcript if e.get("role") == "tool_call"],
        errors_encountered=list_errors(transcript),
    )
