"""S3 judge: cursor drift detection and recovery."""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from bench.judge_helpers import tool_names, final_answer, errors_encountered

def judge(transcript: list[dict], workspace: Path) -> dict:
    score = 0; breakdown = {}; notes = []
    names = tool_names(transcript)

    if "rr_make_cursor" in names: score += 20; breakdown["cursor"] = 20
    if "rr_read_next" in names: score += 20; breakdown["read_next"] = 20

    errs = errors_encountered(transcript)
    had_drift = any(e.get("reason_hint") == "file_changed" or "file_changed" in str(e) for e in errs)
    if had_drift: score += 30; breakdown["drift_recovery"] = 30; notes.append("detected+recovered drift")
    elif errs: notes.append(f"errors but no drift: {errs}")
    else: score += 15; breakdown["no_drift"] = 15

    answer = final_answer(transcript)
    if answer:
        total_m = re.search(r"Total records:\s*(\d+)", answer)
        even_m = re.search(r"Even ID count:\s*(\d+)", answer)
        exp_total = 1001
        if total_m and int(total_m.group(1)) == exp_total: score += 15
        if even_m and int(even_m.group(1)) == sum(1 for i in range(exp_total) if i%2==0): score += 15

    passed = score >= 70
    return {"score": score, "passed": passed, "breakdown": breakdown, "notes": "; ".join(notes)}
