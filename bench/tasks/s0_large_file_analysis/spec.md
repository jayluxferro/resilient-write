# Large-File Error Analysis

The file `server.log` contains 100,000 lines of server logs (~5MB).
It is too large to read in a single call without exceeding context limits.

## Your task

1. Use chunked reads to scan the entire log file.
2. Find ALL unique error types (lines containing "ERROR").
3. Count how many times each error type appears.
4. Report your findings.

## Expected output format

```
Error type: ConnectionTimeoutError — count: N
Error type: NullPointerException — count: N
Error type: DiskFullError — count: N
Error type: OOMError — count: N
Error type: Segfault — count: N
Total ERROR lines: N
```

## Constraints

- The file is ~5MB. Do NOT try to read it all at once.
- Use `rr.stat` first to check the file size.
- Use `rr.read_bytes` or `rr.make_cursor` + `rr.read_next` for chunked iteration.
- You may also use `rr.search_then_page` to find error lines.
