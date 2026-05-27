"""Smoke test: the scaffold imports and builds a server object."""

from resilient_write import __version__
from resilient_write.paths import workspace_roots


def test_version_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_build_server_returns_named_instance() -> None:
    from resilient_write.server import SERVER_NAME, build_server
    server = build_server()
    assert server.name == SERVER_NAME


def test_workspace_root_honours_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RW_WORKSPACE", str(tmp_path))
    assert workspace_roots() == [tmp_path.resolve()]


def test_workspace_root_defaults_to_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RW_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert workspace_roots() == [tmp_path.resolve()]


def test_workspace_root_rejects_system_dirs(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("RW_WORKSPACE", "/")
    with pytest.raises(SystemExit):
        workspace_roots()

    monkeypatch.setenv("RW_WORKSPACE", "/usr")
    with pytest.raises(SystemExit):
        workspace_roots()
