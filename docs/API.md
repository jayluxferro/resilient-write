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

## `rw.chunk_reset`

**Purpose:** L2 — wipe an in-progress chunk session.

**Input:** `{ "session": "report_tex_build" }`
**Output:** `{ "ok": true, "removed": 5 }`

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

**Input:** `{ "n": 20, "filter_path": "report.tex" }` (both optional)

**Output:**
```json
{
  "entries": [
    {"journal_id": "wj_01HF2Z...", "ts": "2026-04-11T17:28:04Z", "path": "report.tex", "sha256": "4b0...", "bytes": 50347, "mode": "overwrite"},
    ...
  ]
}
```
