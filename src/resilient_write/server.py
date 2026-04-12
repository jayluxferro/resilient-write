"""MCP stdio entrypoint for resilient-write.

Registers the Stage-1 tool surface:

    rw.safe_write      — L1 transactional write
    rw.handoff_write   — L5 continuity envelope write
    rw.handoff_read    — L5 continuity envelope read
    rw.journal_tail    — inspection helper for the L1 journal

Each handler calls into a pure-Python layer module. Failures raise
`ResilientWriteError`; the MCP adapter catches those and returns the L3
envelope as the tool response so the calling agent can branch on
structured fields.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import chunks, handoff, journal, risk_score, safe_write, scratchpad
from .errors import ResilientWriteError

SERVER_NAME = "resilient-write"


def workspace_root() -> Path:
    """Resolve the workspace directory the server is keyed to.

    Defaults to `$PWD`. Overridable via `$RW_WORKSPACE` so clients can
    pin an explicit directory when they spawn the stdio process.
    """
    override = os.environ.get("RW_WORKSPACE")
    return Path(override).resolve() if override else Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SAFE_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "content"],
    "properties": {
        "path": {
            "type": "string",
            "description": "Workspace-relative destination path.",
        },
        "content": {
            "type": "string",
            "description": "File content to write.",
        },
        "mode": {
            "type": "string",
            "enum": ["create", "overwrite", "append"],
            "default": "create",
        },
        "expected_prev_sha256": {
            "type": "string",
            "description": (
                "Optional optimistic-concurrency guard. If set, the "
                "current file's SHA-256 must match or the write is "
                "rejected with a stale_precondition envelope."
            ),
        },
        "classify": {
            "type": "boolean",
            "default": False,
            "description": (
                "Run the L0 classifier first; reject with "
                "blocked/content_filter if the verdict meets the "
                "threshold."
            ),
        },
        "classify_reject_at": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "default": "high",
            "description": "Minimum verdict that causes a classify rejection.",
        },
    },
    "additionalProperties": False,
}

_RISK_SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["content"],
    "properties": {
        "content": {
            "type": "string",
            "description": "Draft text to classify.",
        },
        "language_hint": {
            "type": "string",
            "description": "Optional language/format hint (json, latex, ...).",
        },
        "target_path": {
            "type": "string",
            "description": "Optional destination path the content is bound for.",
        },
    },
    "additionalProperties": False,
}

_HANDOFF_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["envelope"],
    "properties": {
        "envelope": {
            "type": "object",
            "description": (
                "Handoff envelope matching docs/HANDOFF_SCHEMA.md. "
                "Required fields: task_id, status, agent, summary, "
                "next_steps, last_good_state."
            ),
        },
        "body": {
            "type": "string",
            "description": "Optional free-form Markdown body.",
        },
        "path": {
            "type": "string",
            "description": "Envelope destination (default: HANDOFF.md).",
        },
        "archive": {
            "type": "boolean",
            "default": False,
        },
    },
    "additionalProperties": False,
}

_HANDOFF_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "default": "HANDOFF.md"},
    },
    "additionalProperties": False,
}

_CHUNK_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session", "index", "content"],
    "properties": {
        "session": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_\\-]{1,64}$",
            "description": "Session name; chunks for the same session share a directory.",
        },
        "index": {
            "type": "integer",
            "minimum": 1,
            "maximum": 999,
            "description": "1-based chunk index.",
        },
        "content": {"type": "string"},
        "total_expected": {
            "type": "integer",
            "minimum": 1,
            "maximum": 999,
            "description": "Optional hint; compose will refuse to run until this many chunks exist.",
        },
    },
    "additionalProperties": False,
}

_CHUNK_COMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session", "output_path"],
    "properties": {
        "session": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_\\-]{1,64}$",
        },
        "output_path": {
            "type": "string",
            "description": "Workspace-relative final file path.",
        },
        "separator": {
            "type": "string",
            "default": "",
            "description": "Inserted between chunks during concatenation.",
        },
        "cleanup": {
            "type": "boolean",
            "default": False,
            "description": "Wipe the chunk dir after a successful compose.",
        },
    },
    "additionalProperties": False,
}

_CHUNK_RESET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session"],
    "properties": {
        "session": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_\\-]{1,64}$",
        },
    },
    "additionalProperties": False,
}

_CHUNK_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session"],
    "properties": {
        "session": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_\\-]{1,64}$",
        },
    },
    "additionalProperties": False,
}

_SCRATCH_PUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["content"],
    "properties": {
        "content": {
            "type": "string",
            "description": "Material to store. UTF-8 text by default; pass encoding=base64 for binary.",
        },
        "label": {
            "type": "string",
            "description": "Optional human-readable alias for this entry.",
        },
        "content_type": {
            "type": "string",
            "description": "Optional MIME-ish hint, e.g. application/json.",
        },
        "notes": {
            "type": "string",
            "description": "Optional free-form notes stored verbatim in the index.",
        },
        "encoding": {
            "type": "string",
            "enum": ["utf-8", "base64"],
            "default": "utf-8",
        },
    },
    "additionalProperties": False,
}

_SCRATCH_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sha256": {"type": "string"},
        "label": {"type": "string"},
    },
    "additionalProperties": False,
}

_SCRATCH_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sha256"],
    "properties": {
        "sha256": {"type": "string"},
        "encoding": {
            "type": "string",
            "enum": ["utf-8", "base64"],
            "default": "utf-8",
        },
    },
    "additionalProperties": False,
}

_JOURNAL_TAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1, "default": 20},
        "filter_path": {"type": "string"},
        "filter_mode": {
            "type": "string",
            "enum": ["create", "overwrite", "append"],
        },
    },
    "additionalProperties": False,
}


_TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="rw.risk_score",
        description=(
            "L0 — deterministic pre-flight classifier. Runs regex + size "
            "heuristics over draft content and returns a structured "
            "verdict (safe/low/medium/high) plus detected patterns and "
            "suggested actions. No LLM, no network."
        ),
        inputSchema=_RISK_SCORE_SCHEMA,
    ),
    Tool(
        name="rw.safe_write",
        description=(
            "L1 — transactional write. Writes content to a temp file, "
            "fsyncs, re-reads and verifies SHA-256, then atomically "
            "renames over the destination. Appends one row to the "
            ".resilient_write/journal.jsonl audit log. Returns a typed "
            "error envelope on any failure."
        ),
        inputSchema=_SAFE_WRITE_SCHEMA,
    ),
    Tool(
        name="rw.chunk_write",
        description=(
            "L2 — write one chunk of a compose session. Chunks land "
            "under .resilient_write/chunks/<session>/part-NNN.txt via "
            "safe_write (mode=overwrite), so retrying a failing chunk "
            "is idempotent. Each chunk gets its own journal row."
        ),
        inputSchema=_CHUNK_WRITE_SCHEMA,
    ),
    Tool(
        name="rw.chunk_compose",
        description=(
            "L2 — concatenate a session's chunk files (part-001..N) in "
            "index order and write the result to output_path through "
            "safe_write. Verifies contiguity and total_expected from "
            "the session manifest. Optional cleanup wipes the session "
            "directory on success."
        ),
        inputSchema=_CHUNK_COMPOSE_SCHEMA,
    ),
    Tool(
        name="rw.chunk_reset",
        description=(
            "L2 — destructively wipe an in-progress chunk session. "
            "Returns the number of removed chunk files."
        ),
        inputSchema=_CHUNK_RESET_SCHEMA,
    ),
    Tool(
        name="rw.chunk_status",
        description=(
            "Inspection helper — report which chunk indices are "
            "currently present for a session and what total was "
            "declared by the most recent chunk_write call."
        ),
        inputSchema=_CHUNK_STATUS_SCHEMA,
    ),
    Tool(
        name="rw.scratch_put",
        description=(
            "L4 — store raw material out-of-band under "
            ".resilient_write/scratch/<sha256>.bin and append a row to "
            "index.jsonl. Content-addressed: identical bytes dedupe "
            "automatically. Accepts utf-8 or base64-encoded input."
        ),
        inputSchema=_SCRATCH_PUT_SCHEMA,
    ),
    Tool(
        name="rw.scratch_ref",
        description=(
            "L4 — look up a scratchpad index entry by sha256 or label "
            "without returning the content itself. Useful to verify "
            "what's there before deciding whether to surface it."
        ),
        inputSchema=_SCRATCH_REF_SCHEMA,
    ),
    Tool(
        name="rw.scratch_get",
        description=(
            "L4 — return raw content by hash. Gated by the "
            "$RW_SCRATCH_DISABLE_GET environment variable: when that "
            "is set, every call returns a policy_violation envelope so "
            "the workspace can run in write-only mode."
        ),
        inputSchema=_SCRATCH_GET_SCHEMA,
    ),
    Tool(
        name="rw.handoff_write",
        description=(
            "L5 — write a HANDOFF.md continuity envelope (YAML front-"
            "matter + Markdown body). Validates required fields and "
            "reports drift warnings for any last_good_state file whose "
            "current hash disagrees with the recorded one."
        ),
        inputSchema=_HANDOFF_WRITE_SCHEMA,
    ),
    Tool(
        name="rw.handoff_read",
        description=(
            "L5 — parse a HANDOFF.md envelope and return the structured "
            "front-matter plus body. Reports drift warnings."
        ),
        inputSchema=_HANDOFF_READ_SCHEMA,
    ),
    Tool(
        name="rw.journal_tail",
        description=(
            "Inspection helper — return the last N rows of the L1 write "
            "journal, optionally filtered by path or mode."
        ),
        inputSchema=_JOURNAL_TAIL_SCHEMA,
    ),
]


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------


def _dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the layer function for a tool name. Pure, synchronous, and
    testable without an MCP event loop."""
    workspace = workspace_root()
    if name == "rw.risk_score":
        return risk_score.score_for_workspace(
            workspace,
            arguments["content"],
            language_hint=arguments.get("language_hint"),
            target_path=arguments.get("target_path"),
        )
    if name == "rw.safe_write":
        return safe_write.safe_write(
            workspace,
            path=arguments["path"],
            content=arguments["content"],
            mode=arguments.get("mode", "create"),
            expected_prev_sha256=arguments.get("expected_prev_sha256"),
            classify=bool(arguments.get("classify", False)),
            classify_reject_at=arguments.get("classify_reject_at", "high"),
            caller=SERVER_NAME,
        )
    if name == "rw.chunk_write":
        return chunks.chunk_write(
            workspace,
            session=arguments["session"],
            index=int(arguments["index"]),
            content=arguments["content"],
            total_expected=arguments.get("total_expected"),
            caller=SERVER_NAME,
        )
    if name == "rw.chunk_compose":
        return chunks.chunk_compose(
            workspace,
            session=arguments["session"],
            output_path=arguments["output_path"],
            separator=arguments.get("separator", ""),
            cleanup=bool(arguments.get("cleanup", False)),
            caller=SERVER_NAME,
        )
    if name == "rw.chunk_reset":
        return chunks.chunk_reset(workspace, session=arguments["session"])
    if name == "rw.chunk_status":
        return chunks.chunk_status(workspace, session=arguments["session"])
    if name == "rw.scratch_put":
        return scratchpad.scratch_put(
            workspace,
            content=arguments["content"],
            label=arguments.get("label"),
            content_type=arguments.get("content_type"),
            notes=arguments.get("notes"),
            encoding=arguments.get("encoding", "utf-8"),
            caller=SERVER_NAME,
        )
    if name == "rw.scratch_ref":
        return scratchpad.scratch_ref(
            workspace,
            sha256=arguments.get("sha256"),
            label=arguments.get("label"),
        )
    if name == "rw.scratch_get":
        return scratchpad.scratch_get(
            workspace,
            sha256=arguments["sha256"],
            encoding=arguments.get("encoding", "utf-8"),
        )
    if name == "rw.handoff_write":
        return handoff.handoff_write(
            workspace,
            arguments["envelope"],
            body=arguments.get("body", ""),
            path=arguments.get("path", handoff.DEFAULT_HANDOFF_FILENAME),
            archive=bool(arguments.get("archive", False)),
            caller=SERVER_NAME,
        )
    if name == "rw.handoff_read":
        return handoff.handoff_read(
            workspace,
            path=arguments.get("path", handoff.DEFAULT_HANDOFF_FILENAME),
        )
    if name == "rw.journal_tail":
        entries = journal.tail(
            workspace,
            n=int(arguments.get("n", 20)),
            filter_path=arguments.get("filter_path"),
            filter_mode=arguments.get("filter_mode"),
        )
        return {"ok": True, "entries": entries}
    raise ResilientWriteError(
        "policy_violation",
        "unknown",
        context={"unknown_tool": name},
    )


def _envelope_or_error(
    name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    try:
        return _dispatch(name, arguments)
    except ResilientWriteError as exc:
        env = exc.to_envelope()
        env.setdefault("context", {}).setdefault("tool", name)
        return env


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def build_server() -> Server:
    """Construct the MCP server instance with all Stage-1 tools wired."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return list(_TOOL_DEFINITIONS)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        result = _envelope_or_error(name, arguments or {})
        return [
            TextContent(
                type="text",
                text=json.dumps(result, separators=(",", ":"), sort_keys=True),
            )
        ]

    return server


async def _run() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Console-script entry point."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
