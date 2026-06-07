"""S2 setup: nothing to pre-create — the model builds the report from scratch."""

from pathlib import Path


def setup(workspace: Path) -> dict:
    return {"ok": True, "note": "no initial files needed"}
