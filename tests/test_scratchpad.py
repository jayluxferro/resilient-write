"""Tests for L4 scratchpad."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from resilient_write import scratchpad
from resilient_write.errors import ResilientWriteError


def _sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# scratch_put
# ---------------------------------------------------------------------------


def test_put_writes_bin_and_index(tmp_path: Path) -> None:
    out = scratchpad.scratch_put(
        tmp_path,
        content="raw secret material\n",
        label="test_alpha",
        content_type="text/plain",
    )
    assert out["ok"] is True
    expected_sha = _sha_text("raw secret material\n")
    assert out["sha256"] == expected_sha
    assert out["scratch_path"] == f".resilient_write/scratch/{expected_sha}.bin"
    assert out["bytes"] == len("raw secret material\n".encode())
    assert out["deduped"] is False

    bin_path = tmp_path / out["scratch_path"]
    assert bin_path.read_bytes() == b"raw secret material\n"

    index_path = tmp_path / ".resilient_write/scratch/index.jsonl"
    assert index_path.exists()
    entries = [json.loads(ln) for ln in index_path.read_text().splitlines() if ln]
    assert len(entries) == 1
    assert entries[0]["sha256"] == expected_sha
    assert entries[0]["label"] == "test_alpha"
    assert entries[0]["content_type"] == "text/plain"


def test_put_dedups_identical_content(tmp_path: Path) -> None:
    a = scratchpad.scratch_put(tmp_path, content="same\n", label="first")
    b = scratchpad.scratch_put(tmp_path, content="same\n", label="second")
    assert a["sha256"] == b["sha256"]
    assert a["deduped"] is False
    assert b["deduped"] is True
    # The bin was written once; the index has two rows (two aliases).
    index_path = tmp_path / ".resilient_write/scratch/index.jsonl"
    lines = [ln for ln in index_path.read_text().splitlines() if ln]
    assert len(lines) == 2


def test_put_base64_encoding(tmp_path: Path) -> None:
    raw = b"\x00\x01\x02\xff\xfe\xfdnot utf8"
    b64 = base64.b64encode(raw).decode("ascii")
    out = scratchpad.scratch_put(
        tmp_path, content=b64, encoding="base64", content_type="application/octet-stream"
    )
    assert out["sha256"] == _sha_bytes(raw)
    bin_path = tmp_path / out["scratch_path"]
    assert bin_path.read_bytes() == raw


def test_put_bad_base64_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_put(
            tmp_path, content="not valid base64 !!", encoding="base64"
        )
    assert exc.value.error == "policy_violation"
    assert "bad_base64" in exc.value.context["reason"]


def test_put_unknown_encoding_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_put(tmp_path, content="x", encoding="hex")
    assert exc.value.error == "policy_violation"


def test_put_warns_when_gitignore_missing(tmp_path: Path) -> None:
    out = scratchpad.scratch_put(tmp_path, content="x\n")
    assert len(out["warnings"]) == 1
    assert out["warnings"][0]["reason"] == "state_dir_not_gitignored"


def test_put_no_warning_when_gitignored(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# comment\n.resilient_write/\nother\n")
    out = scratchpad.scratch_put(tmp_path, content="x\n")
    assert out["warnings"] == []


def test_put_no_warning_alt_gitignore_forms(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".resilient_write\n")
    out = scratchpad.scratch_put(tmp_path, content="x\n")
    assert out["warnings"] == []


# ---------------------------------------------------------------------------
# scratch_ref
# ---------------------------------------------------------------------------


def test_ref_by_sha256(tmp_path: Path) -> None:
    p = scratchpad.scratch_put(tmp_path, content="m\n", label="alpha")
    ref = scratchpad.scratch_ref(tmp_path, sha256=p["sha256"])
    assert ref["ok"] is True
    assert ref["entry"]["label"] == "alpha"
    assert ref["bin_exists"] is True
    assert ref["alias_count"] == 1


def test_ref_by_label(tmp_path: Path) -> None:
    scratchpad.scratch_put(tmp_path, content="m\n", label="alpha")
    ref = scratchpad.scratch_ref(tmp_path, label="alpha")
    assert ref["entry"]["label"] == "alpha"


def test_ref_returns_latest_matching(tmp_path: Path) -> None:
    scratchpad.scratch_put(tmp_path, content="a\n", label="shared")
    scratchpad.scratch_put(tmp_path, content="b\n", label="shared")
    ref = scratchpad.scratch_ref(tmp_path, label="shared")
    # Latest write wins; aliases count across entries matching the label.
    assert ref["entry"]["sha256"] == _sha_text("b\n")
    assert ref["alias_count"] == 2


def test_ref_missing(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_ref(tmp_path, label="ghost")
    assert exc.value.error == "stale_precondition"


def test_ref_requires_sha_or_label(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_ref(tmp_path)
    assert exc.value.error == "policy_violation"


def test_ref_bad_sha256_shape(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_ref(tmp_path, sha256="not-a-hash")
    assert exc.value.error == "policy_violation"
    assert exc.value.context["reason"] == "not_a_lowercase_sha256_hex"


# ---------------------------------------------------------------------------
# scratch_get
# ---------------------------------------------------------------------------


def test_get_returns_utf8_content(tmp_path: Path) -> None:
    p = scratchpad.scratch_put(tmp_path, content="hello\n", label="greet")
    got = scratchpad.scratch_get(tmp_path, sha256=p["sha256"])
    assert got["content"] == "hello\n"
    assert got["label"] == "greet"
    assert got["bytes"] == 6


def test_get_base64_for_binary(tmp_path: Path) -> None:
    raw = b"\x00\x01\x02\xff"
    p = scratchpad.scratch_put(
        tmp_path, content=base64.b64encode(raw).decode(), encoding="base64"
    )
    got = scratchpad.scratch_get(tmp_path, sha256=p["sha256"], encoding="base64")
    assert base64.b64decode(got["content"]) == raw


def test_get_non_utf8_default_raises(tmp_path: Path) -> None:
    raw = b"\xff\xfe\xfd"
    p = scratchpad.scratch_put(
        tmp_path, content=base64.b64encode(raw).decode(), encoding="base64"
    )
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_get(tmp_path, sha256=p["sha256"])  # default utf-8
    assert exc.value.error == "policy_violation"
    assert exc.value.context["reason"] == "not_valid_utf8"


def test_get_missing_hash(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_get(tmp_path, sha256="a" * 64)
    assert exc.value.error == "stale_precondition"


def test_get_gated_by_env(tmp_path: Path, monkeypatch) -> None:
    p = scratchpad.scratch_put(tmp_path, content="secret\n")
    monkeypatch.setenv(scratchpad.DISABLE_GET_ENV, "1")
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_get(tmp_path, sha256=p["sha256"])
    assert exc.value.error == "policy_violation"
    assert exc.value.reason_hint == "permission"
    assert exc.value.context["reason"] == "scratch_get_disabled"


def test_get_drift_detection(tmp_path: Path) -> None:
    p = scratchpad.scratch_put(tmp_path, content="original\n")
    bin_path = tmp_path / p["scratch_path"]
    bin_path.write_bytes(b"tampered\n")
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_get(tmp_path, sha256=p["sha256"])
    assert exc.value.error == "write_corruption"
    assert exc.value.context["reason"] == "hash_drift_on_read"


def test_get_bad_sha_shape(tmp_path: Path) -> None:
    with pytest.raises(ResilientWriteError) as exc:
        scratchpad.scratch_get(tmp_path, sha256="short")
    assert exc.value.error == "policy_violation"
