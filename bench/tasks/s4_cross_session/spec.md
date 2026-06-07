# Session Handoff — Part A

You are Agent A. Your task is to start writing a paper and hand off state
for the next agent.

## Instructions

1. Read `draft.tex` using `rr.read_bytes`.
2. Note its SHA-256 from `rr.stat`.
3. Write a HANDOFF.md using `rw.handoff_write` with this envelope:

```json
{
  "task_id": "paper-draft",
  "status": "partial",
  "agent": "agent-a",
  "summary": "Started the Methods section. Still need Results and Conclusion.",
  "next_steps": ["Add Results section", "Add Conclusion section", "Fix any drift"],
  "last_good_state": [
    {"path": "draft.tex", "sha256": "<SHA256 from rr.stat>"}
  ]
}
```

Include a body note explaining what remains to be done.

## Expected output

The handoff_path and SHA-256 of the written HANDOFF.md.

---

# Session Handoff — Part B

You are Agent B, picking up where Agent A left off.

## Instructions

1. Read the HANDOFF.md using `rw.handoff_read`.
2. Check the `drift_warnings` — if `draft.tex` changed, note it.
3. Continue the work: append a Results section and a Conclusion section
   to `draft.tex` using `rw.safe_write` with `mode=overwrite`.
4. Write a new HANDOFF.md with status "complete".

## Expected output

Report what you found in the handoff, any drift warnings, and the SHA-256
of the final `draft.tex`.
