# Error handling guide (L3)

Every failing tool in `resilient-write` returns the same envelope shape.
The formal schema is at [`spec/errors.schema.json`](../spec/errors.schema.json);
this document tells the MCP client what to *do* when it sees one.

## Envelope at a glance

```json
{
  "ok": false,
  "schema_version": "1",
  "error": "blocked",
  "reason_hint": "content_filter",
  "detected_patterns": ["api_key", "github_pat"],
  "suggested_action": "redact",
  "retry_budget": 2,
  "context": {
    "tool": "rw.safe_write",
    "path": "report.tex",
    "score": 0.82,
    "verdict": "high"
  }
}
```

All fields are **always present**. `detected_patterns` is an empty list
for non-filter failures. `context` is a per-error bag; conventional keys
are documented below but clients should tolerate unknown keys.

## `error` (coarse class)

| Value | When it fires | First response |
|---|---|---|
| `blocked` | The write was refused by a content filter (L0 or a downstream sink). | Consult `detected_patterns`, redact, retry. |
| `stale_precondition` | A mode guard or `expected_prev_sha256` check failed, or a resource the caller assumed existed didn't (missing session, missing chunk, missing handoff). | Re-read current state. Do **not** retry with the same precondition. |
| `write_corruption` | Read-back hash didn't match, or an index/manifest on disk was unparseable, or a scratch bin failed re-hash on read. | Treat the affected file as suspect. Do not retry blindly. |
| `quota_exceeded` | Disk-space or size-limit error from the OS (ENOSPC). | Free space or split via `rw.chunk_write`. |
| `policy_violation` | Path traversal, invalid enum value, absolute path, malformed input, `scratch_get` disabled by env. | Fix the caller's arguments. Almost never retriable. |

## `reason_hint` (fine-grained why)

This is the field an agent should branch on for automated recovery.

| Value | Meaning | Recommended action | Retriable without operator? |
|---|---|---|---|
| `content_filter` | Something in the payload trips a regex in L0 or is silently rejected downstream. | Read `detected_patterns`, apply the `suggested_action` (usually `redact`), retry with the redacted draft. | **No** — retrying unchanged content is the original failure mode this project exists to prevent. |
| `size_limit` | Disk full, payload too large, or a size heuristic triggered. | Free space or use `rw.chunk_write` + `rw.chunk_compose`. | **Yes** (after corrective action). |
| `encoding` | Not valid UTF-8, malformed base64, bad YAML/JSON in an input. | Switch `encoding` (e.g. `base64` for binary scratch content) or fix the input shape. | Only after caller fixes the input. |
| `permission` | Filesystem EACCES/EPERM, absolute-path or path-traversal rejection, `scratch_get` disabled by workspace. | Escalate to the user. These are not transient. | **No**. |
| `network` | Reserved for future layers that touch the network. Not currently produced by any tool in this server. | Retry with backoff. | **Yes**. |
| `unknown` | Catch-all when the error didn't fit a more specific hint. | Treat as non-retriable by default. Inspect `context`. | **No**. |

The `is_retriable()` helper on `ResilientWriteError` returns `True` only
for `network` and `size_limit`. Everything else defaults to "ask the
user or fix the caller." This is deliberately conservative: the failure
mode this project exists to prevent is agents looping on the same
rejected content, so the default posture is "stop and think" rather than
"retry with a different prompt."

## `suggested_action`

A short verb the caller should consider before deciding its own
strategy. The enum is intentionally small:

- `redact` — scrub detected patterns, retry.
- `split` — split the payload and use `rw.chunk_write` / `rw.chunk_compose`.
- `escape` — the content is well-formed but a single line is too long;
  wrap it.
- `ask_user` — defer to the operator.
- `retry_later` — transient; back off and try again.
- `abort` — give up; this is not recoverable by the agent alone.

## `detected_patterns`

For `reason_hint=content_filter`, this lists the L0 pattern *families*
that matched, drawn from the taxonomy in [`docs/POLICY.md`](POLICY.md):
`api_key`, `github_pat`, `jwt`, `pem_block`, `aws_secret`, `pii`,
`binary_hint`.

The raw per-match details — snippet, line number, pattern name — live
under `context.detected` when a classify rejection produced the error.
Clients that want to perform surgical redaction should read
`context.detected` rather than guessing from the family list alone.

## `retry_budget`

Counts down **per-call**, not per-file, per design
(`.agent/memory/decisions.md` → "L3 typed errors"). A non-zero budget
means the server is willing to accept another attempt; zero means the
caller should stop without the user's involvement.

## `context`

Per-error bag. Conventional keys that appear in the current
implementation:

| Key | Produced by | Meaning |
|---|---|---|
| `tool` | MCP adapter | Name of the tool that raised (injected by the server wrapper). |
| `path` | L1, L2, L5 | Workspace-relative file path. |
| `sha256` | L1, L4 | Hash of the file or scratch entry. |
| `expected_prev_sha256` / `actual_prev_sha256` | L1 | Optimistic-concurrency guard fields. |
| `existing_sha256` | L1 (create-over-existing) | Hash of the file that blocked a `create`. |
| `score` / `verdict` / `detected` / `suggested_actions` | L0 via classify hook | Full classifier report. |
| `session` / `have` / `missing` / `unexpected` / `total_expected` | L2 | Chunk compose diagnostics. |
| `encoding` / `valid` | L4 | Bad-encoding diagnostics. |
| `errno` / `strerror` | L1 OSError classifier | Raw OS error when permissioning failed. |

Clients should not rely on the absence of a key for negative inference —
future layers may add keys. Always probe for what you need.

## Minimal client-side handler

```python
def handle_rw_response(env: dict) -> Action:
    if env.get("ok"):
        return Action.CONSUME_SUCCESS

    hint = env["reason_hint"]
    if hint == "content_filter":
        families = env["detected_patterns"]
        return Action.REDACT(families, detail=env["context"].get("detected", []))
    if hint in ("network", "size_limit"):
        return Action.RETRY_WITH_BACKOFF
    if hint == "encoding":
        return Action.FIX_INPUT_AND_RETRY
    # permission, unknown, or anything else unexpected:
    return Action.ASK_USER(env)
```

This is the simplest policy that honours the spirit of the design: stop
looping, surface structured information, and hand control back to the
operator when in doubt.
