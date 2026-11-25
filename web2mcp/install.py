"""`web2mcp install <profile> --client …`: wire a profile into an MCP client.

Writes the profile's stdio MCP server entry into a client's config. Editing a client's
config is hard to undo, so this previews by default and only writes with --write, making a
timestamped backup first. JSON clients (Claude Desktop, Cursor, Claude Code) are merged in
place; Codex uses TOML, so its entry is printed for you to add with `codex mcp add` rather
than rewriting your TOML.
"""

import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from .client import CallError
from .storage import load_json

CLIENTS = ("claude-desktop", "claude-code", "cursor", "codex")


def _home():
    return Path.home()


def _claude_desktop_path():
    system = platform.system()
    if system == "Darwin":
        return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    return _home() / ".config" / "Claude" / "claude_desktop_config.json"


def _cursor_path():
    return _home() / ".cursor" / "mcp.json"


def _claude_code_path():
    return _home() / ".claude.json"


def _codex_path():
    return _home() / ".codex" / "config.toml"


def _default_name(profile):
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(profile).resolve().name).strip("-")
    return name or "web2mcp"


def _server_config(profile):
    root = Path(profile).resolve()
    if not (root / "manifest.json").exists():
        raise CallError("Not a web2mcp profile (no manifest.json)")
    # Use the current interpreter (the one that has web2mcp installed) and the module entry,
    # so the client command does not depend on a generated server.py filename.
    return {"command": sys.executable, "args": ["-m", "web2mcp", "serve", str(root)]}


def _install_json(target, name, server, write):
    existing = load_json(target) if target.exists() else {}
    if not isinstance(existing, dict):
        raise CallError(f"{target} is not a JSON object; fix it before installing")
    servers = dict(existing.get("mcpServers") or {})
    previous = servers.get(name)
    servers[name] = server
    merged = {**existing, "mcpServers": servers}
    action = "unchanged" if previous == server else ("replace" if previous else "add")
    result = {
        "client_config": str(target),
        "server_name": name,
        "action": action,
        "server": server,
        "written": False,
    }
    if not write:
        result["note"] = (
            "Preview only. Re-run with --write to update the file (a timestamped backup is made first)."
        )
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".web2mcp-" + _stamp() + ".bak")
        shutil.copy2(target, backup)
        result["backup"] = str(backup)
    tmp = target.with_name("." + target.name + ".web2mcp.tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    result["written"] = True
    return result


def _install_codex(name, server):
    quoted = " ".join([server["command"], *server["args"]])
    block = (
        f"[mcp_servers.{name}]\n"
        f"command = {json.dumps(server['command'])}\n"
        f"args = {json.dumps(server['args'])}\n"
    )
    return {
        "client": "codex",
        "config_file": str(_codex_path()),
        "run_this": f"codex mcp add {name} -- {quoted}",
        "or_add_to_config_toml": block,
        "written": False,
        "note": "Codex uses TOML. Run the command above, or paste the block into "
        "~/.codex/config.toml. web2mcp does not rewrite your TOML automatically.",
    }


def _install_claude_code(name, server, write):
    # Claude Code keeps MCP servers in ~/.claude.json alongside a lot of other session state,
    # so editing it directly is risky; the supported path is the `claude mcp add` CLI. Print the
    # command (and a project-scope .mcp.json block, which is a safe standalone JSON file).
    quoted = " ".join([server["command"], *server["args"]])
    entry = {"type": "stdio", "command": server["command"], "args": server["args"]}
    return {
        "client": "claude-code",
        "run_this": f"claude mcp add --scope user --transport stdio {name} -- {quoted}",
        "or_project_mcp_json": {"mcpServers": {name: entry}},
        "written": False,
        "note": "Claude Code adds MCP servers with the `claude mcp add` CLI (its ~/.claude.json "
        "holds other state, so web2mcp does not edit it directly). Run the command above for a "
        "user-scope server, or add the block to your project's .mcp.json for a project-scope one.",
    }


def _stamp():
    import time

    return time.strftime("%Y%m%d-%H%M%S")


def install(profile, client, *, name=None, write=False):
    """Install the profile's MCP server into a client's config. Previews unless write=True."""
    if client not in CLIENTS:
        raise CallError(f"Unknown client {client!r}; choose from {list(CLIENTS)}")
    server = _server_config(profile)
    name = name or _default_name(profile)
    if client == "claude-desktop":
        return _install_json(_claude_desktop_path(), name, server, write)
    if client == "cursor":
        return _install_json(_cursor_path(), name, server, write)
    if client == "claude-code":
        return _install_claude_code(name, server, write)
    return _install_codex(name, server)
