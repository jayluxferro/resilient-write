"""Tests for the L0 classifier and its integration with safe_write.

The fixture strings below are deliberately shaped like the real tokens
so the regexes exercise their real match path, but the high-entropy
portions are synthetic fillers so these tests never carry an actual
credential. Keep them that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resilient_write import policy, risk_score
from resilient_write.errors import ResilientWriteError
from resilient_write.safe_write import safe_write

# Shaped-but-synthetic fixtures. 40+ alnum chars where the regex demands it.
SYN_ANTHROPIC_OAT = "sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SYN_ANTHROPIC_API = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
SYN_OPENAI_KEY = "sk-" + "C" * 40
SYN_GHO = "gho_" + "D" * 36
SYN_GHP = "ghp_" + "E" * 36
SYN_AWS_AKID = "AKIA" + "F" * 16
SYN_DATADOG = "pub" + "a" * 32
SYN_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqYXkifQ.signaturepart_XYZ"
SYN_SSN = "123-45-6789"
SYN_EMAIL = "alice@example.com"


def _content_with(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_safe_content_is_safe() -> None:
    result = risk_score.score_content("# just a markdown heading\n\nhello world\n")
    assert result["verdict"] == "safe"
    assert result["score"] == 0.0
    assert result["detected_patterns"] == []


def test_anthropic_oat_flagged() -> None:
    result = risk_score.score_content(f"authorization: Bearer {SYN_ANTHROPIC_OAT}\n")
    kinds = {p["kind"] for p in result["detected_patterns"]}
    assert "api_key" in kinds
    assert result["verdict"] in {"medium", "high", "low"}


def test_github_pat_flagged() -> None:
    result = risk_score.score_content(f'access_token": "{SYN_GHO}"\n')
    kinds = {p["kind"] for p in result["detected_patterns"]}
    assert "github_pat" in kinds


def test_jwt_flagged() -> None:
    result = risk_score.score_content(f"cookie: session={SYN_JWT}\n")
    assert any(p["kind"] == "jwt" for p in result["detected_patterns"])


def test_pem_block_flagged() -> None:
    content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQ...\n"
    result = risk_score.score_content(content)
    assert any(p["kind"] == "pem_block" for p in result["detected_patterns"])
    # PEM weight alone (0.50) crosses `medium` but not `high`.
    assert result["verdict"] in {"medium", "high"}


def test_aws_access_key_id_flagged() -> None:
    result = risk_score.score_content(f"AWS_ACCESS_KEY_ID={SYN_AWS_AKID}\n")
    assert any(p["kind"] == "api_key" for p in result["detected_patterns"])


def test_pii_email_flagged() -> None:
    result = risk_score.score_content(f"contact: {SYN_EMAIL}\n")
    assert any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_pii_ssn_flagged() -> None:
    result = risk_score.score_content(f"ssn: {SYN_SSN}\n")
    assert any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_long_line_heuristic() -> None:
    long_line = "x" * 2500
    result = risk_score.score_content(long_line + "\n")
    kinds = {p["pattern"] for p in result["detected_patterns"]}
    assert "long_line" in kinds
    assert any(a["action"] == "escape" for a in result["suggested_actions"])


def test_total_bytes_heuristic() -> None:
    content = ("filler line\n" * 15_000)  # well over 100 KB
    result = risk_score.score_content(content)
    kinds = {p["pattern"] for p in result["detected_patterns"]}
    assert "big_100kb" in kinds


def test_match_snippets_are_truncated() -> None:
    result = risk_score.score_content(f"{SYN_ANTHROPIC_OAT}\n")
    for pat in result["detected_patterns"]:
        if pat["match"] is not None:
            assert len(pat["match"]) <= 20  # 16 + ellipsis char
            # never echo the full synthetic token back
            assert SYN_ANTHROPIC_OAT not in pat["match"]


def test_line_numbers_reported() -> None:
    content = "line1\nline2\n" + SYN_GHO + "\nline4\n"
    result = risk_score.score_content(content)
    github_hits = [p for p in result["detected_patterns"] if p["kind"] == "github_pat"]
    assert github_hits
    assert github_hits[0]["line"] == 3


def test_suggested_actions_include_redact_targets() -> None:
    content = _content_with(
        f"a: {SYN_ANTHROPIC_OAT}",
        f"b: {SYN_GHO}",
    )
    result = risk_score.score_content(content)
    redact = [a for a in result["suggested_actions"] if a["action"] == "redact"]
    assert redact
    assert set(redact[0]["targets"]) >= {"api_key", "github_pat"}


def test_multiple_hits_in_same_family_dampen() -> None:
    one = risk_score.score_content(f"{SYN_GHO}\n")
    many = risk_score.score_content(
        "\n".join([SYN_GHO, SYN_GHP, SYN_GHO, SYN_GHP, SYN_GHO]) + "\n"
    )
    # Damping: score grows but not linearly with hit count.
    assert many["score"] > one["score"]
    # Family score saturates at weight * 1.5 = 0.525 for github_pat.
    assert many["score"] <= 0.525 + 1e-9


def test_disabled_family_via_workspace_policy(tmp_path: Path) -> None:
    (tmp_path / ".resilient_write").mkdir()
    (tmp_path / ".resilient_write" / "policy.yaml").write_text(
        "version: 1\ndisable_families: [pii]\n"
    )
    pol = policy.load_policy(tmp_path)
    result = risk_score.score_content(f"contact: {SYN_EMAIL}\n", policy=pol)
    assert not any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_extend_patterns_via_workspace_policy(tmp_path: Path) -> None:
    (tmp_path / ".resilient_write").mkdir()
    (tmp_path / ".resilient_write" / "policy.yaml").write_text(
        "version: 1\n"
        "extend_patterns:\n"
        "  api_key:\n"
        "    - name: vendor_key\n"
        "      regex: 'VN-[0-9A-F]{24}'\n"
    )
    pol = policy.load_policy(tmp_path)
    result = risk_score.score_content(
        "token: VN-0123456789ABCDEF01234567\n", policy=pol
    )
    names = {p["pattern"] for p in result["detected_patterns"]}
    assert "vendor_key" in names


def test_threshold_override(tmp_path: Path) -> None:
    (tmp_path / ".resilient_write").mkdir()
    (tmp_path / ".resilient_write" / "policy.yaml").write_text(
        "version: 1\nthresholds:\n  high: 0.30\n  medium: 0.15\n  low: 0.05\n"
    )
    pol = policy.load_policy(tmp_path)
    # A single github_pat (0.35) would normally be `low` (< 0.40 medium).
    # With high=0.30 it jumps straight to `high`.
    result = risk_score.score_content(f"{SYN_GHO}\n", policy=pol)
    assert result["verdict"] == "high"


def test_safe_write_classify_rejects_high_risk(tmp_path: Path) -> None:
    draft = _content_with(
        "authorization: Bearer " + SYN_ANTHROPIC_OAT,
        "x-github-token: " + SYN_GHO,
    )
    with pytest.raises(ResilientWriteError) as exc:
        safe_write(
            tmp_path,
            path="appendix.tex",
            content=draft,
            classify=True,
        )
    assert exc.value.error == "blocked"
    assert exc.value.reason_hint == "content_filter"
    assert "api_key" in exc.value.detected_patterns
    assert "github_pat" in exc.value.detected_patterns
    assert not (tmp_path / "appendix.tex").exists()


def test_safe_write_classify_allows_safe_content(tmp_path: Path) -> None:
    result = safe_write(
        tmp_path,
        path="notes.md",
        content="# a harmless document\n\nprose only\n",
        classify=True,
    )
    assert result["ok"] is True


def test_safe_write_classify_threshold_low(tmp_path: Path) -> None:
    # A single PII hit scores 0.15 → `low`. With reject_at=low it blocks.
    with pytest.raises(ResilientWriteError) as exc:
        safe_write(
            tmp_path,
            path="notes.md",
            content=f"contact: {SYN_EMAIL}\n",
            classify=True,
            classify_reject_at="low",
        )
    assert exc.value.error == "blocked"


def test_openai_key_regex_does_not_shadow_anthropic(tmp_path: Path) -> None:
    """The `openai_key` regex used to match `sk-ant-...` as a secondary
    hit, inflating the api_key family count. After the negative
    lookahead, Anthropic tokens match `anthropic_oat` only."""
    result = risk_score.score_content(f"{SYN_ANTHROPIC_OAT}\n")
    api_key_names = [
        p["pattern"]
        for p in result["detected_patterns"]
        if p["kind"] == "api_key"
    ]
    assert "anthropic_oat" in api_key_names
    assert "openai_key" not in api_key_names


def test_openai_key_regex_still_matches_real_openai(tmp_path: Path) -> None:
    # A plain `sk-` (not `sk-ant-`, not `sk-proj-`) must still match.
    content = "OPENAI_API_KEY=" + SYN_OPENAI_KEY + "\n"
    result = risk_score.score_content(content)
    api_key_names = {
        p["pattern"]
        for p in result["detected_patterns"]
        if p["kind"] == "api_key"
    }
    assert "openai_key" in api_key_names


def test_openai_project_key_no_double_hit(tmp_path: Path) -> None:
    # `sk-proj-...` should match `openai_project_key` only.
    token = "sk-proj-" + "Z" * 40
    result = risk_score.score_content(token + "\n")
    api_key_names = [
        p["pattern"]
        for p in result["detected_patterns"]
        if p["kind"] == "api_key"
    ]
    assert "openai_project_key" in api_key_names
    assert "openai_key" not in api_key_names


def test_policy_file_env_var_absolute(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom_policy.yaml"
    custom.write_text("version: 1\ndisable_families: [pii]\n")
    monkeypatch.setenv(policy.POLICY_FILE_ENV, str(custom))
    pol = policy.load_policy(tmp_path)
    result = risk_score.score_content(f"contact: {SYN_EMAIL}\n", policy=pol)
    assert not any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_policy_file_env_var_relative(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "my.yaml").write_text(
        "version: 1\ndisable_families: [pii]\n"
    )
    monkeypatch.setenv(policy.POLICY_FILE_ENV, "policies/my.yaml")
    pol = policy.load_policy(tmp_path)
    result = risk_score.score_content(f"contact: {SYN_EMAIL}\n", policy=pol)
    assert not any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_policy_file_env_var_missing_file_falls_back_to_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(policy.POLICY_FILE_ENV, str(tmp_path / "ghost.yaml"))
    pol = policy.load_policy(tmp_path)
    # Defaults still apply — no exception raised for a missing override.
    result = risk_score.score_content(f"contact: {SYN_EMAIL}\n", policy=pol)
    assert any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_policy_file_env_var_overrides_workspace_yaml(
    tmp_path: Path, monkeypatch
) -> None:
    # Workspace YAML says disable pii, env var points elsewhere (empty).
    (tmp_path / ".resilient_write").mkdir()
    (tmp_path / ".resilient_write" / "policy.yaml").write_text(
        "version: 1\ndisable_families: [pii]\n"
    )
    other = tmp_path / "other.yaml"
    other.write_text("version: 1\n")
    monkeypatch.setenv(policy.POLICY_FILE_ENV, str(other))
    pol = policy.load_policy(tmp_path)
    # Env var wins; pii is not disabled.
    result = risk_score.score_content(f"contact: {SYN_EMAIL}\n", policy=pol)
    assert any(p["kind"] == "pii" for p in result["detected_patterns"])


def test_policy_yaml_bad_regex_rejected(tmp_path: Path) -> None:
    (tmp_path / ".resilient_write").mkdir()
    (tmp_path / ".resilient_write" / "policy.yaml").write_text(
        "version: 1\n"
        "extend_patterns:\n"
        "  api_key:\n"
        "    - name: bad\n"
        "      regex: '('\n"
    )
    with pytest.raises(ResilientWriteError) as exc:
        policy.load_policy(tmp_path)
    assert exc.value.error == "policy_violation"
    assert "bad_regex" in exc.value.context["reason"]
