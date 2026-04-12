# AGENT.md — briefing for the next agent working on this project

You are picking up work on an MCP server called **resilient-write**. This
document is the handoff: the spec is frozen, some code has been planned but
not written, and your first task is to start building or to keep iterating
on the design if the user directs you to.

Read this file first. Then read the files under `docs/` (which are the frozen
spec). Then read the two memory files under `.agent/memory/` — they capture
things that are important but not part of the spec: the origin story, the
real failure mode that motivated the project, the design decisions that were
considered and rejected, and the current status.

## What this project is

`resilient-write` is an MCP server that provides a durable, fault-tolerant
write surface for coding agents. It exists because today's coding agents
(Claude Code, Cursor, Codex, Copilot) all share one failure mode: when a
tool call is silently rejected by a content filter or a transport error,
the agent loses the content it was trying to write, has no structured
error to reason about, and usually thrashes through several retries before
either recovering by luck or giving up.

The project is organised as **six composable layers** (see
`docs/ARCHITECTURE.md` for the full breakdown). Each layer is one MCP tool,
each layer owns one concern, and layers are adoption-independent — you can
install L1 alone and still get 80% of the benefit.

| Layer | Tool | Concern |
|---|---|---|
| L0 | `rw.risk_score`  | Pre-flight content classifier (will this write fail?) |
| L1 | `rw.safe_write`  | Transactional write: temp file + hash verify + atomic rename + journal |
| L2 | `rw.chunk_write` / `rw.chunk_compose` | Resume-safe chunked writes for oversized payloads |
| L3 | `rw.typed_error` (schema) | Structured error envelope agents can reason about |
| L4 | `rw.scratchpad`  | Out-of-band storage for raw secrets / binaries / PII |
| L5 | `rw.handoff`     | Task continuity envelope (`HANDOFF.md`) for cross-session resumption |

## Current status

**Spec**: frozen.
**Code**: not started. No `src/`, no tests.
**Docs**: complete. Every layer has a spec file. Scenarios and schemas are written.

The MVP you should build first is **L1 + L5** (safe_write + handoff envelope).
That is the smallest install that gives a real reliability win and can be
adopted by existing agent configurations without breaking anything. L0 can
come next because it's standalone and easy to iterate on. L2 / L3 / L4 build
on top once L1 is rock-solid.

## Your first 30 minutes

1. Read `README.md` — the user-facing overview, planned repo layout, install steps.
2. Read `docs/ARCHITECTURE.md` — design principles and per-layer deep dive.
3. Read `docs/API.md` — exact input/output schemas for every tool.
4. Skim `docs/SCENARIOS.md` — the real failure modes this project is built for.
   Each scenario shows "what happened, what went wrong, how each layer catches it".
5. Skim `docs/POLICY.md` — default regex patterns for the L0 classifier.
6. Read `.agent/memory/origin.md` — why this project exists and what happened
   the first time the failure mode was hit in the wild (the LLM CLI telemetry
   report). This gives you the stakes and the vocabulary.
7. Read `.agent/memory/decisions.md` — what was considered and rejected, so
   you don't re-debate settled points.
8. Read `.agent/memory/next-steps.md` — the concrete next task list.

## Hard rules

- **Do not change the spec without explicit user agreement.** The layer
  boundaries, the JSON schemas, and the policy defaults are the contract.
- **No layer may depend on a layer above it.** L1 must work without L0.
  L5 must work without L2. This is why the architecture is deliberately
  flat and the layers are orthogonal.
- **Everything goes in `.resilient_write/` in the consumer workspace.**
  Journal, chunks, scratchpad, policy, handoff archives. One directory.
  Gitignored by default.
- **Safe_write is atomic per-file on POSIX.** If you find yourself reaching
  for flocks, fsync, or a WAL across files, you are solving a harder
  problem than this project is meant to solve. Re-read `docs/ARCHITECTURE.md`
  "Design principles" section.
- **Failure responses are always typed (L3 schema).** Never return free-text
  error strings. If an error doesn't fit the enum, add a new enum value and
  document it.

## What this project is NOT

- **Not a WAL across files.** One file at a time is atomic; cross-file
  consistency is out of scope.
- **Not a backend service.** It runs as a local MCP stdio process,
  keyed to a workspace directory.
- **Not a filter-bypass tool.** L0 redacts aggressively by default to make
  the filter's job easier, not to sneak around it.
- **Not a secrets manager.** The scratchpad (L4) is write-ahead audit
  storage, not a key vault. Anything at-rest is the host OS's job.
- **Not a replacement for the regular filesystem.** Every tool operates on
  plain files under plain paths. You can still `cat`, `grep`, `git diff`.

## How to talk to the user

The user's voice: direct, wants short answers, doesn't want preamble.
They value:
- Proof over promises ("I verified X by running Y, here's the output").
- Named trade-offs over marketing.
- Asking before making risky decisions.
- Honest "this was wrong, here's the correction" when things go wrong.

They dislike:
- "I'm excited to help!" energy.
- Unsolicited summaries at the end of every response.
- Speculative features added beyond what was asked.
- Being told what they already know.

When you start a session here, jump straight to: "Read AGENT.md, read docs,
here's what I'll do first, any objections?"

## Where the context came from

This project was spun out of a concrete failure observed while producing
an LLM CLI telemetry report in 2026-04. Writing the redacted payload
appendix triggered the Write tool to silently reject several drafts
because they contained token-shaped strings (`sk-ant-oat01-…`, `gho_…`).
The agent spent several tool calls retrying identical content before
finally working around the failure with a chunked `cat >> file.tex <<EOF`
hack. The user asked "what mechanism could we put in place to prevent
this next time?" — and this project is the answer.

The blog post that came out of that work is the most detailed worked
example of what each layer would catch if it had existed at the time. See
`.agent/memory/origin.md` for the full narrative.
