# API — MCP tool surface

All tools share the same error envelope (see `ARCHITECTURE.md` L3). Success
responses are tool-specific. Inputs and outputs are JSON objects.

## `rw.risk_score`

**Purpose:** L0 pre-flight classifier over draft content.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes | The draft text to classify |
| `language_hint` | string | no | Helps pattern matcher (`json`, `http`, `latex`, `markdown`, ...) |
| `target_path` | string | no | File path the content is destined for |

**Output:**
```json
{
  "score": 0.82,
  "verdict": "high",
  "bytes": 48210,
  "line_count": 612,
  "detected_patterns": [
    {"kind": "api_key",   "pattern": "sk-ant-",   "match": "sk-ant-oat01-…", "line": 12},
    {"kind": "github_pat","pattern": "gho_",      "match": "gho_61AN…",      "line": 46},
    {"kind": "long_line", "pattern": ">2000",     "match": null,             "line": 204}
  ],
  "suggested_actions": [
    {"action": "redact", "targets": ["api_key", "github_pat"]},
    {"action": "split",  "reason": "risk_concentration_in_appendix"}
  ]
}
```

**Verdict thresholds:** `high ≥ 0.7`, `medium ≥ 0.4`, `low ≥ 0.1`, else `safe`.

---

## `rw.safe_write`

**Purpose:** L1 transactional write.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Destination path (workspace-relative) |
| `content` | string | yes | File content |
| `mode` | enum | no | `create` (default), `overwrite`, `append` |
| `expected_prev_sha256` | string | no | Guard against concurrent edits; set to empty string for `create` |
| `classify` | boolean | no | Run the L0 classifier first; reject if verdict meets threshold. Default `false`. |
| `classify_reject_at` | enum | no | Minimum verdict that triggers rejection: `low`, `medium`, `high` (default). |
| `policy_override` | object | no | Workspace-local overrides for L0 pattern matcher (rare) |

**Success output:**
```json
{
  "ok": true,
  "path": "report.tex",
  "sha256": "4b0c...",
  "bytes": 50347,
  "mode_applied": "overwrite",
  "journal_id": "wj_01HF2Z...",
  "wrote_at": "2026-04-11T17:28:04Z"
}
```

**Failure output:** typed-error envelope (L3).

---

## `rw.chunk_write`

**Purpose:** L2 — write one chunk of a larger compose session.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `session` | string | yes | Session name; chunks for the same session share a directory |
| `index` | integer | yes | 1-based chunk index |
| `content` | string | yes | Chunk content |
| `total_expected` | integer | no | Hint used by `rw.chunk_compose` to verify completeness |

**Success output:**
```json
{
  "ok": true,
  "session": "report_tex_build",
  "index": 3,
  "chunk_path": ".resilient_write/chunks/report_tex_build/part-003.txt",
  "sha256": "9c4..."
}
```

---

## `rw.chunk_compose`

**Purpose:** L2 — concatenate a session's chunks into a final file via `safe_write`.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `session` | string | yes | |
| `output_path` | string | yes | Final file path |
| `separator` | string | no | Inserted between chunks. Default `""`. |
| `cleanup` | boolean | no | If `true`, delete the chunk directory after a successful compose. Default `false`. |

**Success output:**
```json
{
  "ok": true,
  "output_path": "report.tex",
  "sha256": "4b0...",
  "bytes": 50347,
  "chunk_count": 8,
  "chunk_hashes": ["9c4...", "a12...", ...]
}
```

---

## `rw.chunk_append`

**Purpose:** L2 — auto-incrementing chunk write. Detects the highest existing
index in the session and writes `index + 1`. If the session does not exist yet,
starts at 1. Removes the need for the caller to track chunk numbers.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `session` | string | yes | Session name (pattern `^[A-Za-z0-9_\-]{1,64}$`) |
| `content` | string | yes | Chunk content |
| `total_expected` | integer | no | Optional hint; compose will refuse to run until this many chunks exist |

**Success output:**
```json
{
  "ok": true,
  "session": "report_tex_build",
  "index": 4,
  "chunk_path": ".resilient_write/chunks/report_tex_build/part-004.txt",
  "sha256": "e7a...",
  "bytes": 1820,
  "journal_id": "wj_01HF3A..."
}
```

**Notes:** The return shape is identical to `rw.chunk_write` — `chunk_append`
delegates to `chunk_write` internally after computing the next index. Retrying
a failed `chunk_append` call is safe because it re-reads the directory to find
the current highest index.

---

## `rw.chunk_reset`

**Purpose:** L2 — destructively wipe an in-progress chunk session (directory
and all chunk files, including the manifest).

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `session` | string | yes | Session name (pattern `^[A-Za-z0-9_\-]{1,64}$`) |

**Success output:**
```json
{
  "ok": true,
  "session": "report_tex_build",
  "removed": 5,
  "existed": true
}
```

If the session directory does not exist, `removed` is `0` and `existed` is
`false` — the call still succeeds.

---

## `rw.chunk_status`

**Purpose:** L2 inspection helper — report which chunk indices are currently
present for a session and what `total_expected` was declared by the most recent
`chunk_write` or `chunk_append` call.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `session` | string | yes | Session name (pattern `^[A-Za-z0-9_\-]{1,64}$`) |

**Success output (session exists):**
```json
{
  "ok": true,
  "session": "report_tex_build",
  "exists": true,
  "total_expected": 8,
  "present_indices": [1, 2, 3, 5],
  "chunk_dir": ".resilient_write/chunks/report_tex_build"
}
```

**Success output (session does not exist):**
```json
{
  "ok": true,
  "session": "report_tex_build",
  "exists": false
}
```

**Notes:** Use this to decide which chunk to retry after a partial failure —
inspect the `present_indices` array to find gaps, then call `rw.chunk_write`
for the missing indices.

---

## `rw.chunk_preview`

**Purpose:** L2 — dry-run compose. Returns the concatenated content of a chunk
session *without* writing to disk. Performs all the same contiguity and
`total_expected` checks as `rw.chunk_compose`. Use this to validate content
(e.g. via `rw.validate`) before committing.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `session` | string | yes | Session name (pattern `^[A-Za-z0-9_\-]{1,64}$`) |
| `separator` | string | no | Inserted between chunks during concatenation. Default `""`. |

**Success output:**
```json
{
  "ok": true,
  "session": "report_tex_build",
  "content": "\\documentclass{article}\n...",
  "chunk_count": 8,
  "chunk_hashes": ["9c4...", "a12...", "..."],
  "total_bytes": 50347,
  "preview": true
}
```

**Notes:** A typical workflow is `chunk_preview` -> `rw.validate` on the
returned `content` -> if valid, `rw.chunk_compose` with the same separator.
The `preview: true` field distinguishes this from a real compose result.

---

## `rw.scratch_put` / `rw.scratch_ref` / `rw.scratch_get`

**Purpose:** L4 out-of-band storage.

### `rw.scratch_put`
**Input:**
```json
{ "content": "<raw bytes>", "label": "raw_event_logging_txn_2c1e80", "content_type": "application/json" }
```
**Output:**
```json
{ "sha256": "5c9...", "scratch_path": ".resilient_write/scratch/5c9...bin", "bytes": 12287 }
```

### `rw.scratch_ref`
**Input:** `{ "sha256": "5c9..." }` or `{ "label": "raw_event_logging_txn_2c1e80" }`
**Output:** full index entry: `{ sha256, label, created_at, bytes, content_type, notes }`.

### `rw.scratch_get`
**Input:** `{ "sha256": "5c9..." }`
**Output:** raw content (bytes/base64). **May be gated by workspace policy.**

---

## `rw.handoff`

**Purpose:** L5 — emit or read a continuity envelope.

### `rw.handoff_write`
**Input:**
```json
{
  "task_id": "llm-telemetry-report",
  "status": "partial",
  "summary": "Report appendix blocked on L0 classifier.",
  "next_steps": ["Redact sk-ant-* tokens and retry chunk 4 via chunk_write."],
  "context_hints": ["minted needs -shell-escape; run two passes."],
  "last_good_state": [
    {"path": "report.tex", "sha256": "4b0..."},
    {"path": "macros.tex", "sha256": "a12..."}
  ],
  "open_questions": [],
  "blockers": ["rw.safe_write rejected appendix body at line 412; content_filter"]
}
```
**Output:** `{ "ok": true, "handoff_path": "HANDOFF.md" }`

### `rw.handoff_read`
**Input:** `{ "path": "HANDOFF.md" }` (optional; defaults to workspace root)
**Output:** the parsed envelope.

---

## `rw.journal_tail`

**Purpose:** inspection helper — not a layer, but useful glue.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `n` | integer | no | Number of entries to return (default `20`, minimum `1`) |
| `filter_path` | string | no | Only return entries whose `path` matches this value |
| `filter_mode` | enum | no | Only return entries whose `mode` is `create`, `overwrite`, or `append` |

**Output:**
```json
{
  "entries": [
    {"journal_id": "wj_01HF2Z...", "ts": "2026-04-11T17:28:04Z", "path": "report.tex", "sha256": "4b0...", "bytes": 50347, "mode": "overwrite"},
    ...
  ]
}
```

---

## `rw.validate`

**Purpose:** Format-aware syntax validator. Checks content for structural
errors before writing — useful to catch problems pre-compose. No I/O, no
network; all checks are regex/token-level and fast even on large documents.

**Supported formats:** `latex`, `json`, `python`, `yaml`. Auto-detected from
`format_hint`, the `target_path` extension, or the content itself.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes | Content to validate |
| `format_hint` | enum | no | `latex`, `json`, `python`, `yaml`. Auto-detected if omitted. |
| `target_path` | string | no | Path hint for extension-based format detection (e.g. `paper.tex` -> LaTeX) |

**Success output (valid):**
```json
{
  "ok": true,
  "valid": true,
  "format": "json",
  "errors": [],
  "summary": "json: ok"
}
```

**Success output (invalid):**
```json
{
  "ok": true,
  "valid": false,
  "format": "latex",
  "errors": [
    {"line": 42, "col": null, "message": "\\begin{itemize} never closed", "severity": "error"},
    {"line": 1,  "col": null, "message": "\\documentclass not found",     "severity": "warning"}
  ],
  "summary": "latex: 1 error(s), 1 warning(s)"
}
```

**Notes:** The call always returns `"ok": true` — the `"valid"` boolean
carries the pass/fail signal. Only `"severity": "error"` entries cause
`valid` to be `false`; warnings alone still yield `valid: true`.

**LaTeX checks:** balanced braces, matched `\begin`/`\end` environments,
`\documentclass`/`\begin{document}`/`\end{document}` presence (warning),
`\being` typo (warning), unescaped `%` in URLs (warning), bare `_` outside
math mode (warning).

---

## `rw.analytics`

**Purpose:** Journal analytics. Analyses `.resilient_write/journal.jsonl` in a
single pass and returns structured metrics about write patterns, timing, hot
paths, session health, and write velocity.

**Input:**
| Field | Type | Required | Description |
|---|---|---|---|
| `since` | string | no | ISO-8601 timestamp; only include journal entries after this time |
| `session_filter` | string | no | Only include chunk sessions matching this name in the `sessions` breakdown |

**Success output:**
```json
{
  "ok": true,
  "total_writes": 47,
  "unique_paths": 12,
  "total_bytes_written": 283410,
  "by_mode": {"create": 8, "overwrite": 39},
  "by_caller": {"resilient-write": 47},
  "timeline": [
    {"ts": "2026-04-11T17:28:04Z", "path": "report.tex", "bytes": 50347, "mode": "overwrite"},
    "... (last 50 entries)"
  ],
  "hot_paths": [
    {"path": "report.tex", "write_count": 14, "total_bytes": 705258, "last_write": "2026-04-11T18:02:11Z"},
    "... (top 5)"
  ],
  "sessions": {
    "report_tex_build": {
      "chunk_writes": 8,
      "composes": 1,
      "first_write": "2026-04-11T17:20:00Z",
      "last_write": "2026-04-11T17:28:04Z",
      "duration_seconds": 484.0
    }
  },
  "write_velocity": {
    "writes_per_minute": 3.92,
    "avg_bytes_per_write": 6030.0,
    "peak_minute": "2026-04-11T17:25Z"
  },
  "period": {
    "first_entry": "2026-04-11T17:15:02Z",
    "last_entry": "2026-04-11T18:02:11Z",
    "duration_seconds": 2829.0
  }
}
```

**Notes:** All counters respect the `since` filter. The `session_filter` only
narrows the `sessions` breakdown — top-level counters (`total_writes`,
`hot_paths`, etc.) still reflect the full (post-`since`) journal.
