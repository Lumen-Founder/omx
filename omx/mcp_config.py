"""`omx mcp-config`: print ready-to-paste MCP client settings for THIS config file.

Every snippet points the published server (`uvx omx-hyperliquid`) at the config by its ABSOLUTE path,
through the environment variable OMX_CONFIG (or `--config <abs>` in the generic JSON). Nothing here
reads a key or touches the network; the output holds only the config path.

Syntax sources (checked 2026-11-05):
  claude-code     https://code.claude.com/docs/en/mcp  (`claude mcp add [options] <name> -- <command> [args...]`;
                  an option other than --env must sit between --env and the name)
  codex           https://developers.openai.com/codex/mcp  (`codex mcp add <name> --env VAR=VALUE -- <command>`;
                  [mcp_servers.<name>] command / args / env / startup_timeout_sec)
  gemini          https://geminicli.com/docs/tools/mcp-server/  (`gemini mcp add [options] <name> <command> [args...]`,
                  `-e KEY=value`)
  vscode          https://code.visualstudio.com/docs/agents/reference/mcp-configuration  (`code --add-mcp '<json>'`
                  with name / type / command / args / env; mcp.json uses "servers")
  cursor          https://cursor.com/docs/context/mcp/install-links  (cursor://anysphere.cursor-deeplink/mcp/install
                  ?name=..&config=<base64 of JSON.stringify(config)>)
  claude-desktop  https://modelcontextprotocol.io/docs/develop/connect-local-servers  (claude_desktop_config.json,
                  "mcpServers" with command / args / env)

Shell quoting: `posix` uses shlex.quote; `powershell` uses single quotes (literal in PowerShell) and
quotes `--` as '--', because PowerShell drops a bare `--` when the command is a .ps1 shim (npm installs
codex / gemini that way). `code --add-mcp` with inline JSON is not reliable from PowerShell (code.cmd is
a batch file and loses the inner quotes), so on that shell the mcp.json snippet is the main form.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import urllib.parse
from pathlib import Path
from typing import Optional, TextIO

from .config import ConfigError, load

SERVER_NAME = "omx"
PACKAGE = "omx-hyperliquid"
CLIENTS = ("claude-code", "codex", "gemini", "vscode", "cursor", "claude-desktop", "json")
SHELLS = ("posix", "powershell")
_PS_SAFE = re.compile(r"[A-Za-z0-9_./:=\\-]+")
_CURSOR_DEEPLINK = "cursor://anysphere.cursor-deeplink/mcp/install"
_CURSOR_WEB = "https://cursor.com/en/install-mcp"


def default_shell() -> str:
    return "powershell" if os.name == "nt" else "posix"


def quote(arg: str, shell: str) -> str:
    """One argument for a command line typed into `shell`."""
    if shell == "posix":
        return shlex.quote(arg)
    if arg != "--" and _PS_SAFE.fullmatch(arg):
        return arg                                  # options like --env and plain paths stay bare
    return "'" + arg.replace("'", "''") + "'"       # literal in PowerShell; '' is one quote


def command_line(args: list[str], shell: str) -> str:
    return " ".join(quote(a, shell) for a in args)


def server_entry(cfg_abs: str) -> dict:
    """command / args / env of the stdio server, as most clients spell it."""
    return {"command": "uvx", "args": [PACKAGE], "env": {"OMX_CONFIG": cfg_abs}}


def _json(obj) -> str:
    return json.dumps(obj, indent=2)


def claude_code(cfg_abs: str, shell: str) -> str:
    cmd = command_line(["claude", "mcp", "add", "--env", f"OMX_CONFIG={cfg_abs}", "--transport", "stdio",
                        SERVER_NAME, "--", "uvx", PACKAGE], shell)
    return cmd + "\n# add `--scope user` (before the name) to use it in every project; default scope is this project"


def codex(cfg_abs: str, shell: str) -> str:
    cmd = command_line(["codex", "mcp", "add", SERVER_NAME, "--env", f"OMX_CONFIG={cfg_abs}", "--", "uvx",
                        PACKAGE], shell)
    toml = "\n".join([
        f"[mcp_servers.{SERVER_NAME}]",
        'command = "uvx"',
        f'args = ["{PACKAGE}"]',
        "startup_timeout_sec = 60          # the first uvx run downloads the dependencies (default 10)",
        "",
        f"[mcp_servers.{SERVER_NAME}.env]",
        f"OMX_CONFIG = {json.dumps(cfg_abs, ensure_ascii=False)}",
    ])
    return cmd + "\n# or put this in ~/.codex/config.toml (it also raises the startup timeout):\n" + toml


def gemini(cfg_abs: str, shell: str) -> str:
    return command_line(["gemini", "mcp", "add", "-e", f"OMX_CONFIG={cfg_abs}", SERVER_NAME, "uvx", PACKAGE], shell)


def vscode(cfg_abs: str, shell: str) -> str:
    inline = json.dumps({"name": SERVER_NAME, "type": "stdio", **server_entry(cfg_abs)}, separators=(",", ":"))
    mcp_json = _json({"servers": {SERVER_NAME: {"type": "stdio", **server_entry(cfg_abs)}}})
    if shell == "posix":
        return (command_line(["code", "--add-mcp", inline], shell)
                + "\n# or add this to your mcp.json (Command Palette: MCP: Open User Configuration):\n" + mcp_json)
    return ("# add this to your mcp.json (Command Palette: MCP: Open User Configuration):\n" + mcp_json
            + "\n# `code --add-mcp '<json>'` also works from a POSIX shell (Git Bash, WSL); from PowerShell "
              "code.cmd loses the inner quotes, so it is not printed here (unverified there)")


def cursor_config_b64(cfg_abs: str) -> str:
    raw = json.dumps(server_entry(cfg_abs), separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def cursor(cfg_abs: str, shell: str) -> str:
    q = urllib.parse.urlencode({"name": SERVER_NAME, "config": cursor_config_b64(cfg_abs)},
                               quote_via=urllib.parse.quote)
    return (f"{_CURSOR_DEEPLINK}?{q}\n"
            "# web form of the same link (used by the README badge; not in Cursor's install-link doc, unverified):\n"
            f"{_CURSOR_WEB}?{q}")


def claude_desktop(cfg_abs: str, shell: str) -> str:
    return ("# merge into claude_desktop_config.json (Windows: %APPDATA%\\Claude\\claude_desktop_config.json; "
            "macOS: ~/Library/Application Support/Claude/claude_desktop_config.json), then restart Claude Desktop:\n"
            + _json({"mcpServers": {SERVER_NAME: server_entry(cfg_abs)}}))


def generic_json(cfg_abs: str, shell: str = "posix") -> str:
    return _json({"mcpServers": {SERVER_NAME: {"command": "uvx", "args": [PACKAGE, "--config", cfg_abs]}}})


RENDER = {"claude-code": claude_code, "codex": codex, "gemini": gemini, "vscode": vscode, "cursor": cursor,
          "claude-desktop": claude_desktop, "json": generic_json}
TITLES = {"claude-code": "Claude Code", "codex": "OpenAI Codex CLI", "gemini": "Gemini CLI", "vscode": "VS Code",
          "cursor": "Cursor (install link)", "claude-desktop": "Claude Desktop",
          "json": "Any other MCP client (generic JSON)"}


def render_clients(cfg_abs: str, client: str = "all", shell: Optional[str] = None) -> str:
    shell = shell or default_shell()
    if client != "all":
        return RENDER[client](cfg_abs, shell) + "\n"
    parts = [f"# OMX MCP server settings for config: {cfg_abs}",
             f"# shell quoting: {shell}; every entry runs `uvx {PACKAGE}` with OMX_CONFIG set to that path", ""]
    for c in CLIENTS:
        parts += [f"## {TITLES[c]}", RENDER[c](cfg_abs, shell), ""]
    return "\n".join(parts)


def run_mcp_config(cfg_path: Path, client: str, shell: Optional[str], out: TextIO, err: TextIO) -> int:
    """0 and the snippets on `out`; 2 when there is no loadable config (then `omx setup` is suggested)."""
    cfg_abs = os.path.abspath(cfg_path)
    if not Path(cfg_abs).is_file():
        err.write(f"no config at {cfg_abs}. Run `omx setup` first (or pass --config <path>).\n")
        return 2
    try:
        cfg = load(cfg_abs)
    except ConfigError as e:
        err.write(f"config error: {e}\nFix it or run `omx setup` again.\n")
        return 2
    out.write(render_clients(cfg_abs, client, shell))
    if not cfg.agent.enabled:
        err.write("note: [agent] enabled = false in this config, so the MCP server refuses to start until you "
                  "set it to true.\n")
    return 0
