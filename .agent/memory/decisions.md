---
name: design decisions
description: What was considered, what was chosen, and what was rejected — so you don't re-debate settled points
type: project
---

# Decisions

This file captures the *why* behind the current design. If the user asks
you to change one of these decisions, read the "considered and rejected"
column first — most of them have already been debated.

## Overall shape

### Decision: six layers, flat and orthogonal

**Why**: each of the six layers catches exactly one of the six things
that went wrong in the motivating incident (see `.agent/memory/origin.md`).
Flat means any layer can be installed alone; orthogonal means layers
don't depend on each other.

**Considered and rejected**:
- *A single monolithic "safe_write_v2" tool that does everything.*
  Rejected because it couples the pre-flight classifier (L0), the
  write-transaction (L1), and the cross-session handoff (L5) into one
  surface. Agents that only want the atomic write shouldn't have to
  load the classifier or emit handoff envelopes.
- *Three layers instead of six.* Rejected because merging L1+L2 would
  force every write through a chunk session (overkill for 90% of writes),
  merging L0+L3 would make the classifier depend on the error envelope
  format, and merging L4+L5 would couple out-of-band storage to task
  continuity (different lifetimes, different consumers).
- *Adding a seventh layer for encryption at rest.* Rejected. Out of scope.
  That's the host OS's job; we store plaintext and let filesystem-level
  encryption handle the rest.

### Decision: MCP stdio server, workspace-local

**Why**: matches how every other coding agent tool already runs. No
daemon process, no persistent state between workspaces, no auth model to
manage. One process per invocation, keyed to `$RW_WORKSPACE` (default
`$PWD`).

**Considered and rejected**:
- *Long-lived daemon with a socket.* Rejected because it introduces
  PID-file and socket management for no real gain.
- *HTTP server instead of stdio.* Rejected because every MCP client can
  already speak stdio and the failure modes are simpler.
- *Cloud service.* Laughably rejected — the whole point is to never
  touch the network.

## State directory: `.resilient_write/`

### Decision: single top-level directory per workspace

**Why**: the journal (L1), chunk sessions (L2), scratchpad (L4), policy
(L0 config), and handoff archives (L5) all share the same lifetime as
the workspace. Colocating them in one `.gitignore`-able directory keeps
it easy to blow away or back up as a unit.

**Considered and rejected**:
- *Per-layer directories at the workspace root (`.rw-journal/`,
  `.rw-chunks/`, etc).* Rejected because five hidden top-level
  directories is worse than one.
- *Put state in `$XDG_STATE_HOME/resilient-write/<workspace-hash>/`.*
  Rejected because it hides state from the user who's trying to debug a
  write failure. In-workspace is more discoverable and is easier to
  reason about during git operations.

### Decision: append-only journal, one row per write

**Why**: a journal gives the agent a way to answer "what's the last
known-good state of this file?" without `git log`-ing every build. Rows
are timestamped, hashed, and include the caller tool name so cross-agent
investigation is possible.

**Considered and rejected**:
- *SQLite database*. Rejected because jsonlines is trivially diffable
  and greppable, and SQLite would add a dependency for a read pattern
  that is overwhelmingly append-only.
- *Compressed binary format.* Rejected because the journal is meant to
  be human-auditable; compression can be added later if size becomes an
  issue.

## L0 classifier

### Decision: regex + length heuristics only, no LLM

**Why**: must be deterministic and fast (< 50 ms for 100 KB) so it can
run on every write without friction. Calling an LLM on every write would
re-introduce the failure mode we're trying to prevent.

**Considered and rejected**:
- *Local small model (Llama-3.2-1B) as classifier.* Rejected because
  non-determinism makes it hard to debug "why did this get flagged?" and
  because it would add a ~500 MB dependency for a task regex handles
  well.
- *Statistical entropy detector.* Rejected because high-entropy strings
  are a subset of secrets; many base64 blobs are also legitimate
  payload. Prefix matching is more precise.

### Decision: workspace-local overrides via `.resilient_write/policy.yaml`

**Why**: different workspaces have different risk profiles. A security-
research repo legitimately needs to store test emails and redacted
tokens; a production service repo needs to flag every bearer.

**Considered and rejected**:
- *Central policy file per user*. Rejected because it leaks one repo's
  policy decisions into another.

## L1 safe_write

### Decision: temp-file-and-atomic-rename, not file locking

**Why**: POSIX `rename(2)` is atomic on the same filesystem. Locking
adds complexity and platform-specific quirks (Windows `LOCK_EX`, Linux
flock vs fcntl) and doesn't actually give us anything that atomic
rename doesn't.

**Considered and rejected**:
- *`flock(2)` around the whole write.* Rejected. Already-running
  processes don't respect it, so it's a false guarantee.
- *Copy-to-sidecar and leave both.* Rejected because that leaks state
  into the workspace and requires cleanup.

### Decision: SHA-256 content verification, not SHA-1 or BLAKE3

**Why**: SHA-256 is already everywhere, hash verification isn't on the
hot path (tens of KB per write, not GB), and the marginal perf of BLAKE3
doesn't justify another dependency. SHA-1 is too weak for the journal's
audit use case.

### Decision: `expected_prev_sha256` is optional, not mandatory

**Why**: most agent calls are "overwrite this file, I don't care what's
there". Requiring a prev-hash would force agents to read the file first,
which doubles the IO for the common case. Callers who care about
optimistic concurrency (e.g. two agents editing the same file) can opt in.

## L2 chunks

### Decision: chunk files live under `.resilient_write/chunks/<session>/`

**Why**: session is a first-class concept — "build report.tex body",
"collect long bash output", etc. Each session has a manifest, a file
count, and can be wiped in one `rw.chunk_reset` call.

### Decision: chunk numbering is 1-indexed

**Why**: matches how humans number sections. Less surprising for someone
reading `.resilient_write/chunks/my-session/part-001.txt`.

## L3 typed errors

### Decision: structured JSON envelope with enum `reason_hint`

**Why**: agents need to branch on the cause of failure. Free-text errors
force the agent to do string parsing on a non-contract surface, which
breaks when the upstream error message changes.

**Considered and rejected**:
- *HTTP-style status codes.* Rejected because HTTP status codes conflate
  transport failures with content failures with authorization failures.
  Our enum is more specific.

### Decision: `retry_budget` counts down per-call, not per-file

**Why**: if a write keeps failing for the same reason, more retries
won't help — the agent should escalate. Counting per-file would require
tracking state across calls, which complicates the server.

## L4 scratchpad

### Decision: content-addressed, keyed by SHA-256

**Why**: deduplication for free (same content = same path), no filename
conflicts, trivial integrity check.

**Considered and rejected**:
- *Human-readable filenames.* Rejected because the point of scratchpad
  is that you reference raw material by hash only from the main tree.
  Naming it gives you a back-channel to the content.

### Decision: `rw.scratch_get` can be disabled by workspace policy

**Why**: some workspaces want write-only scratchpad — you can store
audit material but never read it back. This is for high-sensitivity
setups where even the authoring agent shouldn't be able to re-surface
the raw content.

## L5 handoff

### Decision: single `HANDOFF.md` at workspace root, YAML front-matter

**Why**: simple, discoverable, human-readable, machine-parseable. One
file per task_id; subsequent writes with the same task_id replace (with
an optional archive).

**Considered and rejected**:
- *Multiple files under `.resilient_write/handoffs/`.* Rejected because
  the top-level `HANDOFF.md` is much more discoverable for a fresh agent
  that doesn't know the project layout yet.
- *JSON instead of YAML front-matter.* Rejected because the body of the
  envelope is meant to be human-readable Markdown, and YAML front-matter
  is the idiomatic way to combine the two.

### Decision: `last_good_state` is content-addressed (sha256 per file), not just path

**Why**: a resuming agent needs to know *not only* which files existed
at the last checkpoint, but also whether they've drifted since. Without
the hash, the agent can't tell that another process edited the file
between sessions.
