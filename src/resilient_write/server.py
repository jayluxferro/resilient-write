"""MCP entrypoint for resilient-write.

Supports three transports: stdio (default), sse, http.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import analytics, checkpoint, chunks, handoff, journal, risk_score, safe_write, scratchpad, validate
from .errors import ResilientWriteError
from .paths import state_root, workspace_roots

SERVER_NAME = "resilient-write"

_SAFE_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["path", "content"],
    "properties": {
        "path": {"type": "string"}, "content": {"type": "string"},
        "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
        "expected_prev_sha256": {"type": "string"},
        "classify": {"type": "boolean", "default": False},
        "classify_reject_at": {"type": "string", "enum": ["low", "medium", "high"], "default": "high"},
    }, "additionalProperties": False,
}

_RISK_SCORE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["content"],
    "properties": {"content": {"type": "string"}, "language_hint": {"type": "string"}, "target_path": {"type": "string"}},
    "additionalProperties": False,
}

_CHUNK_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["session", "index", "content"],
    "properties": {
        "session": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"},
        "index": {"type": "integer", "minimum": 1, "maximum": 999},
        "content": {"type": "string"},
        "total_expected": {"type": "integer", "minimum": 1, "maximum": 999},
    }, "additionalProperties": False,
}

_CHUNK_APPEND_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["session", "content"],
    "properties": {
        "session": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"},
        "content": {"type": "string"},
        "total_expected": {"type": "integer", "minimum": 1, "maximum": 999},
    }, "additionalProperties": False,
}

_CHUNK_COMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["session", "output_path"],
    "properties": {
        "session": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"},
        "output_path": {"type": "string"},
        "separator": {"type": "string", "default": ""},
        "cleanup": {"type": "boolean", "default": False},
    }, "additionalProperties": False,
}

_CHUNK_RESET_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["session"],
    "properties": {"session": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"}},
    "additionalProperties": False,
}

_CHUNK_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["session"],
    "properties": {"session": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"}},
    "additionalProperties": False,
}

_CHUNK_PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["session"],
    "properties": {
        "session": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"},
        "separator": {"type": "string", "default": ""},
    }, "additionalProperties": False,
}

_SCRATCH_PUT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["content"],
    "properties": {
        "content": {"type": "string"}, "label": {"type": "string"},
        "content_type": {"type": "string"}, "notes": {"type": "string"},
        "encoding": {"type": "string", "enum": ["utf-8", "base64"], "default": "utf-8"},
    }, "additionalProperties": False,
}

_SCRATCH_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"sha256": {"type": "string"}, "label": {"type": "string"}},
    "additionalProperties": False,
}

_SCRATCH_GET_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["sha256"],
    "properties": {
        "sha256": {"type": "string"},
        "encoding": {"type": "string", "enum": ["utf-8", "base64"], "default": "utf-8"},
    }, "additionalProperties": False,
}

_HANDOFF_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["envelope"],
    "properties": {
        "envelope": {"type": "object"}, "body": {"type": "string", "default": ""},
        "path": {"type": "string", "default": "HANDOFF.md"},
        "archive": {"type": "boolean", "default": False},
    }, "additionalProperties": False,
}

_HANDOFF_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "default": "HANDOFF.md"}},
    "additionalProperties": False,
}

_JOURNAL_TAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1, "default": 20},
        "filter_path": {"type": "string"},
        "filter_mode": {"type": "string", "enum": ["create", "overwrite", "append"]},
    }, "additionalProperties": False,
}

_VALIDATE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["content"],
    "properties": {
        "content": {"type": "string"},
        "format_hint": {"type": "string", "enum": ["latex", "json", "python", "yaml"]},
        "target_path": {"type": "string"},
    }, "additionalProperties": False,
}

_ANALYTICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"since": {"type": "string"}, "session_filter": {"type": "string"}},
    "additionalProperties": False,
}

_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["name", "data"],
    "properties": {
        "name": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"},
        "data": {},
        "format": {"type": "string", "enum": ["json", "yaml", "markdown"], "default": "json"},
        "ttl": {"type": "string", "default": "session"},
    }, "additionalProperties": False,
}

_CHECKPOINT_READ_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["name"],
    "properties": {"name": {"type": "string", "pattern": "^[A-Za-z0-9_\\-]{1,64}$"}},
    "additionalProperties": False,
}

_CHECKPOINT_LIST_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {}, "additionalProperties": False,
}

_CHECKPOINT_CLEANUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"include_session": {"type": "boolean", "default": True}},
    "additionalProperties": False,
}

_TOOL_DEFINITIONS: list[Tool] = [
    Tool(name="rw.risk_score", description="Use before any file write to check for content that may trigger safety filters. Runs deterministic regex + size heuristics and returns a verdict (safe/low/medium/high) with detected patterns and suggested actions. No LLM, no network, <50ms.", inputSchema=_RISK_SCORE_SCHEMA),
    Tool(name="rw.safe_write", description="Use instead of raw Write/edit_file for all file creation and overwrites. Writes atomically (temp file → fsync → SHA-256 verify → rename), appends to an audit journal, and returns structured error envelopes on failure so you can branch on the reason rather than retrying blindly.", inputSchema=_SAFE_WRITE_SCHEMA),
    Tool(name="rw.chunk_write", description="Use for large files: write one numbered chunk to a session directory via safe_write. Retrying a chunk is idempotent. Each chunk gets its own journal row. Compose all chunks into the final file with rw.chunk_compose.", inputSchema=_CHUNK_WRITE_SCHEMA),
    Tool(name="rw.chunk_append", description="Use for building files section by section — auto-detects the highest chunk index and writes index+1. No need to track numbers. If a crash occurs between calls, only the current section is lost; prior chunks are already on disk.", inputSchema=_CHUNK_APPEND_SCHEMA),
    Tool(name="rw.chunk_compose", description="Use after all chunks are written to assemble the final file. Concatenates part-001..N in order, verifies contiguity and total_expected, then writes through safe_write. Optional cleanup wipes the session directory.", inputSchema=_CHUNK_COMPOSE_SCHEMA),
    Tool(name="rw.chunk_reset", description="Use to discard an abandoned or stale chunk session. Destructively wipes all chunk files and returns the count of removed files.", inputSchema=_CHUNK_RESET_SCHEMA),
    Tool(name="rw.chunk_status", description="Use to inspect a chunk session before compose — reports which indices are present and what total_expected was declared. Helps decide which chunk to retry.", inputSchema=_CHUNK_STATUS_SCHEMA),
    Tool(name="rw.chunk_preview", description="Use before rw.chunk_compose to preview the result. Returns concatenated content without writing to disk. Performs all contiguity and total_expected checks. Run rw.validate on the result to catch errors pre-commit.", inputSchema=_CHUNK_PREVIEW_SCHEMA),
    Tool(name="rw.scratch_put", description="Use to store sensitive material (credentials, PII, binary blobs) out-of-band instead of writing it to the workspace tree. Content-addressed by SHA-256; identical payloads deduplicate automatically.", inputSchema=_SCRATCH_PUT_SCHEMA),
    Tool(name="rw.scratch_ref", description="Use to check what is in the scratchpad without retrieving the content. Looks up metadata by sha256 or label.", inputSchema=_SCRATCH_REF_SCHEMA),
    Tool(name="rw.scratch_get", description="Use to retrieve scratchpad content by hash. Gated by $RW_SCRATCH_DISABLE_GET — when set, returns a policy_violation envelope (write-only mode).", inputSchema=_SCRATCH_GET_SCHEMA),
    Tool(name="rw.handoff_write", description="Use at end of session or when blocked to save task state for the next agent. Writes a HANDOFF.md envelope with task_id, status, next_steps, and last_good_state hashes. Reports drift warnings for files that changed since last recorded state.", inputSchema=_HANDOFF_WRITE_SCHEMA),
    Tool(name="rw.handoff_read", description="Use at start of session to resume a prior task. Parses HANDOFF.md and returns the structured envelope plus drift warnings for any files whose hashes have changed.", inputSchema=_HANDOFF_READ_SCHEMA),
    Tool(name="rw.journal_tail", description="Use to inspect recent write history — returns the last N journal rows, optionally filtered by path or mode.", inputSchema=_JOURNAL_TAIL_SCHEMA),
    Tool(name="rw.validate", description="Use before writing to catch syntax errors. Checks LaTeX (braces, environments), JSON, Python, and YAML. Returns a diagnostic envelope with line numbers. Pair with rw.chunk_preview to validate before rw.chunk_compose.", inputSchema=_VALIDATE_SCHEMA),
    Tool(name="rw.analytics", description="Use to understand write patterns — analyzes the journal to report write counts, timing, hot paths, chunk-session summaries, and write velocity.", inputSchema=_ANALYTICS_SCHEMA),
    Tool(name="rw.checkpoint", description="Use to save a named snapshot of accumulated intermediate data to disk mid-session. Offloads context-heavy data (e.g. parallel agent results) so context compaction doesn't lose fidelity. Overwrites existing checkpoints with the same name. Written atomically via safe_write.", inputSchema=_CHECKPOINT_SCHEMA),
    Tool(name="rw.checkpoint_read", description="Use to retrieve a previously saved checkpoint by name. Cheaper than re-reading agent outputs or re-running sub-tasks. Returns the full data payload with metadata.", inputSchema=_CHECKPOINT_READ_SCHEMA),
    Tool(name="rw.checkpoint_list", description="Use to list all available checkpoints with their sizes, formats, and timestamps — without loading data payloads. Useful for deciding what to read or clean up.", inputSchema=_CHECKPOINT_LIST_SCHEMA),
    Tool(name="rw.checkpoint_cleanup", description="Use to remove expired checkpoints based on TTL. Removes session-scoped checkpoints by default, checks ISO duration TTLs against current time, and always keeps permanent checkpoints. Returns a list of removed entries.", inputSchema=_CHECKPOINT_CLEANUP_SCHEMA),
]


def _dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    sr = state_root()
    ws = workspace_roots()

    if name == "rw.risk_score":
        return risk_score.score_for_workspace(
            sr, arguments["content"],
            language_hint=arguments.get("language_hint"),
            target_path=arguments.get("target_path"),
        )
    if name == "rw.safe_write":
        return safe_write.safe_write(
            ws, path=arguments["path"], content=arguments["content"],
            mode=arguments.get("mode", "create"),
            expected_prev_sha256=arguments.get("expected_prev_sha256"),
            classify=bool(arguments.get("classify", False)),
            classify_reject_at=arguments.get("classify_reject_at", "high"),
            caller=SERVER_NAME, state_root=sr,
        )
    if name == "rw.chunk_write":
        return chunks.chunk_write(
            sr, session=arguments["session"], index=int(arguments["index"]),
            content=arguments["content"], total_expected=arguments.get("total_expected"),
            caller=SERVER_NAME,
        )
    if name == "rw.chunk_append":
        return chunks.chunk_append(
            sr, session=arguments["session"], content=arguments["content"],
            total_expected=arguments.get("total_expected"), caller=SERVER_NAME,
        )
    if name == "rw.chunk_compose":
        return chunks.chunk_compose(
            sr, session=arguments["session"], output_path=arguments["output_path"],
            separator=arguments.get("separator", ""),
            cleanup=bool(arguments.get("cleanup", False)),
            caller=SERVER_NAME, workspaces=ws,
        )
    if name == "rw.chunk_reset":
        return chunks.chunk_reset(sr, session=arguments["session"])
    if name == "rw.chunk_status":
        return chunks.chunk_status(sr, session=arguments["session"])
    if name == "rw.scratch_put":
        return scratchpad.scratch_put(
            sr, content=arguments["content"], label=arguments.get("label"),
            content_type=arguments.get("content_type"), notes=arguments.get("notes"),
            encoding=arguments.get("encoding", "utf-8"), caller=SERVER_NAME,
        )
    if name == "rw.scratch_ref":
        return scratchpad.scratch_ref(sr, sha256=arguments.get("sha256"), label=arguments.get("label"))
    if name == "rw.scratch_get":
        return scratchpad.scratch_get(sr, sha256=arguments["sha256"], encoding=arguments.get("encoding", "utf-8"))
    if name == "rw.handoff_write":
        return handoff.handoff_write(
            ws, arguments["envelope"], body=arguments.get("body", ""),
            path=arguments.get("path", handoff.DEFAULT_HANDOFF_FILENAME),
            archive=bool(arguments.get("archive", False)), caller=SERVER_NAME, state_root=sr,
        )
    if name == "rw.handoff_read":
        return handoff.handoff_read(ws, path=arguments.get("path", handoff.DEFAULT_HANDOFF_FILENAME))
    if name == "rw.journal_tail":
        entries = journal.tail(sr, n=int(arguments.get("n", 20)),
                                filter_path=arguments.get("filter_path"),
                                filter_mode=arguments.get("filter_mode"))
        return {"ok": True, "entries": entries}
    if name == "rw.validate":
        return validate.validate_content(arguments["content"], format_hint=arguments.get("format_hint"),
                                          target_path=arguments.get("target_path"))
    if name == "rw.analytics":
        return analytics.analyze_journal(sr, since=arguments.get("since"),
                                          session_filter=arguments.get("session_filter"))
    if name == "rw.chunk_preview":
        return chunks.chunk_preview(sr, session=arguments["session"], separator=arguments.get("separator", ""))
    if name == "rw.checkpoint":
        return checkpoint.checkpoint_save(
            sr, name=arguments["name"], data=arguments["data"],
            fmt=arguments.get("format", "json"), ttl=arguments.get("ttl", "session"), caller=SERVER_NAME,
        )
    if name == "rw.checkpoint_read":
        return checkpoint.checkpoint_read(sr, name=arguments["name"])
    if name == "rw.checkpoint_list":
        return checkpoint.checkpoint_list(sr)
    if name == "rw.checkpoint_cleanup":
        return checkpoint.checkpoint_cleanup(sr, include_session=bool(arguments.get("include_session", True)))
    raise ResilientWriteError("policy_violation", "unknown", context={"unknown_tool": name})


def _envelope_or_error(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return _dispatch(name, arguments)
    except ResilientWriteError as exc:
        env = exc.to_envelope()
        env.setdefault("context", {}).setdefault("tool", name)
        return env


def build_server() -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return list(_TOOL_DEFINITIONS)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = _envelope_or_error(name, arguments or {})
        return [TextContent(type="text", text=json.dumps(result, separators=(",", ":"), sort_keys=True))]

    return server


async def _run_stdio() -> None:
    server = build_server()
    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


async def _run_sse(host: str, port: int) -> None:
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    server = build_server()
    init_options = server.create_initialization_options()
    sse = SseServerTransport("/messages/")
    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], init_options)
    app = Starlette(routes=[Route("/sse", endpoint=handle_sse), Mount("/messages/", app=sse.handle_post_message)])
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    await srv.serve()


async def _run_streamable_http(host: str, port: int) -> None:
    import contextlib
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount
    server = build_server()
    session_manager = StreamableHTTPSessionManager(app=server, stateless=False)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run(): yield
    app = Starlette(routes=[Mount("/mcp", app=session_manager.handle_request)], lifespan=lifespan)
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    await srv.serve()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="resilient-write", description="Resilient-write MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "-w", "--workspace",
        action="append",
        default=None,
        dest="workspaces",
        help="Add a workspace directory (repeatable). Prepends to $RW_WORKSPACE.",
    )
    args = parser.parse_args()

    if args.workspaces:
        existing = os.environ.get("RW_WORKSPACE", "")
        if existing and existing.strip().startswith("["):
            try:
                existing_roots = json.loads(existing)
            except json.JSONDecodeError:
                existing_roots = [existing]
        elif existing:
            existing_roots = [existing]
        else:
            existing_roots = []
        os.environ["RW_WORKSPACE"] = json.dumps(args.workspaces + existing_roots)
    if args.transport == "stdio":
        asyncio.run(_run_stdio())
    elif args.transport == "sse":
        asyncio.run(_run_sse(args.host, args.port))
    else:
        asyncio.run(_run_streamable_http(args.host, args.port))


if __name__ == "__main__":
    main()
