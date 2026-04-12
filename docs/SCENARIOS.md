# Scenarios — real failure modes this project is built for

Each scenario is drawn from an actual failure observed while building
the LLM-CLI telemetry report. The goal is to show concretely what each
layer catches and what the agent should do.

---

## Scenario 1 — Redacted-looking token trips a content filter

**What happened.** The agent was appending a payload sample to `report.tex`:

```tex
\begin{lstlisting}
authorization: Bearer sk-ant-oat01-QvUvUpr4f-LgPT0kejk4rWRd…
\end{lstlisting}
```

The `Write` tool silently rejected the payload. The error returned no hint
about *why*. The agent retried twice with the same content, then a third
time with a slightly different newline pattern, then gave up and re-did the
entire appendix from scratch in three `cat >> ... <<EOF` stages over Bash.

Total cost: ~5 failed tool calls, ~2 minutes of wall time, one near-miss on
losing half the appendix.

**How the layers catch it.**

| Step | Layer | Effect |
|---|---|---|
| 1 | L0 `rw.risk_score` | Run on the draft *before* the write. Returns `verdict: high`, `detected_patterns: [{kind: api_key, pattern: "sk-ant-", line: 412}]`, `suggested_actions: [{action: redact, targets: [api_key]}]`. |
| 2 | Agent | Sees the L0 verdict, runs a local regex substitution replacing the match with `sk-ant-oat01-{REDACTED}`. |
| 3 | L1 `rw.safe_write` | Write now succeeds; journal records the new hash. |
| 4 | — | Total time: < 5 seconds, no wasted tool calls. |

**Alternate path if the write still fails.**

If L0 missed the pattern and the sink still rejected the write, L3 would
return:

```json
{
  "error": "blocked",
  "reason_hint": "content_filter",
  "detected_patterns": ["api_key"],
  "suggested_action": "redact",
  "retry_budget": 2
}
```

…and the agent would branch on `suggested_action = redact` instead of
retrying identical content.

---

## Scenario 2 — Large heredoc truncated mid-file

**What happened.** A 90 KB `cat > report.tex <<EOF` was partially written.
The shell tool returned success but the file on disk was missing ~12 KB of
content at the end. `pdflatex` then failed with `! File ended while scanning
use of \@writefile.` — a confusing error that took a minute to diagnose.

**How the layers catch it.**

| Step | Layer | Effect |
|---|---|---|
| 1 | L1 `rw.safe_write` | Writes to `report.tex.tmp.<uuid>` first, re-reads, hashes, and compares against the sha256 of the original content. Mismatch → abort with `write_corruption`. |
| 2 | — | The original `report.tex` is untouched; the agent sees a clear error and can retry or chunk. |

For a file that is genuinely too big for a single write, Scenario 3 applies.

---

## Scenario 3 — File too large for one write, resume-safe build

**What happened.** The same appendix was split into three manual heredoc
stages because the agent inferred the first attempt had been truncated.
Each stage was `cat >>` to append. There was no explicit marker of "which
stages have succeeded", so if the second `cat >>` failed the agent would
have silently left the file with 1/3 of the content.

**How the layers catch it.**

| Step | Layer | Effect |
|---|---|---|
| 1 | L2 `rw.chunk_write` | Agent calls `rw.chunk_write(session="report_appendix", index=1, content=…, total_expected=3)`. Chunk saved to `.resilient_write/chunks/report_appendix/part-001.txt`. |
| 2 | L2 `rw.chunk_write` | index=2. Saved. |
| 3 | L2 `rw.chunk_write` | index=3. Saved. |
| 4 | L2 `rw.chunk_compose` | Concatenates all three chunks in order, verifies the set is contiguous, writes the final file via `safe_write`. Returns the final hash. |
| 5 | — | If chunk 2 had failed, chunks 1 and 3 would remain on disk and the agent would only retry chunk 2. `rw.chunk_compose` would refuse to run until the set is complete. |

**Net effect:** resumable progress. No "all-or-nothing" cliff.

---

## Scenario 4 — Raw credential evidence needs to be kept for audit, not for the tree

**What happened.** The telemetry report includes raw payload samples
containing real bearer tokens and GitHub PATs as observed on the wire.
The report body redacts them to `{REDACTED}`, but the un-redacted raw
material still lives in `EVIDENCE.md` inside the working tree — which
means it is indexed by the editor, backed up, potentially synced, and
eligible to be committed by accident.

**How the layers catch it.**

| Step | Layer | Effect |
|---|---|---|
| 1 | L4 `rw.scratch_put` | Store the raw payload in `.resilient_write/scratch/<sha256>.bin` with a label like `raw_event_logging_txn_2c1e80`. Returns the hash. |
| 2 | Agent | Writes the *redacted* version to `report.tex` body, referencing the scratch hash in a comment. |
| 3 | — | `.resilient_write/` is gitignored. The raw material never reaches version control, never appears in editor search, never indexes into an embedding store. |
| 4 | L3 | If someone (or another agent) later tries `rw.scratch_get` from a workspace with a policy that disallows `scratch_get`, the tool returns `policy_violation` and refuses. |

---

## Scenario 5 — Task interrupted, fresh agent resumes without re-deriving context

**What happened.** When the user later said "continue with it", the assistant
had to re-discover the state of the working tree by re-reading every file and
re-inferring which sections were already stable. That was wasted effort.

**How the layers catch it.**

| Step | Layer | Effect |
|---|---|---|
| 1 | L5 `rw.handoff_write` | Agent emits a `HANDOFF.md` envelope listing `last_good_state` hashes for `report.tex`, `macros.tex`, each figure, plus `next_steps` and `context_hints`. |
| 2 | — | Agent session ends or is interrupted. |
| 3 | L5 `rw.handoff_read` | Fresh agent (or same agent in a new session) reads `HANDOFF.md`. It knows immediately which files are trustworthy, what's still open, and which gotchas to avoid (e.g. `minted needs -shell-escape`). |
| 4 | L1 `rw.safe_write` (guard) | Before editing any file listed in `last_good_state`, the fresh agent verifies the on-disk hash matches. If not, it flags a drift warning instead of blindly editing. |

**Net effect:** zero-context resumption. No rework.

---

## Scenario 6 — Agent thrashes retrying identical content

**What happened.** In multiple sessions across different tasks, an agent has
retried the same rejected write 3–5 times in a row with no change to the
payload. This burns tokens and wall time.

**How the layers catch it.**

| Step | Layer | Effect |
|---|---|---|
| 1 | L3 `rw.typed_error` | Each failure returns `retry_budget: N`. Budget starts at 2–3 and decrements. |
| 2 | — | After `retry_budget == 0`, the tool refuses further identical attempts with `error: quota_exceeded`. The agent is forced to change tactic. |
| 3 | L5 `rw.handoff_write` | If the agent has no further ideas, it emits a handoff envelope with `status: blocked` instead of entering a retry loop. |

---

## Non-scenarios

Things this project deliberately does **not** try to solve:

- **Bypassing legitimate content filters.** If a content filter is blocking
  a genuine secret, the agent should *redact* or *escalate*, not route around.
  L0's default patterns are aggressive precisely so redaction happens first.

- **Guaranteed delivery across crashes.** This is a local durability layer,
  not a distributed consensus protocol. `safe_write` is atomic per-file on
  POSIX, but there is no write-ahead log across files.

- **Encryption of scratchpad contents.** Scratchpad is plaintext by default
  under `.resilient_write/scratch/`. If the workspace needs at-rest encryption,
  use filesystem-level encryption or an OS keychain; don't reinvent it here.

- **Replacing the host filesystem.** All tools operate on normal files under
  normal paths. `safe_write` is a thin wrapper; you can still `cat`, `grep`,
  `git diff` exactly as before. The only new thing is `.resilient_write/`.
