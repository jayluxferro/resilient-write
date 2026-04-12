"""Format-aware syntax validation — ``rw.validate``.

Motivation: while writing a LaTeX paper via chunked writes, macro
definitions were missing and LaTeX errors only surfaced after a full
build.  Pre-compose validation would have caught unbalanced braces,
unclosed environments, and missing ``\\documentclass`` *before* the
chunks were concatenated — saving the agent (and user) a wasted build
cycle.

This module provides a single public function, `validate_content`,
that performs lightweight syntax checks for LaTeX, JSON, Python, and
YAML.  It is a pure function: no I/O, no network, no mutable global
state.  All validators are intentionally shallow (regex / token level)
so they stay fast even on large documents.

The return envelope always succeeds (``"ok": True``); the ``"valid"``
boolean and ``"errors"`` list carry the diagnostic payload.
"""

from __future__ import annotations

import ast
import json
import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXT_MAP: dict[str, str] = {
    ".tex": "latex",
    ".json": "json",
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def _detect_format(
    content: str,
    *,
    format_hint: str | None,
    target_path: str | None,
) -> str:
    """Resolve the format to validate against."""
    if format_hint is not None:
        return format_hint.lower()

    if target_path is not None:
        _, ext = os.path.splitext(target_path)
        fmt = _EXT_MAP.get(ext.lower())
        if fmt is not None:
            return fmt

    # Auto-detect from content.
    stripped = content.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    if stripped.startswith("---"):
        return "yaml"

    return "unknown"


def _make_error(
    line: int,
    message: str,
    *,
    col: int | None = None,
    severity: str = "error",
) -> dict[str, Any]:
    return {"line": line, "col": col, "message": message, "severity": severity}


def _result(
    fmt: str,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    has_errors = any(e["severity"] == "error" for e in errors)
    if errors and not has_errors:
        summary = f"{fmt}: {len(errors)} warning(s)"
    elif has_errors:
        n_err = sum(1 for e in errors if e["severity"] == "error")
        n_warn = sum(1 for e in errors if e["severity"] == "warning")
        parts = [f"{n_err} error(s)"]
        if n_warn:
            parts.append(f"{n_warn} warning(s)")
        summary = f"{fmt}: " + ", ".join(parts)
    else:
        summary = f"{fmt}: ok"
    return {
        "ok": True,
        "valid": not has_errors,
        "format": fmt,
        "errors": errors,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# LaTeX validator
# ---------------------------------------------------------------------------

# Matches \begin{envname} and \end{envname}.
_LATEX_ENV_RE = re.compile(r"\\(begin|end)\{([^}]*)\}")

# Common typo: \being instead of \begin.
_LATEX_TYPO_RE = re.compile(r"\\being\b")

# Unescaped percent inside a URL-like context (crude heuristic).
_LATEX_URL_PCT_RE = re.compile(r"(?:https?://|ftp://)\S*(?<!\\)%")

# Unescaped underscore outside math mode and common verbatim commands.
# We check per-line whether we are inside $...$ and skip \texttt{}, \verb
# contexts.  This regex finds bare _ that are not preceded by \.
_LATEX_BARE_UNDERSCORE_RE = re.compile(r"(?<!\\)_")


def _latex_check_braces(content: str) -> list[dict[str, Any]]:
    """Track brace depth line-by-line; report mismatches."""
    errors: list[dict[str, Any]] = []
    depth = 0
    for lineno, line in enumerate(content.splitlines(), 1):
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    errors.append(
                        _make_error(lineno, "Unexpected closing brace '}'")
                    )
                    depth = 0  # reset to avoid cascading
    if depth > 0:
        errors.append(
            _make_error(
                content.count("\n") + 1,
                f"Unclosed braces: {depth} still open at end of file",
            )
        )
    return errors


def _latex_check_environments(content: str) -> list[dict[str, Any]]:
    """Stack-based \\begin/\\end matching."""
    errors: list[dict[str, Any]] = []
    stack: list[tuple[str, int]] = []  # (env_name, line)

    # Build a line-number lookup.
    line_starts = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            line_starts.append(i + 1)

    def _lineno(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    for m in _LATEX_ENV_RE.finditer(content):
        kind, env = m.group(1), m.group(2)
        ln = _lineno(m.start())
        if kind == "begin":
            stack.append((env, ln))
        else:  # end
            if not stack:
                errors.append(
                    _make_error(ln, f"\\end{{{env}}} without matching \\begin")
                )
            else:
                top_env, top_ln = stack[-1]
                if top_env == env:
                    stack.pop()
                else:
                    errors.append(
                        _make_error(
                            ln,
                            f"\\end{{{env}}} mismatches \\begin{{{top_env}}} "
                            f"opened at line {top_ln}",
                        )
                    )
                    stack.pop()

    for env, ln in reversed(stack):
        errors.append(
            _make_error(ln, f"\\begin{{{env}}} never closed")
        )
    return errors


def _latex_check_document_structure(content: str) -> list[dict[str, Any]]:
    """Warn if \\documentclass or \\begin{document}/\\end{document} are missing."""
    errors: list[dict[str, Any]] = []
    if not re.search(r"\\documentclass", content):
        errors.append(
            _make_error(1, "\\documentclass not found", severity="warning")
        )
    if not re.search(r"\\begin\{document\}", content):
        errors.append(
            _make_error(1, "\\begin{document} not found", severity="warning")
        )
    if not re.search(r"\\end\{document\}", content):
        errors.append(
            _make_error(1, "\\end{document} not found", severity="warning")
        )
    return errors


def _latex_check_typos(content: str) -> list[dict[str, Any]]:
    """Flag common LaTeX typos."""
    errors: list[dict[str, Any]] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for m in _LATEX_TYPO_RE.finditer(line):
            errors.append(
                _make_error(
                    lineno,
                    "Probable typo: \\being (did you mean \\begin?)",
                    col=m.start() + 1,
                    severity="warning",
                )
            )
    return errors


def _latex_check_url_percent(content: str) -> list[dict[str, Any]]:
    """Warn about unescaped % inside URLs."""
    errors: list[dict[str, Any]] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for m in _LATEX_URL_PCT_RE.finditer(line):
            errors.append(
                _make_error(
                    lineno,
                    "Unescaped '%' in URL (use \\% in LaTeX)",
                    col=m.end(),
                    severity="warning",
                )
            )
    return errors


def _latex_line_in_math(line: str) -> bool:
    """Crude check: is the entire line inside math mode?

    Returns True if there is an odd number of unescaped $ before any
    non-math content. This is intentionally conservative — we only use
    it to suppress underscore warnings on lines that are clearly math.
    """
    depth = 0
    i = 0
    while i < len(line):
        if line[i] == "$" and (i == 0 or line[i - 1] != "\\"):
            depth += 1
        i += 1
    return depth % 2 == 1


def _latex_check_underscores(content: str) -> list[dict[str, Any]]:
    """Warn about unescaped _ outside math mode and verbatim contexts."""
    errors: list[dict[str, Any]] = []
    # Patterns that suppress the warning for the whole line.
    _SUPPRESS_RE = re.compile(r"\\(texttt|verb|url|href|lstinline)\b")

    for lineno, line in enumerate(content.splitlines(), 1):
        # Skip lines in verbatim-like commands.
        if _SUPPRESS_RE.search(line):
            continue
        # Skip if line appears to be in math mode (contains $).
        if "$" in line:
            continue
        # Skip comment lines.
        stripped = line.lstrip()
        if stripped.startswith("%"):
            continue
        for m in _LATEX_BARE_UNDERSCORE_RE.finditer(line):
            errors.append(
                _make_error(
                    lineno,
                    "Unescaped '_' outside math mode (use \\_)",
                    col=m.start() + 1,
                    severity="warning",
                )
            )
    return errors


def _validate_latex(content: str) -> list[dict[str, Any]]:
    """Run all LaTeX checks and return merged error list."""
    errors: list[dict[str, Any]] = []
    errors.extend(_latex_check_braces(content))
    errors.extend(_latex_check_environments(content))
    errors.extend(_latex_check_document_structure(content))
    errors.extend(_latex_check_typos(content))
    errors.extend(_latex_check_url_percent(content))
    errors.extend(_latex_check_underscores(content))
    return errors


# ---------------------------------------------------------------------------
# JSON validator
# ---------------------------------------------------------------------------


def _validate_json(content: str) -> list[dict[str, Any]]:
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        return [_make_error(exc.lineno, exc.msg, col=exc.colno)]
    return []


# ---------------------------------------------------------------------------
# Python validator
# ---------------------------------------------------------------------------


def _validate_python(content: str) -> list[dict[str, Any]]:
    try:
        ast.parse(content)
    except SyntaxError as exc:
        line = exc.lineno or 1
        col = exc.offset  # may be None
        msg = exc.msg if exc.msg else "SyntaxError"
        return [_make_error(line, msg, col=col)]
    return []


# ---------------------------------------------------------------------------
# YAML validator
# ---------------------------------------------------------------------------


def _validate_yaml(content: str) -> list[dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return [
            _make_error(
                1,
                "PyYAML not installed — YAML validation skipped",
                severity="warning",
            )
        ]
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        line = 1
        col = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line = exc.problem_mark.line + 1  # 0-based -> 1-based
            col = exc.problem_mark.column + 1
        msg = str(exc.problem) if hasattr(exc, "problem") else str(exc)
        return [_make_error(line, msg, col=col)]
    return []


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, Any] = {
    "latex": _validate_latex,
    "json": _validate_json,
    "python": _validate_python,
    "yaml": _validate_yaml,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_content(
    content: str,
    *,
    format_hint: str | None = None,
    target_path: str | None = None,
) -> dict[str, Any]:
    """Validate *content* as a specific format and return a diagnostic envelope.

    Parameters
    ----------
    content:
        The text to validate.
    format_hint:
        One of ``"latex"``, ``"json"``, ``"python"``, ``"yaml"``.
        If *None*, the format is auto-detected from *target_path* or
        from the content itself.
    target_path:
        Optional file path used for extension-based format detection
        (e.g. ``"paper.tex"`` -> LaTeX).

    Returns
    -------
    dict
        Always contains ``"ok": True``.  The ``"valid"`` field is
        *True* when no errors (only warnings or nothing) were found.
        ``"errors"`` is a list of ``{"line", "col", "message",
        "severity"}`` dicts.  ``"summary"`` is a one-line human
        description.
    """
    fmt = _detect_format(content, format_hint=format_hint, target_path=target_path)
    validator = _VALIDATORS.get(fmt)
    if validator is None:
        return _result(fmt, [])
    errors = validator(content)
    return _result(fmt, errors)
