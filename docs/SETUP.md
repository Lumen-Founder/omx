# Setup: `omx setup` and `omx mcp-config`

Two commands take you from nothing to an AI agent that can trade inside your limits:

```
omx setup                 # guided: network, account, API wallet key, eligibility, limits, fee, config, checks
omx daemon                # in its own terminal, kept running while you trade
omx mcp-config            # prints the MCP client settings for your config
```

Use `omx --config <path> setup` to put the config somewhere other than `./omx.toml`
(or set `OMX_CONFIG`). Every later command takes the same `--config`.

## `omx setup`

An interactive walk that reuses the same code as `omx init`, `omx eligibility`,
`omx doctor` and `omx approve-builder`. It needs a real terminal.

What it never does: ask for your MAIN wallet's private key or seed phrase, print
or log a key, put a key in the config, or move funds (OMX has no withdrawal or
transfer code).

| Step | What happens |
|---|---|
| 1. Network | `testnet` (Enter) or `mainnet`; mainnet asks once more. |
| 2. Main account | Your MAIN account's **public** address (`0x` + 40 hex). |
| 3. API wallet | Create an API (agent) wallet on the Hyperliquid API page ([testnet](https://app.hyperliquid-testnet.xyz/API), [mainnet](https://app.hyperliquid.xyz/API)). Then either give the path of a file where you saved its key, or type the key into a **hidden** prompt; OMX saves it to a new file only your user can read (default `secrets/hl_agent_<network>.key` next to the config). Only the address derived from the key is shown. A key whose address is your MAIN account is refused before anything is written. An existing key file is never overwritten. |
| 4. Eligibility | The `omx eligibility` questions. If a valid answer is already on file you may keep it. |
| 5. AI agent | On by default. Approval threshold (default 1,000 USD) and daily agent budget (default 5,000 USD): Enter keeps the default. |
| 6. Builder fee | The fee in plain words, then an explicit choice: OMX builder 1 bp (the `omx init` default on mainnet), none (`--no-builder`), or 0.1 to 0.9 bp. Testnet has no OMX builder, so no fee. |
| 7. Write | A summary, a separate question before an existing config is replaced, then one confirmation. The key file (if new) and the config are written only now. |
| 8. Checks | `omx doctor`: read-only `/info` reads; failures come with the fix. `daemon running` fails until you start it. |
| 9. Next steps | If a builder fee is configured and not approved yet, it offers the `omx approve-builder` page (your browser wallet signs; OMX never sees a key or the signature). Then it prints the `omx daemon` command and the `omx mcp-config` output. |

Ctrl+C or end of input at any prompt stops the walk; the key file and the
config are not written. The eligibility answers are recorded when given, as
`omx eligibility` does (every attempt is kept, blocked ones included).

Key file details: written to a temporary file in the target folder, restricted to
your user (POSIX mode 0600; Windows `icacls /inheritance:r /grant:r <user>:F`),
then moved into place without replacing anything. Keep the folder out of synced
or shared folders. A normal (echoed) prompt that receives something shaped like
a private key discards it.

## `omx mcp-config`

```
omx mcp-config [--client claude-code|codex|gemini|vscode|cursor|claude-desktop|json|all] [--shell auto|posix|powershell]
```

Prints ready-to-paste settings that point the published server
(`uvx omx-hyperliquid`) at your config by its **absolute** path, through the
environment variable `OMX_CONFIG` (the generic JSON uses `--config <path>`).
Default `--client all`. With no config (or one that does not load) it says to
run `omx setup` and exits with code 2. If `[agent] enabled = false`, it notes
that the MCP server will refuse to start.

| Client | Output | Syntax source |
|---|---|---|
| Claude Code | `claude mcp add --env OMX_CONFIG=<abs> --transport stdio omx -- uvx omx-hyperliquid` | [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp) |
| Codex CLI | `codex mcp add omx --env OMX_CONFIG=<abs> -- uvx omx-hyperliquid`, and a `~/.codex/config.toml` block with `startup_timeout_sec = 60` | [Codex MCP docs](https://developers.openai.com/codex/mcp) |
| Gemini CLI | `gemini mcp add -e OMX_CONFIG=<abs> omx uvx omx-hyperliquid` | [geminicli.com/docs/tools/mcp-server](https://geminicli.com/docs/tools/mcp-server/) |
| VS Code | `code --add-mcp '<json>'` (POSIX shells) and an `mcp.json` block (`"servers"`) | [VS Code MCP configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) |
| Cursor | `cursor://anysphere.cursor-deeplink/mcp/install?name=omx&config=<base64>` and the `https://cursor.com/en/install-mcp?...` form (unverified) | [Cursor install links](https://cursor.com/docs/context/mcp/install-links) |
| Claude Desktop | a `claude_desktop_config.json` block (`"mcpServers"`, with `env`) | [MCP: connect local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| JSON | `{"mcpServers": {"omx": {"command": "uvx", "args": ["omx-hyperliquid", "--config", "<abs>"]}}}` | generic |

Quoting: `--shell auto` picks PowerShell on Windows and POSIX elsewhere.
PowerShell output uses single-quoted literals and writes `--` as `'--'`, because
PowerShell drops a bare `--` when the command is a `.ps1` shim (npm installs
`codex` and `gemini` that way). On PowerShell the VS Code entry is the `mcp.json`
block only: `code.cmd` loses the inner quotes of an inline JSON argument. In
JSON and TOML, Windows backslashes are escaped (`\\`).

The output contains only the config path, never a key.

The client runs `uvx omx-hyperliquid`, the version published on PyPI. Start
`omx daemon` before the agent works; without it every tool answers
`DAEMON_NOT_RUNNING`.
