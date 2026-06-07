"""Shared helpers for judges — handles both XML and OpenAI function-calling transcript formats."""

import json
import re
from typing import Any


def tool_names(transcript: list[dict[str, Any]]) -> list[str]:
    """Extract tool names from a transcript regardless of format."""
    names: list[str] = []
    for entry in transcript:
        # OpenAI function-calling format
        if entry.get("role") == "tool_call":
            n = entry.get("name", "")
            if n:
                names.append(n)
        # XML format (legacy)
        content = entry.get("content", "")
        if isinstance(content, str):
            for m in re.finditer(r'"name":\s*"(\w+)"', content):
                names.append(m.group(1))
    return names


def tool_args(transcript: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Extract arguments for all calls to a specific tool."""
    args_list: list[dict[str, Any]] = []
    for entry in transcript:
        if entry.get("role") == "tool_call" and entry.get("name") == name:
            args_list.append(entry.get("arguments", {}))
    return args_list


def tool_results(transcript: list[dict[str, Any]], name: str | None = None) -> list[dict[str, Any]]:
    """Extract tool results, optionally filtered by name."""
    results: list[dict[str, Any]] = []
    for entry in transcript:
        if entry.get("role") == "tool_result":
            if name and entry.get("name") != name:
                continue
            try:
                result = json.loads(entry.get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                result = {"raw": entry.get("content", "")}
            results.append(result)
    return results


def errors_encountered(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract error results."""
    return [r for r in tool_results(transcript) if not r.get("ok", True)]


def final_answer(transcript: list[dict[str, Any]]) -> str | None:
    """Extract the final answer from a transcript."""
    # OpenAI format: last assistant message with no tool calls after it
    for entry in reversed(transcript):
        if entry.get("role") == "assistant":
            content = entry.get("content", "")
            if content:
                return content.strip()

    # XML format fallback
    for entry in reversed(transcript):
        content = entry.get("content", "")
        if isinstance(content, str) and "<final_answer>" in content:
            start = content.index("<final_answer>") + len("<final_answer>")
            end = content.index("</final_answer>") if "</final_answer>" in content else len(content)
            return content[start:end].strip()
    return None


def count_tool_calls(transcript: list[dict[str, Any]], prefix: str) -> int:
    """Count calls to tools starting with a prefix."""
    return sum(1 for n in tool_names(transcript) if n.startswith(prefix))
