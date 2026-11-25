"""`web2mcp install`: write a profile's MCP server into a client config (preview by default)."""

import pytest

from web2mcp import install as install_mod
from web2mcp.client import CallError
from web2mcp.install import install
from web2mcp.storage import load_json, save_private


def _profile(tmp_path):
    prof = tmp_path / "prof"
    save_private(prof / "manifest.json", {"version": 2, "operations": []})
    return prof


def test_cursor_preview_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_home", lambda: tmp_path)
    prof = _profile(tmp_path)
    result = install(prof, "cursor")
    assert result["written"] is False and result["action"] == "add"
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    assert result["server"]["args"][-1] == str(prof.resolve())


def test_cursor_write_creates_config_with_backup_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_home", lambda: tmp_path)
    prof = _profile(tmp_path)
    # A pre-existing config with an unrelated server must be preserved.
    cfg_path = tmp_path / ".cursor" / "mcp.json"
    save_private(cfg_path, {"mcpServers": {"other": {"command": "x", "args": []}}})
    result = install(prof, "cursor", write=True)
    assert result["written"] is True
    assert "backup" in result  # the prior file was backed up
    cfg = load_json(cfg_path)
    assert "other" in cfg["mcpServers"]  # unrelated server kept
    assert cfg["mcpServers"][result["server_name"]]["args"][-1] == str(prof.resolve())


def test_claude_desktop_write(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_home", lambda: tmp_path)
    monkeypatch.setattr(install_mod.platform, "system", lambda: "Darwin")
    prof = _profile(tmp_path)
    result = install(prof, "claude-desktop", name="demo", write=True)
    cfg_path = tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    cfg = load_json(cfg_path)
    assert cfg["mcpServers"]["demo"]["args"] == ["-m", "web2mcp", "serve", str(prof.resolve())]
    assert result["server_name"] == "demo"


def test_claude_code_prints_cli_command_and_does_not_edit_claude_json(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_home", lambda: tmp_path)
    prof = _profile(tmp_path)
    result = install(prof, "claude-code")
    assert result["written"] is False
    assert result["run_this"].startswith("claude mcp add")
    assert str(prof.resolve()) in result["run_this"]
    assert "mcpServers" in result["or_project_mcp_json"]
    assert not (tmp_path / ".claude.json").exists()  # never edited directly


def test_codex_prints_command_and_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_home", lambda: tmp_path)
    prof = _profile(tmp_path)
    result = install(prof, "codex")
    assert result["written"] is False
    assert result["run_this"].startswith("codex mcp add")
    assert "[mcp_servers." in result["or_add_to_config_toml"]
    assert not (tmp_path / ".codex").exists()


def test_rejects_non_profile_and_unknown_client(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_home", lambda: tmp_path)
    with pytest.raises(CallError, match="Not a web2mcp profile"):
        install(tmp_path / "nope", "cursor")
    with pytest.raises(CallError, match="Unknown client"):
        install(_profile(tmp_path), "emacs")
