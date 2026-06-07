"""S0 judge: validate chunked-read approach and error-count accuracy."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from bench.judge_helpers import tool_names, final_answer, errors_encountered, count_tool_calls


def judge(transcript: list[dict], workspace: Path) -> dict:
    score = 0
    breakdown = {}
    notes = []
    names = tool_names(transcript)

    # C1: Checked file size (0-15 pts)
    if "rr_stat" in names:
        score += 15
        breakdown["checked_file_size"] = 15
        notes.append("checked file size with rr.stat")
    else:
        notes.append("did NOT check file size")

    # C2: Used chunked reads (0-20 pts)
    chunked = any(n in ("rr_read_bytes", "rr_make_cursor", "rr_read_next") for n in names)
    searched = any(n == "rr_search_then_page" for n in names)
    if chunked:
        score += 20
        breakdown["used_chunked_reads"] = 20
        notes.append("used chunked reads")
    elif searched:
        score += 12
        breakdown["used_search_only"] = 12
        notes.append("used search instead of chunked reads — partial credit")
    else:
        notes.append("did NOT use any read tool")

    # C3: Correct error counts (0-40 pts)
    answer = final_answer(transcript)
    if not answer:
        notes.append("no final answer found")
        return {"score": score, "passed": False, "breakdown": breakdown, "notes": "; ".join(notes)}

    expected = _expected_counts(workspace)
    found = _parse_counts(answer)
    if not found:
        notes.append("could not parse error counts from final answer")
        return {"score": score, "passed": False, "breakdown": breakdown, "notes": "; ".join(notes)}

    correct = sum(1 for et, ec in expected.items() if found.get(et) == ec)
    total = len(expected)
    accuracy = correct / total if total > 0 else 0
    breakdown["error_count_accuracy"] = round(accuracy * 40)
    score += round(accuracy * 40)
    notes.append(f"error counting: {correct}/{total} types correct")

    # C4: Efficiency (0-15 pts)
    rr_calls = count_tool_calls(transcript, "rr_")
    if rr_calls <= 15:
        score += 15
        breakdown["efficiency"] = 15
        notes.append(f"efficient ({rr_calls} rr calls)")
    elif rr_calls <= 30:
        score += 8
        breakdown["moderate_efficiency"] = 8
        notes.append(f"moderate efficiency ({rr_calls} rr calls)")
    else:
        notes.append(f"inefficient ({rr_calls} rr calls)")

    # C5: Error recovery (0-10 pts)
    errs = errors_encountered(transcript)
    if errs:
        if answer:
            score += 10
            breakdown["error_recovery"] = 10
            notes.append(f"recovered from {len(errs)} errors")
        else:
            notes.append(f"encountered {len(errs)} errors, did not recover")
    else:
        score += 10
        breakdown["no_errors"] = 10

    passed = score >= 70
    return {"score": score, "passed": passed, "breakdown": breakdown, "notes": "; ".join(notes)}


def _expected_counts(workspace: Path) -> dict[str, int]:
    log = workspace / "server.log"
    counts = {}
    with open(log) as f:
        for line in f:
            if "ERROR" in line:
                m = re.search(r"ERROR (\S+):", line)
                if m:
                    name = m.group(1)
                    counts[name] = counts.get(name, 0) + 1
    return counts


def _parse_counts(text: str) -> dict[str, int] | None:
    counts = {}
    for line in text.splitlines():
        m = re.match(r"Error type:\s*(\S+)\s*[-–—]\s*count:\s*(\d+)", line.strip(), re.IGNORECASE)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts if counts else None
