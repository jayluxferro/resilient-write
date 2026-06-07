"""Resilient-write tool adapter for the benchmark harness.

Imports resilient-write functions directly and adapts them to the
same call interface used by the harness.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Add resilient-write to path
RW_PATH = Path(__file__).resolve().parent.parent.parent / "resilient-write" / "src"
if str(RW_PATH) not in sys.path:
    sys.path.insert(0, str(RW_PATH))

from resilient_write.safe_write import safe_write as rw_safe_write
from resilient_write.chunks import (
    chunk_append,
    chunk_compose,
    chunk_preview,
    chunk_status,
    chunk_write,
)
from resilient_write.handoff import handoff_read, handoff_write
from resilient_write.scratchpad import scratch_get, scratch_put, scratch_ref
from resilient_write.risk_score import score_content


def execute_rw_tool(name: str, arguments: dict[str, Any], workspace: Path) -> str:
    """Execute a resilient-write tool. All writes go to the first workspace."""
    ws = [workspace]
    sr = workspace  # state root = workspace for benchmark

    try:
        if name == "rw_risk_score" or name == "rw.risk_score":
            result = score_content(arguments["content"],
                                   target_path=arguments.get("target_path"))
        elif name == "rw_safe_write" or name == "rw.safe_write":
            result = rw_safe_write(ws, path=arguments["path"],
                                    content=arguments["content"],
                                    mode=arguments.get("mode", "create"))
        elif name == "rw_chunk_append" or name == "rw.chunk_append":
            result = chunk_append(sr, session=arguments["session"],
                                  content=arguments["content"],
                                  total_expected=arguments.get("total_expected"))
        elif name == "rw_chunk_status" or name == "rw.chunk_status":
            result = chunk_status(sr, session=arguments["session"])
        elif name == "rw_chunk_preview" or name == "rw.chunk_preview":
            result = chunk_preview(sr, session=arguments["session"])
        elif name == "rw_chunk_compose" or name == "rw.chunk_compose":
            result = chunk_compose(sr, session=arguments["session"],
                                    output_path=arguments["output_path"],
                                    cleanup=arguments.get("cleanup", False),
                                    workspaces=ws)
        elif name == "rw_handoff_write" or name == "rw.handoff_write":
            result = handoff_write(ws, arguments["envelope"],
                                    body=arguments.get("body", ""))
        elif name == "rw_handoff_read" or name == "rw.handoff_read":
            result = handoff_read(ws, path=arguments.get("path", "HANDOFF.md"))
        elif name == "rw_scratch_put" or name == "rw.scratch_put":
            result = scratch_put(sr, content=arguments["content"],
                                 label=arguments.get("label"))
        elif name == "rw_scratch_get" or name == "rw.scratch_get":
            result = scratch_get(sr, sha256=arguments["sha256"])
        elif name == "rw_scratch_ref" or name == "rw.scratch_ref":
            result = scratch_ref(sr, sha256=arguments.get("sha256"),
                                 label=arguments.get("label"))
        else:
            result = {"ok": False, "error": f"unknown_tool: {name}"}
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "reason_hint": type(exc).__name__}

    return json.dumps(result)


RW_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "rw_risk_score",
            "description": "Scan content for secrets/PII. Returns verdict with suggested actions.",
            "parameters": {
                "type": "object", "required": ["content"],
                "properties": {
                    "content": {"type": "string"},
                    "target_path": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_safe_write",
            "description": "Atomic write. Returns structured error on failure.",
            "parameters": {
                "type": "object", "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_chunk_append",
            "description": "Append a section to a chunked session. Auto-increments index.",
            "parameters": {
                "type": "object", "required": ["session", "content"],
                "properties": {
                    "session": {"type": "string"},
                    "content": {"type": "string"},
                    "total_expected": {"type": "integer", "minimum": 1, "maximum": 999},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_chunk_status",
            "description": "Check which chunks are present in a session.",
            "parameters": {
                "type": "object", "required": ["session"],
                "properties": {"session": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_chunk_compose",
            "description": "Concatenate all chunks and write via safe_write.",
            "parameters": {
                "type": "object", "required": ["session", "output_path"],
                "properties": {
                    "session": {"type": "string"},
                    "output_path": {"type": "string"},
                    "cleanup": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_handoff_write",
            "description": "Save task state for a future agent session.",
            "parameters": {
                "type": "object", "required": ["envelope"],
                "properties": {
                    "envelope": {"type": "object"},
                    "body": {"type": "string", "default": ""},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_handoff_read",
            "description": "Read a HANDOFF.md from a prior session.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "HANDOFF.md"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_scratch_put",
            "description": "Store sensitive content out-of-band (SHA-256 addressed).",
            "parameters": {
                "type": "object", "required": ["content"],
                "properties": {
                    "content": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rw_scratch_get",
            "description": "Retrieve scratchpad content by SHA-256 hash.",
            "parameters": {
                "type": "object", "required": ["sha256"],
                "properties": {"sha256": {"type": "string"}},
            },
        },
    },
]
