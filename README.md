# resilient-write

[![PyPI version](https://img.shields.io/pypi/v/resilient-write)](https://pypi.org/project/resilient-write/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/resilient-write)](https://pypi.org/project/resilient-write/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-186%20passed-brightgreen)]()

An MCP server that gives coding agents a **durable, fault-tolerant write surface**
so they can keep making forward progress when a tool call is blocked by a content
filter, a size cap, or an opaque transport error.

## Why this exists

Coding agents today write through a single tool like `Write` or `edit_file`.
When that tool rejects a payload (for any reason), the failure mode is usually:

1. The agent loses the content it was trying to write (it only exists in model memory).
2. There is no structured error, so the agent can't reason about what to change.
3. Retries thrash because the agent re-sends the exact same rejected content.
4. Any downstream step that depended on the file is now operating on half-broken state.
5. No handoff mechanism exists, so a fresh agent or sibling agent has to re-derive the context.

This project addresses those five failure modes as five orthogonal layers.

## Layered architecture (sixteen MCP tools, one convention file)

| Layer | Tool name | What it does | Catches |
|---|---|---|---|
| **L0** | `rw.risk_score`         | Static pre-flight classifier over draft content (regexes + length + binary heuristics). Returns a numeric risk score plus a list of detected patterns. | Secret-shaped strings, long lines, encoding issues before they hit the sink. |
| **L1** | `rw.safe_write`          | Transactional write: temp file + hash verify + atomic rename + journal append. | Half-written files, lost prior content, no audit trail. |
| **L2** | `rw.chunk_write`        | Write one numbered chunk to a session directory via safe_write; idempotent retries. | Single-chunk failures without losing prior chunks. |
| **L2** | `rw.chunk_append`       | Auto-incrementing chunk write — detects the highest index and writes index+1. | Misnumbered chunks; lets the agent stream sections without tracking indices. |
| **L2** | `rw.chunk_compose`      | Concatenate a session's chunks in order and write the result through safe_write. | Any single-call write that is too large or too risky; allows incremental progress with rollback. |
| **L2** | `rw.chunk_reset`        | Destructively wipe an in-progress chunk session. | Stale session state after an abandoned compose. |
| **L2** | `rw.chunk_status`       | Report which chunk indices are present and what total was declared. | Missing or duplicate chunks before compose. |
| **L2** | `rw.chunk_preview`      | Dry-run compose — returns concatenated content without writing to disk. | Pre-write validation; pair with `rw.validate` to catch errors before commit. |
| **L3** | `rw.typed_error` (schema) | Specification for structured tool errors `{reason_hint, detected_patterns, suggested_action, retry_budget}`. Wraps `safe_write` and `chunk_compose`. | Opaque tool-harness errors that agents cannot reason about. |
| **L4** | `rw.scratch_put`         | Store raw material out-of-band, content-addressed by SHA-256. | Cases where the content legitimately does not belong in the workspace. |
| **L4** | `rw.scratch_ref`         | Look up a scratchpad entry by hash or label without returning content. | Verify what is stored before deciding to surface it. |
| **L4** | `rw.scratch_get`         | Retrieve raw content by hash (disableable via `$RW_SCRATCH_DISABLE_GET`). | Controlled retrieval; supports write-only mode. |
| **L5** | `rw.handoff_write`       | Write a `HANDOFF.md` continuity envelope (YAML front-matter + body). Reports drift warnings. | Cross-agent and cross-session continuity when a task is interrupted. |
| **L5** | `rw.handoff_read`        | Parse a `HANDOFF.md` envelope and return structured front-matter plus body. | Picking up where another agent left off. |
| — | `rw.journal_tail`        | Inspection helper — last N rows of the L1 write journal, with optional filters. | Debugging write history and audit. |
| — | `rw.validate`            | Format-aware syntax validator (LaTeX, JSON, Python, YAML). | Structural errors (unbalanced braces, bad parses) before they reach disk. |
| — | `rw.analytics`           | Journal analytics — write counts, timing, hot paths, session summaries. | Understanding agent write patterns and diagnosing performance issues. |

Layers can be adopted independently. The minimum useful install is **L1 + L5**.

## Install

```bash
pip install resilient-write
```

Or run directly:

```bash
uvx resilient-write
```

### MCP client configuration

Add to your Claude Code, Cursor, or Codex MCP config:

```json
{
  "mcpServers": {
    "resilient-write": {
      "command": "uvx",
      "args": ["resilient-write"],
      "env": {
        "RW_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

See `docs/INSTALL.md` for full setup instructions for all clients.

## Usage

### Basic: write a file safely

The agent calls `rw.safe_write` instead of raw `Write`. The file is written atomically (temp → fsync → verify → rename) and logged to the audit journal.

```
rw.safe_write(path="src/main.py", content="print('hello')", mode="create")
→ {"ok": true, "path": "src/main.py", "sha256": "a1b2c3...", "bytes": 14}
```

### Pre-flight risk check

Before writing content that might contain tokens or credentials, run `rw.risk_score`:

```
rw.risk_score(content="Bearer sk-ant-oat01-AAAA...")
→ {"ok": true, "score": 0.82, "verdict": "high",
   "detected_patterns": [{"kind": "api_key", ...}],
   "suggested_actions": [{"action": "redact", "targets": ["api_key"]}]}
```

If the verdict is `high`, redact the flagged patterns before writing.

### Large files: chunked writes

For files over ~5KB, build them section by section:

```
rw.chunk_append(session="my-report", content="# Introduction\n...")
rw.chunk_append(session="my-report", content="# Methods\n...")
rw.chunk_append(session="my-report", content="# Results\n...")

# Preview before committing
rw.chunk_preview(session="my-report")

# Validate syntax
rw.validate(content=<preview_content>, format_hint="latex")

# Compose the final file
rw.chunk_compose(session="my-report", output_path="report.tex", cleanup=true)
```

If a chunk fails mid-session, prior chunks are already on disk — just retry the failing one.

### Handling errors

Every failure returns a structured envelope the agent can branch on:

```json
{
  "ok": false,
  "error": "blocked",
  "reason_hint": "content_filter",
  "detected_patterns": ["api_key"],
  "suggested_action": "redact",
  "retry_budget": 2
}
```

The agent reads `suggested_action` and acts accordingly — no guesswork, no blind retries.

### Sensitive content: scratchpad

Store raw credentials or PII out-of-band instead of writing them to the workspace:

```
rw.scratch_put(content="sk-ant-oat01-real-key-here", label="captured-token")
→ {"ok": true, "sha256": "5c9a3b...", "dedup": false}
```

The `.resilient_write/` directory is gitignored. Reference the hash in your report instead of the raw value.

### Session handoff

Before ending a session (or when blocked), save state for the next agent:

```
rw.handoff_write(envelope={
  "task_id": "my-report",
  "status": "partial",
  "agent": "claude-opus-4-6",
  "summary": "Sections 1-3 complete, section 4 blocked on content filter",
  "next_steps": ["Redact api_key patterns in section 4", "Retry chunk 4"],
  "last_good_state": [{"path": "report.tex", "sha256": "4b0c12..."}]
})
```

A fresh agent calls `rw.handoff_read` to pick up where you left off.

### Making agents prefer rw.* tools automatically

Drop a `CLAUDE.md` (for Claude Code) or `.cursorrules` (for Cursor) in your project root. The server also sends MCP-level `instructions` at initialization, telling compatible clients to prefer `rw.*` tools over raw writes. See the included `CLAUDE.md` for the recommended content.

## Status

- [x] Architecture document
- [x] Per-layer specs
- [x] JSON schemas (`spec/errors.schema.json`)
- [x] Reference Python implementation (all six layers, 16 tools)
- [x] Test suite (186 tests, all green)
- [x] Published MCP config snippets (`docs/INSTALL.md`)
- [x] [Published to PyPI](https://pypi.org/project/resilient-write/)

## Origin

This project was spun out of a concrete failure observed while producing an
LLM-CLI telemetry analysis report. A Write tool call was silently rejected
when the draft contained redacted-looking credential strings; the agent
recovered only after five retries and a hand-written chunked-append workaround.
The five layers here correspond to the five things that would have caught
that failure before it wasted cycles. See `docs/SCENARIOS.md` for the full
postmortem.
