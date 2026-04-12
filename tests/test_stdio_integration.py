"""End-to-end MCP stdio integration test.

Spawns the `resilient-write` console script as a real subprocess and
speaks JSON-RPC to it through the MCP Python SDK's high-level
`ClientSession`. This covers the parts of the server that the direct
dispatch-adapter tests skip: tool registration via `@server.list_tools`,
the `initialize` handshake, argument coercion out of JSON, and the
`call_tool` path that wraps responses in `TextContent`.

Slower than the in-process tests (~1s per case), so we keep the
coverage surface small — one success per layer is enough to catch
wiring regressions.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _params(workspace: Path) -> StdioServerParameters:
    """Build stdio params that launch the installed console script.

    We use `python -m resilient_write.server` rather than the
    `resilient-write` console script so the test works even when the
    script isn't on `$PATH` (fresh checkouts before `uv sync` wires it
    up, bare venvs, etc.). The env is forwarded wholesale so PATH and
    VIRTUAL_ENV reach the child intact.
    """
    env = dict(os.environ)
    env["RW_WORKSPACE"] = str(workspace)
    # Make sure we don't accidentally inherit a stale get-gate from
    # another test in the same process.
    env.pop("RW_SCRATCH_DISABLE_GET", None)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "resilient_write.server"],
        env=env,
    )


def _text(result) -> str:
    """Extract the single TextContent payload from a call_tool result."""
    assert result.content, f"no content in result: {result}"
    first = result.content[0]
    # The SDK exposes TextContent with a `.text` attribute.
    return getattr(first, "text")


async def test_stdio_initialize_and_list_tools(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            assert "rw.safe_write" in names
            assert "rw.risk_score" in names
            assert "rw.chunk_write" in names
            assert "rw.scratch_put" in names
            assert "rw.handoff_write" in names
            assert "rw.journal_tail" in names


async def test_stdio_safe_write_round_trip(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "rw.safe_write",
                {"path": "notes.txt", "content": "hello from stdio\n"},
            )
            payload = json.loads(_text(result))
            assert payload["ok"] is True
            assert payload["path"] == "notes.txt"

            on_disk = (tmp_path / "notes.txt").read_text()
            assert on_disk == "hello from stdio\n"

            tail = await session.call_tool("rw.journal_tail", {"n": 5})
            tail_payload = json.loads(_text(tail))
            assert tail_payload["ok"] is True
            assert len(tail_payload["entries"]) == 1
            assert tail_payload["entries"][0]["path"] == "notes.txt"


async def test_stdio_risk_score_safe_content(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "rw.risk_score",
                {"content": "# heading\nharmless prose\n"},
            )
            payload = json.loads(_text(result))
            assert payload["ok"] is True
            assert payload["verdict"] == "safe"
            assert payload["detected_patterns"] == []


async def test_stdio_typed_error_envelope(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "rw.safe_write",
                {"path": "../escape.txt", "content": "x"},
            )
            payload = json.loads(_text(result))
            assert payload["ok"] is False
            assert payload["schema_version"] == "1"
            assert payload["error"] == "policy_violation"
            assert payload["reason_hint"] == "permission"
            assert payload["context"]["tool"] == "rw.safe_write"


async def test_stdio_chunk_compose_round_trip(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for i, piece in enumerate(["alpha ", "bravo ", "charlie"], start=1):
                r = await session.call_tool(
                    "rw.chunk_write",
                    {
                        "session": "stdio_demo",
                        "index": i,
                        "content": piece,
                        "total_expected": 3,
                    },
                )
                assert json.loads(_text(r))["ok"] is True

            compose = await session.call_tool(
                "rw.chunk_compose",
                {
                    "session": "stdio_demo",
                    "output_path": "composed.txt",
                    "cleanup": True,
                },
            )
            payload = json.loads(_text(compose))
            assert payload["ok"] is True
            assert payload["chunk_count"] == 3
            assert (tmp_path / "composed.txt").read_text() == "alpha bravo charlie"


async def test_stdio_scratchpad_round_trip(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            put = await session.call_tool(
                "rw.scratch_put",
                {"content": "raw material\n", "label": "stdio_demo"},
            )
            put_payload = json.loads(_text(put))
            assert put_payload["ok"] is True
            sha = put_payload["sha256"]

            got = await session.call_tool(
                "rw.scratch_get", {"sha256": sha}
            )
            got_payload = json.loads(_text(got))
            assert got_payload["ok"] is True
            assert got_payload["content"] == "raw material\n"


async def test_stdio_handoff_round_trip(tmp_path: Path) -> None:
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            envelope = {
                "task_id": "stdio-task",
                "status": "complete",
                "agent": "claude-opus-4-6",
                "summary": "end to end stdio smoke test",
                "next_steps": [],
                "last_good_state": [],
            }
            w = await session.call_tool(
                "rw.handoff_write", {"envelope": envelope}
            )
            assert json.loads(_text(w))["ok"] is True

            r = await session.call_tool("rw.handoff_read", {})
            payload = json.loads(_text(r))
            assert payload["ok"] is True
            assert payload["envelope"]["task_id"] == "stdio-task"
