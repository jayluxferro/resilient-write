"""S3 setup: create a data file that will be modified mid-read to trigger drift."""

import time
from pathlib import Path


def setup(workspace: Path) -> dict:
    data = workspace / "data.jsonl"
    lines = [f'{{"id": {i}, "value": "record_{i}"}}\n' for i in range(1000)]
    data.write_text("".join(lines))
    return {"ok": True, "records": 1000}


def trigger_drift(workspace: Path) -> None:
    """Modify the file to simulate a concurrent write."""
    data = workspace / "data.jsonl"
    time.sleep(0.1)
    with open(data, "a") as f:
        f.write('{"id": 1000, "value": "injected_after_cursor"}\n')
