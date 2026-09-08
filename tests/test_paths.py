"""Tests for workspace-root parsing and the unsafe-root guard override.

The unsafe-root blocklist refuses ``/`` and OS-critical directories by
default. ``$RW_ALLOW_UNSAFE_ROOTS`` downgrades the refusal to a one-time
stderr warning — this file pins both behaviors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from resilient_write import paths


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch):
    # ``_warned_unsafe`` is process-global; reset so warn-once assertions
    # don't leak between tests.
    monkeypatch.setattr(paths, "_warned_unsafe", set())


def test_guard_refuses_unsafe_root_without_override(monkeypatch):
    monkeypatch.delenv("RW_ALLOW_UNSAFE_ROOTS", raising=False)
    with pytest.raises(SystemExit):
        paths._guard_unsafe([Path("/")], "RW_WORKSPACE")


def test_guard_allows_unsafe_root_with_override(monkeypatch, capsys):
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", "1")
    paths._guard_unsafe([Path("/")], "RW_WORKSPACE")  # must not raise
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "RW_ALLOW_UNSAFE_ROOTS" in err
    assert "'/'" in err


def test_guard_warns_once_per_root(monkeypatch, capsys):
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", "1")
    paths._guard_unsafe([Path("/tmp")], "RW_WORKSPACE")
    paths._guard_unsafe([Path("/tmp")], "RW_WORKSPACE")
    assert capsys.readouterr().err.count("WARNING") == 1


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_truthy_values_enable_override(value, monkeypatch):
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", value)
    paths._guard_unsafe([Path("/")], "RW_WORKSPACE")  # must not raise


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsy_values_do_not_enable_override(value, monkeypatch):
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", value)
    with pytest.raises(SystemExit):
        paths._guard_unsafe([Path("/")], "RW_WORKSPACE")


def test_workspace_roots_respects_override(monkeypatch):
    monkeypatch.setenv("RW_WORKSPACE", '["/"]')
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", "1")
    assert paths.workspace_roots() == [Path("/")]


def test_state_root_respects_override(monkeypatch):
    monkeypatch.setenv("RW_STATE_DIR", "/var")
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", "1")
    # _parse_root_list resolves the root, so compare against the resolved
    # form (/private/var on macOS, /var on Linux).
    assert paths.state_root() == Path("/var").resolve()


def test_state_root_refuses_without_override(monkeypatch):
    monkeypatch.setenv("RW_STATE_DIR", "/var")
    monkeypatch.delenv("RW_ALLOW_UNSAFE_ROOTS", raising=False)
    with pytest.raises(SystemExit):
        paths.state_root()


@pytest.mark.skipif(sys.platform != "darwin", reason="/tmp symlink canonicalization is macOS-specific")
def test_tmp_symlink_resolution_is_caught_on_macos(monkeypatch):
    # Path("/tmp").resolve() -> /private/tmp on macOS; the resolved blocklist
    # must catch the canonicalized form, not just the raw "/tmp" string.
    monkeypatch.setenv("RW_STATE_DIR", "/tmp")
    monkeypatch.delenv("RW_ALLOW_UNSAFE_ROOTS", raising=False)
    with pytest.raises(SystemExit):
        paths.state_root()


@pytest.mark.skipif(sys.platform != "darwin", reason="/tmp symlink canonicalization is macOS-specific")
def test_tmp_override_unlocks_canonicalized_form_on_macos(monkeypatch):
    monkeypatch.setenv("RW_STATE_DIR", "/tmp")
    monkeypatch.setenv("RW_ALLOW_UNSAFE_ROOTS", "1")
    assert paths.state_root() == Path("/private/tmp")
