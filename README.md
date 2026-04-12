# resilient-write

An MCP server that gives coding agents a **durable, fault-tolerant write surface**
so they can keep making forward progress when a tool call is blocked by a content
filter, a size cap, or an opaque transport error.

This repo is a **design + spec** at first. Code lands after the spec is frozen.

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

## Repo layout (planned)

```
resilient-write/
├── README.md                    # this file
├── docs/
│   ├── ARCHITECTURE.md          # deep dive on each layer
│   ├── API.md                   # MCP tool schemas (input/output)
│   ├── POLICY.md                # default L0 classifier patterns + thresholds
│   ├── HANDOFF_SCHEMA.md        # envelope format for L5
│   └── SCENARIOS.md             # walk-through of real failure modes
├── spec/
│   ├── tools.schema.json        # JSON Schema for the sixteen tools
│   └── handoff.schema.json      # JSON Schema for HANDOFF.md front-matter
├── src/                         # Python implementation (post-spec)
│   └── resilient_write/
│       ├── __init__.py
│       ├── server.py            # MCP entrypoint
│       ├── safe_write.py        # L1
│       ├── risk_score.py        # L0
│       ├── chunk_compose.py     # L2
│       ├── scratchpad.py        # L4
│       ├── handoff.py           # L5
│       └── errors.py            # L3 typed error envelope
└── tests/
    └── scenarios/               # replay fixtures for SCENARIOS.md
```

## Install (planned)

```
uvx resilient-write              # run the MCP server
# or
pipx install resilient-write
```

MCP config for Claude Code / Cursor / Codex / Copilot clients lives in `docs/INSTALL.md`.

## Status

- [x] Architecture document
- [x] Per-layer specs
- [x] JSON schemas (`spec/errors.schema.json`)
- [x] Reference Python implementation (all six layers, 16 tools)
- [x] Test fixtures (186 tests, all green)
- [x] Published MCP config snippets (`docs/INSTALL.md`)
- [ ] Published to PyPI

## Origin

This project was spun out of a concrete failure observed while producing an
LLM-CLI telemetry analysis report. A Write tool call was silently rejected
when the draft contained redacted-looking credential strings; the agent
recovered only after five retries and a hand-written chunked-append workaround.
The five layers here correspond to the five things that would have caught
that failure before it wasted cycles. See `docs/SCENARIOS.md` for the full
postmortem.
