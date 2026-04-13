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

from . import analytics, chunks, handoff, journal, risk_score, safe_write, scratchpad, validate
from .errors import ResilientWriteError

SERVER_NAME = "resilient-write"


_UNSAFE_ROOTS = frozenset({"/", "/bin", "/sbin", "/usr", "/etc", "/var", "/tmp"})


def workspace_root() -> Path:
    """Resolve the workspace directory the server is keyed to.

    Defaults to `$PWD`. Overridable via `$RW_WORKSPACE` so clients can
    pin an explicit directory when they spawn the stdio process.

    Raises ``SystemExit`` if the resolved path is a system directory
    (``/``, ``/usr``, …) — writing state files there is never intended
    and almost certainly means ``$RW_WORKSPACE`` was not expanded by the
    MCP client.
    """
    override = os.environ.get("RW_WORKSPACE")
    root = Path(override).resolve() if override else Path.cwd().resolve()
    if str(root) in _UNSAFE_ROOTS:
        import sys

        print(
            f"resilient-write: refusing to use '{root}' as workspace root. "
            "Set $RW_WORKSPACE to your project directory.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return root


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

_CHUNK_APPEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session", "content"],
    "properties": {
        "session": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_\\-]{1,64}$",
            "description": "Session name; chunks for the same session share a directory.",
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

_VALIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["content"],
    "properties": {
        "content": {
            "type": "string",
            "description": "Content to validate.",
        },
        "format_hint": {
            "type": "string",
            "enum": ["latex", "json", "python", "yaml"],
            "description": "Format to validate as. Auto-detected if omitted.",
        },
        "target_path": {
            "type": "string",
            "description": "Optional path hint for auto-detecting format from extension.",
        },
    },
    "additionalProperties": False,
}

_ANALYTICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "since": {
            "type": "string",
            "description": "ISO timestamp; only include journal entries after this time.",
        },
        "session_filter": {
            "type": "string",
            "description": "Only include chunk sessions matching this name.",
        },
    },
    "additionalProperties": False,
}

_CHUNK_PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session"],
    "properties": {
        "session": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_\\-]{1,64}$",
        },
        "separator": {
            "type": "string",
            "default": "",
            "description": "Inserted between chunks during concatenation.",
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
            "Use before any file write to check for content that may "
            "trigger safety filters. Runs deterministic regex + size "
            "heuristics and returns a verdict (safe/low/medium/high) "
            "with detected patterns and suggested actions. No LLM, "
            "no network, <50ms."
        ),
        inputSchema=_RISK_SCORE_SCHEMA,
    ),
    Tool(
        name="rw.safe_write",
        description=(
            "Use instead of raw Write/edit_file for all file creation "
            "and overwrites. Writes atomically (temp file → fsync → "
            "SHA-256 verify → rename), appends to an audit journal, "
            "and returns structured error envelopes on failure so you "
            "can branch on the reason rather than retrying blindly."
        ),
        inputSchema=_SAFE_WRITE_SCHEMA,
    ),
    Tool(
        name="rw.chunk_write",
        description=(
            "Use for large files: write one numbered chunk to a session "
            "directory via safe_write. Retrying a chunk is idempotent. "
            "Each chunk gets its own journal row. Compose all chunks "
            "into the final file with rw.chunk_compose."
        ),
        inputSchema=_CHUNK_WRITE_SCHEMA,
    ),
    Tool(
        name="rw.chunk_compose",
        description=(
            "Use after all chunks are written to assemble the final "
            "file. Concatenates part-001..N in order, verifies "
            "contiguity and total_expected, then writes through "
            "safe_write. Optional cleanup wipes the session directory."
        ),
        inputSchema=_CHUNK_COMPOSE_SCHEMA,
    ),
    Tool(
        name="rw.chunk_append",
        description=(
            "Use for building files section by section — auto-detects "
            "the highest chunk index and writes index+1. No need to "
            "track numbers. If a crash occurs between calls, only the "
            "current section is lost; prior chunks are already on disk."
        ),
        inputSchema=_CHUNK_APPEND_SCHEMA,
    ),
    Tool(
        name="rw.chunk_reset",
        description=(
            "Use to discard an abandoned or stale chunk session. "
            "Destructively wipes all chunk files and returns the count "
            "of removed files."
        ),
        inputSchema=_CHUNK_RESET_SCHEMA,
    ),
    Tool(
        name="rw.chunk_status",
        description=(
            "Use to inspect a chunk session before compose — reports "
            "which indices are present and what total_expected was "
            "declared. Helps decide which chunk to retry."
        ),
        inputSchema=_CHUNK_STATUS_SCHEMA,
    ),
    Tool(
        name="rw.scratch_put",
        description=(
            "Use to store sensitive material (credentials, PII, binary "
            "blobs) out-of-band instead of writing it to the workspace "
            "tree. Content-addressed by SHA-256; identical payloads "
            "deduplicate automatically."
        ),
        inputSchema=_SCRATCH_PUT_SCHEMA,
    ),
    Tool(
        name="rw.scratch_ref",
        description=(
            "Use to check what is in the scratchpad without retrieving "
            "the content. Looks up metadata by sha256 or label."
        ),
        inputSchema=_SCRATCH_REF_SCHEMA,
    ),
    Tool(
        name="rw.scratch_get",
        description=(
            "Use to retrieve scratchpad content by hash. Gated by "
            "$RW_SCRATCH_DISABLE_GET — when set, returns a "
            "policy_violation envelope (write-only mode)."
        ),
        inputSchema=_SCRATCH_GET_SCHEMA,
    ),
    Tool(
        name="rw.handoff_write",
        description=(
            "Use at end of session or when blocked to save task state "
            "for the next agent. Writes a HANDOFF.md envelope with "
            "task_id, status, next_steps, and last_good_state hashes. "
            "Reports drift warnings for files that changed since last "
            "recorded state."
        ),
        inputSchema=_HANDOFF_WRITE_SCHEMA,
    ),
    Tool(
        name="rw.handoff_read",
        description=(
            "Use at start of session to resume a prior task. Parses "
            "HANDOFF.md and returns the structured envelope plus drift "
            "warnings for any files whose hashes have changed."
        ),
        inputSchema=_HANDOFF_READ_SCHEMA,
    ),
    Tool(
        name="rw.journal_tail",
        description=(
            "Use to inspect recent write history — returns the last N "
            "journal rows, optionally filtered by path or mode."
        ),
        inputSchema=_JOURNAL_TAIL_SCHEMA,
    ),
    Tool(
        name="rw.validate",
        description=(
            "Use before writing to catch syntax errors. Checks LaTeX "
            "(braces, environments), JSON, Python, and YAML. Returns "
            "a diagnostic envelope with line numbers. Pair with "
            "rw.chunk_preview to validate before rw.chunk_compose."
        ),
        inputSchema=_VALIDATE_SCHEMA,
    ),
    Tool(
        name="rw.analytics",
        description=(
            "Use to understand write patterns — analyzes the journal "
            "to report write counts, timing, hot paths, chunk-session "
            "summaries, and write velocity."
        ),
        inputSchema=_ANALYTICS_SCHEMA,
    ),
    Tool(
        name="rw.chunk_preview",
        description=(
            "Use before rw.chunk_compose to preview the result. "
            "Returns concatenated content without writing to disk. "
            "Performs all contiguity and total_expected checks. Run "
            "rw.validate on the result to catch errors pre-commit."
        ),
        inputSchema=_CHUNK_PREVIEW_SCHEMA,
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
    if name == "rw.chunk_append":
        return chunks.chunk_append(
            workspace,
            session=arguments["session"],
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
    if name == "rw.validate":
        return validate.validate_content(
            arguments["content"],
            format_hint=arguments.get("format_hint"),
            target_path=arguments.get("target_path"),
        )
    if name == "rw.analytics":
        return analytics.analyze_journal(
            workspace,
            since=arguments.get("since"),
            session_filter=arguments.get("session_filter"),
        )
    if name == "rw.chunk_preview":
        return chunks.chunk_preview(
            workspace,
            session=arguments["session"],
            separator=arguments.get("separator", ""),
        )
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


_SERVER_INSTRUCTIONS = (
    "This workspace has the resilient-write MCP server active. "
    "Prefer rw.safe_write over raw Write/edit_file for all file creation "
    "and overwrites — it provides atomic writes, hash verification, "
    "structured error envelopes, and an audit journal. "
    "For files larger than ~5KB, use rw.chunk_append to build the file "
    "section by section, then rw.chunk_compose to assemble it. "
    "Before writing content that may contain tokens or credentials, "
    "call rw.risk_score first and redact any flagged patterns. "
    "At the end of a session or when blocked, call rw.handoff_write "
    "to save task state for the next agent."
)


def build_server() -> Server:
    """Construct the MCP server instance with all tools wired."""
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
    init_options = server.create_initialization_options()
    init_options.instructions = _SERVER_INSTRUCTIONS
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    """Console-script entry point."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
