"""S4 setup: create files that a prior agent worked on (for drift detection)."""

from pathlib import Path


def setup(workspace: Path) -> dict:
    # Files from a "prior session"
    (workspace / "draft.tex").write_text(r"""\documentclass{article}
\begin{document}
\section{Methods}
Our approach uses a six-layer architecture for resilient writes.
Each layer addresses a specific failure mode observed in production
coding agents.

\end{document}
""")
    return {"ok": True, "files": ["draft.tex"]}
