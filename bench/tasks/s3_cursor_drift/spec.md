# Resilient File Pagination with Drift

The file `data.jsonl` contains 1,000 JSON records. You need to read all of them
and count how many have `id` values that are even numbers.

## Important

The file may be modified by another process while you're reading it. If you
create a cursor with `rr.make_cursor` and the file changes before you call
`rr.read_next`, the cursor will be rejected with a `stale_precondition` error.

## Instructions

1. Use `rr.stat` to check the file.
2. Create a cursor with `rr.make_cursor`.
3. Read chunks with `rr.read_next`.
4. If you get a `stale_precondition` error: re-create the cursor from the
   current position and continue. Do NOT start over from the beginning.
5. Continue until EOF.

## Expected output

```
Total records: N
Even ID count: N
```
