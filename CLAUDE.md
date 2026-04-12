# Resilient Write — Agent Instructions

This workspace has the `resilient-write` MCP server active. **Always prefer
`rw.*` tools over raw `Write`/`Edit` file operations** for any file creation
or large-scale content generation.

## When to use each tool

| Task | Tool |
|---|---|
| Create or overwrite a file | `rw.safe_write` (mode=create or overwrite) |
| Append to a file | `rw.safe_write` (mode=append) |
| Write a large file (>5 KB) | `rw.chunk_append` → `rw.chunk_compose` |
| Check content for secrets/risk | `rw.risk_score` |
| Validate syntax before writing | `rw.validate` |
| Preview chunked content | `rw.chunk_preview` |
| Store sensitive material | `rw.scratch_put` |
| Save session state for handoff | `rw.handoff_write` |
| Inspect write history | `rw.journal_tail` or `rw.analytics` |

## Chunked writing protocol

For any file larger than ~5 KB or with multiple logical sections:

1. Use `rw.chunk_append` for each section (auto-increments index)
2. Call `rw.chunk_preview` to verify the concatenated result
3. Optionally run `rw.validate` on the preview content
4. Call `rw.chunk_compose` with `cleanup=true` to write the final file

## Why this matters

Raw `Write` tool calls can fail silently when content triggers safety
filters, exceeds size limits, or contains token-shaped strings. The
`rw.*` tools provide:

- **Pre-flight risk scoring** to avoid filter rejections
- **Atomic writes** with hash verification to prevent corruption
- **Structured errors** so you can branch on failure type
- **Resume-safe chunks** so partial progress is never lost
- **Audit journal** for every write operation

## Quick reference

- Risk check before writing: `rw.risk_score` → check verdict
- If verdict is "high": redact detected patterns, then write
- If a write fails: read the error envelope's `suggested_action`
- Never retry identical rejected content — change the content first
- Use `rw.handoff_write` before session end if work is incomplete
