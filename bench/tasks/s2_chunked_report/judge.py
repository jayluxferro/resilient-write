"""S2 judge: verify chunked workflow."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from bench.judge_helpers import tool_names, final_answer, errors_encountered

def judge(transcript: list[dict], workspace: Path) -> dict:
    score = 0; breakdown = {}; notes = []
    names = tool_names(transcript)

    appends = sum(1 for n in names if "chunk_append" in n)
    if appends >= 3: score += 20; breakdown["append"] = 20
    elif appends > 0: score += 10; breakdown["partial_append"] = 10
    else: notes.append("no chunk_append")
    notes.append(f"{appends} chunk_appends")

    if any("chunk_status" in n for n in names): score += 15; breakdown["status"] = 15
    if any("chunk_compose" in n for n in names): score += 15; breakdown["compose"] = 15
    else: notes.append("no compose")

    report = workspace / "report.md"
    if report.exists():
        score += 20; breakdown["output"] = 20
        if len(report.read_text()) > 200: score += 10; breakdown["substantial"] = 10
    else: notes.append("report.md missing")

    errs = errors_encountered(transcript)
    if errs and report.exists(): score += 20; breakdown["recovery"] = 20; notes.append(f"recovered from {len(errs)} errors")
    elif errs: notes.append(f"{len(errs)} errors, no recovery")
    else: score += 10; breakdown["smooth"] = 10

    passed = score >= 70
    return {"score": score, "passed": passed, "breakdown": breakdown, "notes": "; ".join(notes)}
