"""S4 judge: handoff write/read + drift."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from bench.judge_helpers import tool_names, final_answer

def judge(transcript: list[dict], workspace: Path) -> dict:
    score = 0; breakdown = {}; notes = []
    names = tool_names(transcript)

    if any("handoff_write" in n for n in names): score += 25; breakdown["write"] = 25
    if any("handoff_read" in n for n in names): score += 25; breakdown["read"] = 25

    answer = final_answer(transcript)
    if answer and "drift" in answer.lower(): score += 20; breakdown["drift"] = 20
    elif answer: score += 10

    if (workspace / "HANDOFF.md").exists(): score += 15; breakdown["handoff_exists"] = 15
    if (workspace / "draft.tex").exists():
        c = (workspace / "draft.tex").read_text()
        if "Results" in c and "Conclusion" in c: score += 15

    passed = score >= 70
    return {"score": score, "passed": passed, "breakdown": breakdown, "notes": "; ".join(notes)}
