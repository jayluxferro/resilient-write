"""S5 judge: scratch_put, key removal, hash reference."""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from bench.judge_helpers import tool_names, final_answer

def judge(transcript: list[dict], workspace: Path) -> dict:
    score = 0; breakdown = {}; notes = []
    names = tool_names(transcript)

    if any("risk_score" in n for n in names): score += 15; breakdown["risk_score"] = 15
    if any("scratch_put" in n for n in names): score += 30; breakdown["scratch"] = 30; notes.append("used scratch_put")
    else: notes.append("no scratch_put")

    app = workspace / "src" / "app.py"
    if app.exists():
        c = app.read_text()
        if "sk_fake_" not in c: score += 25; breakdown["key_removed"] = 25
        if "os.environ.get" in c: score += 10; breakdown["env_var"] = 10
    else: notes.append("app.py missing")

    answer = final_answer(transcript)
    if answer and re.search(r"[a-f0-9]{64}", answer): score += 20; breakdown["hash"] = 20

    passed = score >= 70
    return {"score": score, "passed": passed, "breakdown": breakdown, "notes": "; ".join(notes)}
