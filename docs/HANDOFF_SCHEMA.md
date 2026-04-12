# HANDOFF.md envelope schema

A handoff envelope is a Markdown file with a YAML front-matter block. It is
human-readable (so the user can read it) and machine-parseable (so the next
agent can load it structurally).

## File template

```markdown
---
task_id: llm-telemetry-report
status: partial                # complete | partial | blocked | handed_off
started_at: 2026-04-11T13:42:00Z
updated_at: 2026-04-11T17:28:04Z
agent: claude-opus-4-6
summary: |
  Built the 19-page report (report.tex) and 22-slide deck (presentation.tex).
  Payload appendix blocked on L0 classifier until sk-ant-* tokens were redacted.
next_steps:
  - Run latexmk -pdf -shell-escape report.tex to pick up new cross-refs.
  - Spot-check page 12 risk matrix formatting.
context_hints:
  - minted requires -shell-escape and two pdflatex passes.
  - TeXLive 2022 needs a stub for \tikz@deactivatthings.
  - \mono{} is url-based and CANNOT appear in captions or section titles.
last_good_state:
  - path: report.tex
    sha256: 4b0c12ea...
    bytes: 50347
  - path: presentation.tex
    sha256: 9ea1fe43...
    bytes: 20462
  - path: macros.tex
    sha256: a1236f09...
    bytes: 4866
open_questions: []
blockers:
  - rw.safe_write returned {error: "blocked", reason_hint: "content_filter"}
    when appendix included sk-ant-oat01-…; resolved by redacting to {REDACTED}
    and retrying.
artifacts:
  - kind: pdf
    path: report.pdf
    sha256: 3fe9...
  - kind: pdf
    path: presentation.pdf
    sha256: 77a2...
---

# Handoff: LLM-CLI telemetry report

<free-form prose section — optional — the agent can write a short README here
for human readers. The YAML front-matter is the machine contract.>
```

## Fields

| Field | Type | Required | Purpose |
|---|---|---|---|
| `task_id` | string | yes | Stable identifier for the task; used to dedupe envelopes |
| `status` | enum | yes | `complete` / `partial` / `blocked` / `handed_off` |
| `started_at` | ISO-8601 | no | When the task began |
| `updated_at` | ISO-8601 | yes | When this envelope was written |
| `agent` | string | yes | Model / agent id that wrote the envelope |
| `summary` | multiline string | yes | One-paragraph human-readable status |
| `next_steps` | list of strings | yes (may be empty) | Concrete actions the next agent should take |
| `context_hints` | list of strings | no | Non-obvious facts learned during the run (build flags, version quirks, etc.) |
| `last_good_state` | list of `{path, sha256, bytes}` | yes | Content-addressed snapshot of files the next agent can trust |
| `open_questions` | list of strings | no | Things the agent deferred to a human |
| `blockers` | list of strings | no | Why the task is not yet complete |
| `artifacts` | list of `{kind, path, sha256}` | no | Final outputs (PDFs, binaries, etc.) |

## Rules

1. **Atomic.** `rw.handoff_write` uses `safe_write` internally so the envelope is either fully written or not at all.
2. **Idempotent per `task_id`.** A second call with the same `task_id` replaces the previous envelope but keeps history in `.resilient_write/handoffs/<timestamp>_<task_id>.md` if the `archive` flag is set.
3. **Free prose is optional but encouraged.** The YAML front-matter is the machine contract; the Markdown body is for humans.
4. **No secrets in the envelope.** The `blockers` and `context_hints` fields get passed through `rw.risk_score` before writing; if they trip L0, the write is rejected.
5. **`last_good_state` is authoritative.** A resuming agent should treat those hashes as ground truth and refuse to proceed if any file on disk no longer matches (this is almost always a sign that something else edited the file between sessions).

## Minimal example (complete task)

```markdown
---
task_id: rebuild-report-figures
status: complete
updated_at: 2026-04-11T17:28:04Z
agent: claude-opus-4-6
summary: All 10 figures rebuilt; both PDFs compile cleanly.
next_steps: []
last_good_state:
  - path: report.pdf
    sha256: 3fe9...
  - path: presentation.pdf
    sha256: 77a2...
artifacts:
  - kind: pdf
    path: report.pdf
    sha256: 3fe9...
  - kind: pdf
    path: presentation.pdf
    sha256: 77a2...
---

# Handoff: rebuild report figures

All figures regenerated and both PDFs verified. No follow-up needed.
```

## Minimal example (blocked task)

```markdown
---
task_id: draft-payload-appendix
status: blocked
updated_at: 2026-04-11T15:40:10Z
agent: claude-opus-4-6
summary: Evidence appendix is blocked on L0 classifier due to raw credential strings in drafts.
next_steps:
  - Redact all sk-ant-oat01-* and gho_* matches to {REDACTED}.
  - Retry via rw.chunk_write so only the failing chunk needs to re-send.
context_hints:
  - L0 thresholds are at the default; consider tightening for this repo.
last_good_state:
  - path: report.tex
    sha256: 4b0c...
blockers:
  - rw.safe_write returned error=blocked reason_hint=content_filter
    (detected: api_key, github_pat) on three retries.
---
```
