# Resilient Write — Future Work

From the research paper (arXiv:2604.10842, §6.2 Future Directions).

---

## 1. Cross-File Write-Ahead Logging (WAL)

**Paper reference:** "Cross-file write-ahead logging would enable true workspace-level transactions."

**Problem:** If an agent writes files A, B, C in sequence and crashes between B and C, there is no mechanism to roll back to a consistent state. Each `safe_write` is individually atomic, but the *set* of writes is not.

**Proposed design:**
- New layer (L1.5) between transactional writes and chunked composition
- `rw.tx_begin()` → returns a transaction ID
- `rw.tx_write(tx_id, path, content)` → writes to a staging area, not the final path
- `rw.tx_commit(tx_id)` → atomically moves all staged files to their final paths
- `rw.tx_rollback(tx_id)` → discards all staged files
- WAL stored in `.resilient_write/wal/<tx_id>/` with a manifest tracking intended operations
- On server restart, incomplete transactions are auto-rolled back

**Scope:** Large — new layer, new storage structure, crash recovery logic.

**Status:** Not started

---

## 2. Embedding-Based Risk Scoring for L0

**Paper reference:** "Integrating L0 with a lightweight embedding model could improve recall on obfuscated secrets without sacrificing the latency budget."

**Problem:** The current L0 classifier uses deterministic regex + size heuristics. It catches common secret formats (API keys, JWTs, PEM blocks, AWS keys) but misses obfuscated or novel patterns — e.g., base64-wrapped credentials, non-English PII, or secrets with inserted whitespace.

**Proposed design:**
- Add an optional embedding-based classifier alongside the regex classifier
- Use a small, local model (e.g., sentence-transformers or ONNX runtime) to compute similarity against known secret embeddings
- Hard latency budget: <100ms for 100KB (current regex is <50ms)
- Gated behind `$RW_EMBEDDING_MODEL` env var — when unset, falls back to regex-only
- Results merged with regex hits: embedding matches add to the score but don't replace regex patterns
- Policy file (`policy.yaml`) gains an `embedding` section for threshold tuning

**Scope:** Medium — requires adding an ML dependency, careful latency benchmarking, and a curated embedding dataset of secret patterns.

**Status:** Not started

---

## 3. Handoff Dependency Graph

**Paper reference:** "The handoff envelope (L5) could be extended with a machine-readable dependency graph, enabling orchestrators to schedule resumption tasks automatically."

**Problem:** The current handoff envelope (`HANDOFF.md`) has flat `next_steps` (a list of strings) and `last_good_state` (a list of path+hash pairs). An orchestrator reading this can see *what* to do next but not *in what order* or *what depends on what*.

**Proposed design:**
- Extend the handoff schema with an optional `dependency_graph` field:
  ```yaml
  dependency_graph:
    - id: "compile-latex"
      depends_on: []
      description: "Compile paper/main.tex"
    - id: "run-benchmarks"
      depends_on: []
      description: "Re-run Table 2 benchmarks"
    - id: "update-figures"
      depends_on: ["run-benchmarks"]
      description: "Regenerate fig_failure_comparison.pdf from new data"
    - id: "final-review"
      depends_on: ["compile-latex", "update-figures"]
      description: "Proofread and submit"
  ```
- `rw.handoff_write` validates the graph (no cycles, all `depends_on` refs exist)
- `rw.handoff_read` returns a topological sort of ready tasks (those with all deps satisfied)
- Backward-compatible: graph is optional, existing envelopes without it still work

**Scope:** Small-medium — schema extension, topological sort, cycle detection. No new storage layer.

**Status:** Not started
