# Build a Report in Chunks

You need to produce a three-section report in `report.md`:

- Section 1: "Introduction" — 2-3 paragraphs about resilient file I/O
- Section 2: "Architecture" — 2-3 paragraphs about the layered design
- Section 3: "Conclusion" — 1-2 paragraphs summarizing

## Instructions

1. Write each section as a separate chunk using `rw.chunk_append` (session: "report").
2. After writing all 3 chunks, verify with `rw.chunk_status`.
3. Preview with `rw.chunk_preview`.
4. Compose to `report.md` with `rw.chunk_compose`.

## Simulated failure

One of your `rw.chunk_append` calls will FAIL with a structured error.
When this happens:
- Read the error envelope carefully.
- Use `rw.chunk_status` to check what's on disk.
- Re-submit only the failed chunk.

## Expected output

The SHA-256 of the final composed `report.md`.
