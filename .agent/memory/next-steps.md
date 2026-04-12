---
name: next steps
description: Concrete prioritised task list for building this out, in order
type: project
---

# Next steps

## Status as of 2026-04-11

- Spec: frozen (`README.md`, `docs/*`)
- Code: not started
- Tests: not started

The user's direction was: *"after the blog post and presentation are
done, spin up a fresh agent in this directory and continue."* That time
is now. You are the fresh agent.

## Stage 0 — orient (first 15 minutes)

1. Read `AGENT.md` at the repo root.
2. Read `README.md`, `docs/ARCHITECTURE.md`, `docs/API.md`.
3. Skim `docs/SCENARIOS.md` and `docs/POLICY.md`.
4. Read `.agent/memory/origin.md` and `.agent/memory/decisions.md`.
5. Confirm with the user what the first stage should be before writing
   any code. Do not start coding unprompted.

## Stage 1 — MVP: L1 + L5

**Rationale**: `rw.safe_write` and `rw.handoff_write` are the smallest
useful pair. L1 delivers transactional writes (80% of the value). L5
delivers cross-session continuity (the other 20%). You can ship the
MVP with ~400 lines of Python and it's immediately usable by any MCP
client.

### Tasks

1. Scaffold the Python project.
   - `pyproject.toml` with `mcp` (the Python MCP SDK) and `pyyaml` as
     the only runtime deps. No `anthropic`, no `openai`.
   - `src/resilient_write/__init__.py`, `src/resilient_write/server.py`.
   - `uv` or `hatch` as the build backend. User's preference not known
     — ask before committing to one.

2. Implement `rw.safe_write` (L1).
   - Path validation (must be relative to `$RW_WORKSPACE`; reject `..`).
   - Tmp file creation with `tempfile.NamedTemporaryFile(dir=parent,
     delete=False)`.
   - Write content, flush, fsync, close.
   - Read back, hash, compare to expected.
   - `os.replace(tmp, path)` (atomic on POSIX; Python's stdlib handles
     Windows semantics).
   - Append a row to `.resilient_write/journal.jsonl` with timestamp,
     path, sha256, bytes, mode, caller (from MCP client info).
   - Return the journal entry on success.
   - Raise the L3 typed error on failure.

3. Implement `rw.handoff_write` and `rw.handoff_read` (L5).
   - YAML front-matter + markdown body.
   - Schema validation against `docs/HANDOFF_SCHEMA.md`.
   - Validate that every `last_good_state` file actually exists and
     matches its recorded hash, or return a warning (not an error).
   - Accept `archive: true` to copy the previous envelope to
     `.resilient_write/handoffs/<timestamp>_<task_id>.md` before
     overwriting.

4. Implement `rw.journal_tail` (inspection helper).
   - Returns the last N rows from `journal.jsonl`.
   - Optional filter by path or mode.

5. Write a minimal test harness.
   - `pytest` with tempdir fixtures.
   - Per-layer tests: `tests/test_safe_write.py`, `tests/test_handoff.py`,
     `tests/test_journal.py`.
   - Integration tests that exercise the MCP server end-to-end against
     a stub client.

6. Write `docs/INSTALL.md` update with the actual command once installable.

7. Publish to PyPI under the `resilient-write` name. Coordinate with the
   user — they may have a preferred namespace (e.g. `sperixlabs-resilient-write`).

## Stage 2 — L0 pre-flight classifier

Should be buildable in a couple of hours once L1 is stable.

### Tasks

1. Implement `rw.risk_score`.
   - Regex pattern table from `docs/POLICY.md`.
   - Length heuristics (total bytes, longest line, line count).
   - Scoring function per `docs/POLICY.md` weights.
   - Return the typed result with `verdict`, `detected_patterns`,
     `suggested_actions`.

2. Add workspace policy loader.
   - Read `.resilient_write/policy.yaml` if present.
   - Merge `extend_patterns` into defaults.
   - Apply `disable_families`.
   - Apply `thresholds` overrides.

3. Wire L0 as a hook in L1: if a caller passes `classify: true` to
   `rw.safe_write`, run the classifier first and reject the write with
   a typed error if the verdict exceeds a threshold.

4. Add unit tests for every regex pattern using the redacted-but-shaped
   strings from `docs/POLICY.md`.

## Stage 3 — L2 chunks

### Tasks

1. Implement `rw.chunk_write(session, index, content, total_expected)`.
   - Validate index is 1-based and ≤ total_expected.
   - Write via `rw.safe_write` to `.resilient_write/chunks/<session>/part-NNN.txt`.
   - Update or create `.resilient_write/chunks/<session>/manifest.json`.

2. Implement `rw.chunk_compose(session, output_path, separator, cleanup)`.
   - Verify the set is contiguous.
   - Concatenate.
   - Write final via `rw.safe_write`.
   - Return final hash + list of chunk hashes.
   - Optionally wipe chunk dir on success.

3. Implement `rw.chunk_reset(session)` — destructive, used when the agent
   wants to start a session over.

4. Tests for resumability: write chunk 1, write chunk 2, simulate
   chunk 3 failing, verify chunks 1 and 2 survive, retry chunk 3, verify
   compose succeeds.

## Stage 4 — L4 scratchpad

### Tasks

1. Implement `rw.scratch_put(content, label, content_type)`.
   - Hash content.
   - Write to `.resilient_write/scratch/<sha256>.bin` via `rw.safe_write`.
   - Append to `index.jsonl`.
   - Return `{sha256, scratch_path, bytes}`.

2. Implement `rw.scratch_ref(sha256 | label)`.
   - Look up index entry without returning content.

3. Implement `rw.scratch_get(sha256)`.
   - Gated by `$RW_SCRATCH_DISABLE_GET`. If set, return a
     `policy_violation` typed error.
   - Otherwise return the raw bytes.

4. Add `.gitignore` check: if `.resilient_write/` is not in the
   workspace's `.gitignore`, emit a warning on first use (but do not
   fail).

## Stage 5 — L3 typed error formalisation

L3 exists in `docs/API.md` but once the code is real it deserves its own
Pydantic model (or `msgspec.Struct`) and a JSON schema validator so
callers can parse errors programmatically.

### Tasks

1. `src/resilient_write/errors.py` — typed error classes + JSON serialiser.
2. `spec/errors.schema.json` — public JSON Schema.
3. Update every layer to raise these instead of plain exceptions.
4. Add docs on how an MCP client should handle each `reason_hint`.

## Stage 6 — polish

1. `docs/INSTALL.md` — real install instructions.
2. MCP config snippets for Claude Code, Cursor, Codex CLI, Copilot CLI,
   and OpenCode.
3. A `CHANGELOG.md`.
4. CI (GitHub Actions) running pytest on Linux + macOS.
5. Publish.

## Things you might get tempted to do that you should not

- **Don't add a config DSL beyond policy.yaml.** Environment variables
  for paths, YAML for patterns. That's the full config surface.
- **Don't make `rw.safe_write` aware of content type.** It's a byte
  shuffler. Let L0 and L4 do type-dependent logic.
- **Don't add a "backup" or "history" feature to L1.** That's L5's job
  (via `last_good_state`).
- **Don't build a web UI.** If the user asks for one, push back:
  `journal_tail` + `cat HANDOFF.md` is the UI.
- **Don't try to handle multi-process concurrency.** One MCP client per
  workspace at a time is the assumption. Two concurrent agents writing
  the same file is a *user bug*, not our problem to solve.

## When to push back on scope

If the user asks you to:

- **"Add encryption at rest"** → no; filesystem-level encryption is the
  right layer.
- **"Make it work across a network"** → no; that's a different project
  (see `docs/ARCHITECTURE.md` "What this project is NOT").
- **"Auto-retry with different content"** → no; the agent decides the
  retry strategy, not the server. The server just reports typed errors.
- **"Make it a daemon"** → no; stdio MCP is the model, see
  `.agent/memory/decisions.md`.

If the user asks you to:

- **"Change the journal format"** → ok, just update the schema version
  and write a migration note.
- **"Add a new error `reason_hint`"** → ok, add to the enum, document it
  in `docs/API.md`, bump the schema version.
- **"Add an additional L0 pattern family"** → ok, add to `docs/POLICY.md`
  defaults AND make sure workspace overrides still work.
