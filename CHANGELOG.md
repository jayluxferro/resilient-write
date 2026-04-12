# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`RW_POLICY_FILE`** environment variable: point the L0 classifier at
  a policy YAML outside `.resilient_write/`. Absolute paths honoured
  as-is; relative paths resolve against the workspace root; missing
  files fall back to defaults without error.
- **End-to-end MCP stdio integration tests** (`tests/test_stdio_integration.py`):
  spawn the console script as a real subprocess and drive it through
  `mcp.ClientSession` (initialize handshake, `list_tools`, `call_tool`)
  for one success case per layer plus one typed-error envelope case.

### Changed
- **L0 `openai_key` regex** tightened with a negative lookahead so
  `sk-ant-*` / `sk-proj-*` tokens no longer double-match the generic
  OpenAI pattern. Anthropic and OpenAI-project hits now count toward
  their own pattern names only, producing cleaner detected-patterns
  output (scoring was already damped, so no verdict drift).

### Removed
- `FIRST_INSTRUCTION.md` bootstrap file. Redundant with the "Your first
  30 minutes" section in `AGENT.md`.
- `.agent/memory/user-profile.md` untracked from git (still present
  on disk for local agent use; gitignored going forward).


## [0.1.0] — 2026-04-12

First working release. Complete six-layer MCP server with 134 tests.

### Added
- **L0 — `rw.risk_score`** — deterministic pre-flight classifier. Regex
  families (`api_key`, `github_pat`, `jwt`, `pem_block`, `aws_secret`,
  `pii`, `binary_hint`) plus size heuristics. Damped family scoring
  saturating at `weight * 1.5`. Match snippets truncated to 16
  characters so responses never carry the full secret. Runs in ~11 ms
  on 142 KB (budget: 50 ms). Workspace overrides via
  `.resilient_write/policy.yaml`: `extend_patterns`, `disable_families`,
  `thresholds`, `retry_budget`.
- **L1 — `rw.safe_write`** — transactional write via `O_CREAT | O_EXCL`
  temp file + `fsync` + SHA-256 read-back verify + atomic `os.replace`.
  Supports `create` / `overwrite` / `append` and `expected_prev_sha256`
  optimistic concurrency. Every success appends one row to
  `.resilient_write/journal.jsonl` with `journal_id`, timestamp, path,
  sha256, bytes, mode, caller. `EACCES`/`EPERM` → `policy_violation`/
  `permission`; `ENOSPC` → `quota_exceeded`/`size_limit`.
- **L1 hook — `rw.safe_write(classify=True)`** — runs L0 before touching
  disk. Rejections raise `blocked`/`content_filter` envelopes carrying
  the full classifier report in `context`.
- **L1 inspection — `rw.journal_tail`** — returns the last N journal
  rows with optional `filter_path` / `filter_mode`.
- **L2 — `rw.chunk_write` / `rw.chunk_compose` / `rw.chunk_reset` /
  `rw.chunk_status`** — resume-safe chunked writes. Sessions live at
  `.resilient_write/chunks/<session>/part-NNN.txt` (1–999) with a small
  `manifest.json` tracking `total_expected`. `chunk_write` routes
  through `safe_write(mode=overwrite)` so retrying a failing chunk is
  idempotent and audit-logged. `chunk_compose` verifies contiguity,
  reconciles against the manifest, concatenates with an optional
  separator, writes the final file via `safe_write` again, returns per-
  chunk hashes, and optionally wipes the session on success.
- **L3 — typed error envelope** — formal JSON Schema at
  `spec/errors.schema.json`, force-included into the wheel at
  `resilient_write/_spec/`. `ResilientWriteError.to_envelope()` emits
  `{ok, schema_version, error, reason_hint, detected_patterns,
  suggested_action, retry_budget, context}`. Factory classmethods for
  each error kind. `is_retriable()` heuristic: only `network` and
  `size_limit` retry without operator intervention. `content_filter`
  is deliberately not retriable.
- **L4 — `rw.scratch_put` / `rw.scratch_ref` / `rw.scratch_get`** —
  content-addressed out-of-band storage at
  `.resilient_write/scratch/<sha256>.bin`. Identical bytes deduplicate;
  every call appends an `index.jsonl` row so labels become aliases.
  Accepts `utf-8` or `base64` encodings so raw binary material stays
  binary end-to-end. `scratch_get` is gated by
  `$RW_SCRATCH_DISABLE_GET` and re-hashes the bin on read to catch
  tampering. A non-fatal warning surfaces when `.resilient_write/` is
  not covered by the workspace's `.gitignore`.
- **L5 — `rw.handoff_write` / `rw.handoff_read`** — `HANDOFF.md` with
  YAML front-matter + Markdown body. Validates required fields (`task_id`,
  `status`, `agent`, `summary`, `next_steps`, `last_good_state`) and the
  status enum. Writes atomically through `safe_write`. Reports drift
  warnings for every `last_good_state` entry whose current SHA-256
  disagrees with the recorded one. Optional archive copies the previous
  envelope to `.resilient_write/handoffs/<ts>_<task_id>.md` before
  overwriting.
- **Workspace path safety** — every user path is resolved against the
  configured workspace root and rejected if absolute, empty, or if it
  escapes the root via `..`. Result: `policy_violation`/`permission`.
- **MCP surface** — 14 tools registered under the `rw.*` namespace with
  explicit JSON Schemas. Dispatch adapter catches `ResilientWriteError`
  and returns the L3 envelope as the tool response.

### Documentation
- `docs/ARCHITECTURE.md` — per-layer deep dive
- `docs/API.md` — tool schemas and examples
- `docs/POLICY.md` — default L0 pattern families and thresholds
- `docs/HANDOFF_SCHEMA.md` — L5 envelope specification
- `docs/ERRORS.md` — per-`reason_hint` handling guide for clients
- `docs/INSTALL.md` — MCP client config snippets (Claude Code, Cursor,
  Codex CLI, Copilot CLI, OpenCode)

### Tests
- 134 passing tests across `test_safe_write.py`, `test_journal.py`,
  `test_handoff.py`, `test_risk_score.py`, `test_chunks.py`,
  `test_scratchpad.py`, `test_errors.py`, `test_server_dispatch.py`,
  `test_scaffold.py`. Every layer's failure envelope is validated
  end-to-end against `spec/errors.schema.json` through the MCP dispatch
  adapter.

### Known non-goals (see `.agent/memory/decisions.md`)
- No write-ahead log across files. One file at a time is atomic;
  cross-file consistency is out of scope.
- No multi-process concurrency handling. One MCP client per workspace
  is the assumed deployment.
- No encryption at rest. Filesystem-level encryption is the host OS's
  job.
- No network transport. This is a local stdio MCP process.

[Unreleased]: https://github.com/jayluxferro/resilient-write/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jayluxferro/resilient-write/releases/tag/v0.1.0
