"""S0 setup: generate a server log with embedded error patterns."""

import random
from pathlib import Path

ERROR_PATTERNS = [
    "ConnectionTimeoutError",
    "NullPointerException",
    "DiskFullError",
    "OOMError",
    "Segfault",
]

def setup(workspace: Path, scale: int = 1) -> dict:
    """scale=1 → ~550KB (10K lines), scale=10 → ~5.5MB (100K lines)."""
    n_lines = 10_000 * scale
    log = workspace / "server.log"
    lines = []
    for i in range(n_lines):
        if random.random() < 0.02:
            err = random.choice(ERROR_PATTERNS)
            lines.append(f"[{i:06d}] ERROR {err}: something went wrong at iteration {i}\n")
        else:
            lines.append(f"[{i:06d}] INFO  Request processed successfully in {random.randint(1, 500)}ms\n")
    log.write_text("".join(lines))
    return {"ok": True, "log_size": log.stat().st_size, "log_lines": n_lines, "scale": scale}
