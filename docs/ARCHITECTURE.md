# Architecture

`resilient-write` is a single MCP server exposing six tools that compose into
a durable write layer. Each tool owns one concern. The layers are orthogonal
so you can install any subset.

```
┌──────────────────────────────────────────────────────────────────────┐
│                    agent (Claude Code, Cursor, ...)                  │
└──────────────────────────────────────────────────────────────────────┘
                 │  draft content
                 ▼
    ┌───────────────────────┐
    │ L0  rw.risk_score     │  ◄── pre-flight: will this write fail?
    └───────────┬───────────┘
                │ score + hints
                ▼
    ┌───────────────────────┐
    │ L1  rw.safe_write     │  ◄── transactional: temp + hash + rename
    └───────────┬───────────┘
                │ ok / typed_error
                ▼          ▲
    ┌───────────────────────┐
    │ L2  rw.chunk_compose  │  ◄── build from numbered chunks
    └───────────┬───────────┘
                │
                ▼
    ┌───────────────────────┐     ┌───────────────────────┐
    │ L4  rw.scratchpad     │     │ L5  rw.handoff        │
    │   (out-of-band raw)   │     │ (HANDOFF.md envelope) │
    └───────────────────────┘     └───────────────────────┘

            L3  rw.typed_error  ← schema that wraps everything above
```

## Design principles

1. **Fail transparently.** Every failure returns a structured error the agent can reason about, not free text.
2. **Never overwrite in place.** All writes go through temp files + atomic rename. The previous good version of a file is recoverable.
3. **Stateful by default.** A `.resilient_write/` directory in each workspace holds the journal, chunks, scratchpad, and policy. State is append-only.
4. **Policy is configurable per workspace.** Default redaction patterns live in `docs/POLICY.md`; workspaces can extend or override.
5. **No hidden magic.** Every tool call is logged. Every redaction is traceable. The agent can always read the journal to understand why something was blocked.
6. **Layers are optional.** You can use `safe_write` alone. You can add `risk_score` later. You can adopt `handoff` as pure convention with zero code.

---

## Layer 0 — `rw.risk_score` (pre-flight classifier)

**Purpose:** tell the agent, *before* it calls a sink like `safe_write`, whether
the current draft is likely to be rejected by a downstream content filter.

**Input:**
```json
{
  "content": "<string>",
  "language_hint": "json|http|markdown|latex|...",
  "target_path": "optional/path.ext"
}
```

**Output:**
```json
{
  "score": 0.82,
  "verdict": "high|medium|low|safe",
  "detected_patterns": [
    {"kind": "api_key", "pattern": "sk-ant-", "location": "line 12"},
    {"kind": "long_line", "pattern": ">2000 chars", "location": "line 46"}
  ],
  "suggested_actions": [
    {"action": "redact", "targets": ["api_key"]},
    {"action": "split", "reason": "large_total_size"}
  ]
}
```

**Implementation notes:**
- Static regex + heuristics only, no LLM calls. Deterministic, fast, < 50ms for 100 KB.
- Patterns live in `docs/POLICY.md` and can be extended with a workspace-local YAML.
- Each match records the regex name and approximate location so the agent can redact surgically.
- Thresholds: score > 0.7 → `high`, 0.4 → `medium`, 0.1 → `low`, else `safe`.

**Typical call sites:** before any `safe_write` on content that was assembled
from model output, especially when that content includes payload samples,
credentials, or user-provided data.

---

## Layer 1 — `rw.safe_write` (transactional write)

**Purpose:** every write is an all-or-nothing transaction. A failed write never
corrupts the previous good version.

**Input:**
```json
{
  "path": "report.tex",
  "content": "<string>",
  "expected_prev_sha256": "optional-hash-of-previous-version",
  "mode": "create|overwrite|append"
}
```

**Output on success:**
```json
{
  "ok": true,
  "path": "report.tex",
  "sha256": "4b0...",
  "bytes": 50347,
  "journal_id": "wj_01HF..."
}
```

**Output on failure:** a typed error (see L3).

**Algorithm:**
1. If `expected_prev_sha256` is set, hash the current file and compare. Abort with `stale_precondition` if mismatch.
2. Write `content` to `path.tmp.<uuid>` with `O_CREAT | O_EXCL`.
3. Re-read the temp file and verify `sha256(temp) == sha256(content)`. Abort with `write_corruption` if mismatch.
4. `os.rename(temp, path)` — atomic on POSIX.
5. Append a row to `.resilient_write/journal.jsonl` with timestamp, path, sha256, bytes, caller, and mode.
6. Return success.

**State:** `.resilient_write/journal.jsonl` is append-only. A `journal_tail(n)`
helper returns the last N rows for quick inspection.

**Why this matters:** it turns every write into a checkpoint. If the next
step fails, the agent can `git diff` or hash-compare to know exactly what was
last persisted. If L1 is all you install, you still get 80% of the benefit.

---

## Layer 2 — `rw.chunk_compose` (chunked writing + resume)

**Purpose:** when a single-shot write is too large or too risky, produce the
final file by composing numbered chunks. Resume-safe: if chunk 5 fails,
chunks 1–4 are already persisted, and the agent only needs to retry chunk 5.

**Tools (two):**

### `rw.chunk_write`
```json
{
  "session": "report_tex_build",
  "index": 3,
  "content": "<chunk text>",
  "total_expected": 8
}
```
Writes `.resilient_write/chunks/<session>/part-003.txt` via `safe_write`. Returns a typed error if rejected — *only that chunk* needs to retry.

### `rw.chunk_compose`
```json
{
  "session": "report_tex_build",
  "output_path": "report.tex",
  "separator": "\n\n"
}
```
- Enumerates all `part-*.txt` files in the session.
- Verifies the set is contiguous (`part-001..part-total_expected`).
- Concatenates in order.
- Writes the result via `safe_write` to `output_path`.
- Returns the final hash and the list of chunk hashes.

**Why this matters:** it is exactly the pattern I ended up doing by hand with
`cat >> report.tex <<EOF` in three stages. Formalising it means the agent
doesn't have to improvise the same recovery each time.

**State:** chunks live under `.resilient_write/chunks/<session>/`. A
`rw.chunk_reset` helper wipes a session when the agent wants to start over.

---

## Layer 3 — `rw.typed_error` (schema, not a tool)

**Purpose:** every error from L1 / L2 / L4 uses the same shape so the agent
can branch on `reason_hint` programmatically.

**Schema:**
```json
{
  "error": "blocked|stale_precondition|write_corruption|quota_exceeded|policy_violation",
  "reason_hint": "content_filter|size_limit|encoding|permission|network|unknown",
  "detected_patterns": ["api_key", "long_line"],
  "suggested_action": "redact|split|escape|ask_user|retry_later|abort",
  "retry_budget": 2,
  "context": {
    "path": "report.tex",
    "attempt": 3,
    "bytes_attempted": 48210
  }
}
```

**Fields:**
- `error` (required): a short enumerated string identifying the *class* of failure.
- `reason_hint` (required): a best-effort hint about *why* the sink rejected the payload. Free text is allowed if nothing matches, but callers should prefer the enum.
- `detected_patterns`: only populated when the failure is a content-filter rejection; mirrors the L0 output format so the agent can hand it straight back to `rw.risk_score`.
- `suggested_action`: the agent treats this as a hint. It can choose a different action if context warrants, but defaults to the suggestion.
- `retry_budget`: how many retries the caller has left before the tool refuses to accept more. Decremented on each structured failure.

**Why this matters:** it is the *only* way the agent can tell "this was a
filter block, try redacting" apart from "this was a disk-full error, abort
the whole task."

---

## Layer 4 — `rw.scratchpad` (out-of-band raw storage)

**Purpose:** some content genuinely does not belong in the working tree:
raw credentials captured during analysis, un-redacted PII samples, large
binary dumps. Scratchpad gives the agent a place to keep that material
*outside* the tree so it can still be referenced by hash or path.

**Layout:**
```
.resilient_write/scratch/
  <sha256>.bin          # content keyed by hash
  index.jsonl           # {sha256, original_label, created_at, notes}
```

**Tools:**

### `rw.scratch_put`
```json
{
  "content": "<raw>",
  "label": "raw_event_logging_batch_txn_2c1e80"
}
```
Returns `{ "sha256": "...", "scratch_path": ".resilient_write/scratch/...bin" }`.

### `rw.scratch_ref`
Looks up a scratchpad entry by hash or label; returns metadata without the content.

### `rw.scratch_get`
Returns the raw content. This is the only tool that can surface a scratched item back into the agent's context, so it is the natural place to enforce access policies.

**Rules:**
- `.resilient_write/` is gitignored by default.
- The main working tree references scratch material only by hash, never by inline content.
- A workspace policy can forbid `scratch_get` entirely, meaning scratched material is write-only for the current session.

**Why this matters:** in the telemetry report we had real bearer tokens,
GitHub PATs, and user emails in the raw evidence. The right place for those
is scratchpad, not the main file tree. The report body references redacted
placeholders; the un-redacted versions stay in `.resilient_write/scratch/`
with their hashes, and are never indexed, committed, or synced.

---

## Layer 5 — `rw.handoff` (task continuity envelope)

**Purpose:** when a task is interrupted — filter block, context exhaustion,
user stop, process crash — a fresh agent (or a sibling in a plan) can pick
up without re-deriving context from scratch.

**Tool:**
```json
{
  "task_id": "llm-telemetry-report",
  "status": "complete|partial|blocked|handed_off",
  "summary": "Built the 19-page report and 22-slide deck. Payload appendix uses minted.",
  "next_steps": [
    "Run second pdflatex pass to resolve cross-refs after editing §5."
  ],
  "context_hints": [
    "minted requires -shell-escape and two passes",
    "tikz@deactivatthings stub needed for TL2022"
  ],
  "last_good_state": [
    {"path": "report.tex",       "sha256": "4b0..."},
    {"path": "presentation.tex", "sha256": "9ea..."}
  ],
  "open_questions": [],
  "blockers": []
}
```

**Effect:** writes a `HANDOFF.md` in the current working directory (idempotent;
the file is overwritten if the same `task_id` already has an envelope).

**Read path:** a fresh agent starting work can `rw.handoff_read(path)` to
get back the structured envelope.

**Rule:** any agent that terminates work — for any reason — before marking
the task `complete` SHOULD emit a handoff envelope. Clients can enforce this
with a post-run hook.

**Why this matters:** in the current failure, there was no way for a rescue
agent to know which files were known-good. A handoff envelope closes that
gap with one write.

---

## State directory layout

Everything lives under `.resilient_write/` at the workspace root:

```
.resilient_write/
├── journal.jsonl              # append-only write journal (L1)
├── chunks/                    # active chunk sessions (L2)
│   └── <session>/
│       ├── manifest.json
│       └── part-001.txt, ...
├── scratch/                   # out-of-band raw storage (L4)
│   ├── <sha256>.bin
│   └── index.jsonl
├── policy.yaml                # overrides for L0 patterns + thresholds
└── handoffs/                  # historical L5 envelopes (optional)
    └── 2026-04-11T17-28_llm-telemetry-report.md
```

`.resilient_write/` SHOULD be gitignored by default in every workspace.
A `.gitignore` template is shipped in `docs/INSTALL.md`.

---

## What each layer catches (mapped to real failure modes)

| Real failure | Caught by |
|---|---|
| Draft contains `sk-ant-oat01-…` and the Write tool silently rejects it | **L0** flags `api_key` before the sink call; **L3** returns a typed `blocked/content_filter` error if the sink still rejects |
| A 90 KB heredoc write is truncated mid-way and leaves a broken `.tex` file | **L1** aborts with `write_corruption`, leaves the previous file untouched |
| A very large file cannot be written in one shot even after redaction | **L2** splits into chunks; only the failing chunk retries |
| Agent needs to keep raw credentials around for audit but not in the working tree | **L4** stores them in `.resilient_write/scratch/` keyed by hash |
| Filter-blocked task needs to be resumed by a different agent | **L5** leaves a `HANDOFF.md` with `last_good_state` hashes |
| Agent retries identical rejected content in a loop | **L3** `retry_budget` forces escalation after N attempts |
