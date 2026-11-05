# OMX: Hyperliquid execution for AI agents (MCP)

[![tests](https://github.com/Lumen-Founder/omx/actions/workflows/test.yml/badge.svg)](https://github.com/Lumen-Founder/omx/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/omx-hyperliquid)](https://pypi.org/project/omx-hyperliquid/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

OMX is a non-custodial execution engine for Hyperliquid perpetuals. It runs on
your own machine, turns an order intent (market, limit, TWAP, POV, chase) into
child orders, checks every child against a pre-trade policy you configure, and
writes every decision to a hash-chained audit log. A command line and an MCP
server let a human or an AI agent drive it inside those limits.

OMX makes no claim about trading performance or profit. It is execution
plumbing with guard rails.

| You worry about | What OMX does |
|---|---|
| Your keys | Signs only with a Hyperliquid [API (agent) wallet](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets) you create in your own account; never asks for your main key or seed. The config holds the key file's path, not the key. No withdrawal or transfer code. |
| Limits | Every child order is checked locally: coin allowlist, liquidity and depth, price band and slippage, per-coin and account notional (resting orders included), leverage, daily loss, data age. The engine refuses any child the policy rejects. No MCP tool can change limits; they come from the config the daemon loaded. An agent that also has shell or file access as your OS user could edit that file, so run such agents under another OS user ([trust boundary](docs/AGENTS.md)). |
| Human approval | An agent intent above `approval_above_usd`, or beyond the agent's daily budget, waits for you: `omx approvals approve <id>`, then a confirm code typed in the daemon's terminal. Pending approvals expire. |
| Stopping it | Any agent or human can trip the kill switch; it cancels resting OMX orders. Only a human can clear it (`omx unkill`). |
| Audit | Every decision goes to a hash-chained log; `omx audit verify` detects tampering. |
| Fees | Optional 1 bp builder fee, charged only after you approve it in your own wallet; `--no-builder` for none. See [docs/FEES.md](docs/FEES.md). |

Path: the keyless demo, then paper trading on live Hyperliquid books (also
keyless), then testnet, then mainnet with small size.

## Try it now: no key, no account

One click installs the MCP server in demo mode (a simulated venue in its own
process; nothing real can be traded):

[![Install in Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=omx&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJvbXgtaHlwZXJsaXF1aWQiXX0%3D)
[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_OMX-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522omx%2522%252C%2522type%2522%253A%2522stdio%2522%252C%2522command%2522%253A%2522uvx%2522%252C%2522args%2522%253A%255B%2522omx-hyperliquid%2522%255D%257D)
[![Add to LM Studio](https://files.lmstudio.ai/deeplink/mcp-install-light.svg)](https://lmstudio.ai/install-mcp?name=omx&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJvbXgtaHlwZXJsaXF1aWQiXX0%3D)
[![Install in Goose](https://block.github.io/goose/img/extension-install-dark.svg)](https://block.github.io/goose/extension?cmd=uvx&arg=omx-hyperliquid&id=omx&name=OMX&description=Hyperliquid%20execution%20for%20AI%20agents%20%28keyless%20demo%20by%20default%29)
[![Add to Kiro](https://kiro.dev/images/add-to-kiro.svg)](https://kiro.dev/launch/mcp/add?name=omx&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22omx-hyperliquid%22%5D%7D)

Or one line:

```
claude mcp add omx -- uvx omx-hyperliquid          # Claude Code
codex mcp add omx -- uvx omx-hyperliquid           # OpenAI Codex CLI
gemini mcp add omx uvx omx-hyperliquid             # Gemini CLI
```

- Claude Code plugin: `/plugin marketplace add Lumen-Founder/omx`, then `/plugin install omx@omx`.
- Gemini CLI extension: `gemini extensions install https://github.com/Lumen-Founder/omx`.
- Any other MCP client (Claude Desktop, Cline, Windsurf, Zed, JetBrains, ...):
  `{"mcpServers": {"omx": {"command": "uvx", "args": ["omx-hyperliquid"]}}}`.
  Claude Desktop keeps it in `claude_desktop_config.json` (Settings > Developer >
  Edit Config; Windows `%APPDATA%\Claude\`, macOS `~/Library/Application Support/Claude/`);
  Cursor in `~/.cursor/mcp.json`. Pin a version with `"omx-hyperliquid@0.1.6"`.
- No `uv`? Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`;
  macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`; then open a new terminal. Or
  `pip install omx-hyperliquid` and use `omx-hyperliquid` as the command.

With no `--config`, no `OMX_CONFIG` and no `./omx.toml`, `omx-hyperliquid` runs
the demo (`--demo` forces it). The engine, policy checks, approval threshold,
agent budget, kill switch and audit log are the real code; prices are a
random-walk model, not Hyperliquid data. Every answer carries `"demo": true`.
State lives in a temporary directory that is deleted on exit. A config that is
named but missing is an error, never a silent demo.

Then ask the agent to quote ETH, preview a small TWAP and submit it (for
example buy 0.1 ETH over 10 s in 5 slices), and poll `order_status` until it
is `done`. An intent above 1,000 USD returns an `approval_id` and waits for a
human; the demo has no approval step, so it stays pending and expires. After
`kill_switch` every new order is refused. In real use only a human clears it,
with `omx unkill` (it asks you to type `I UNDERSTAND`); the demo has no
`unkill`, so restart the server to start fresh.

### Paper trading on live Hyperliquid books

`omx-hyperliquid --paper` (or `omx-mcp --paper`, `omx paper`) runs the same
tools with simulated fills against Hyperliquid mainnet's live public order
books. No key, no account, no config, no daemon: it only reads public market
data (`/info` l2Book, allMids, metaAndAssetCtxs) and never signs or sends an
order. The paper account starts at 10,000 USD (`--paper-equity 2500` to change
it); taker orders walk the real book at Hyperliquid's base-tier fees (0.045 %
taker, 0.015 % maker), resting orders fill only when the book trades through
their price, and the default policy limits apply (including Hyperliquid's 10
USD minimum order). Every answer carries `"paper": true`. Fills are a model,
not Hyperliquid's matching engine; see [docs/PAPER.md](docs/PAPER.md).

```
claude mcp add omx-paper -- uvx omx-hyperliquid --paper
```

Any other client: `{"command": "uvx", "args": ["omx-hyperliquid", "--paper"]}`.

Codex CLI waits 10 s for a server to start by default, and the first `uvx` run
downloads the dependencies; if it times out, add `startup_timeout_sec = 60`
under `[mcp_servers.omx]` in `~/.codex/config.toml`.

### Set up your own account

```
pip install omx-hyperliquid        # or: uv tool install omx-hyperliquid
omx setup                          # one guided terminal session, testnet first
omx --config <path printed by setup> daemon     # in its own terminal, keep it open
omx mcp-config                     # prints the settings for your MCP client
```

`omx setup` walks you through the network (testnet first), your main
account's public address, an API wallet key (typed into a hidden prompt and
saved to a file only your user can read, or a key file you already saved), the
eligibility questions, agent limits, the builder-fee choice, then read-only
checks. It never asks for your main wallet key or seed phrase, and refuses a
key whose address is your main account. `omx mcp-config` prints the settings
for Claude Code, Codex, Gemini, VS Code, Cursor or Claude Desktop, filled in
with your config's absolute path. See [docs/SETUP.md](docs/SETUP.md) and the
[Quick start](#quick-start-testnet) for each step by hand.

Agent frameworks: [omx-integrations](https://github.com/Lumen-Founder/omx-integrations)
has copy-and-run examples for the OpenAI Agents SDK, LangChain, the Claude
Agent SDK, PydanticAI and CrewAI, each checked against this server with the
framework's own MCP client (demo mode by default).

**Status (v0.1.6):** pre-1.0. The full path (agent intent -> policy -> signed
order with a builder code -> fill -> kill switch -> audit verify) has been run
end to end on Hyperliquid testnet. OMX has not had an external security audit.
Start on testnet, then mainnet with small size. MIT licensed, no warranty.

<!-- mcp-name: io.github.Lumen-Founder/omx-hyperliquid-trading -->

## Eligibility

OMX is not available to persons or entities residing, located, incorporated or
with a registered office in the United States, Ontario (Canada) or sanctioned
territories, nor to citizens of sanctioned territories, per the Hyperliquid
Terms of Use. Using a VPN or proxy, or a false residency statement, to get
around this is a breach; OMX offers no way around a block. OMX does not offer
outcome markets. `omx eligibility` asks these questions and refuses to trade
when the answer is blocked; it stores only country/region codes and yes/no
answers.

## Non-custodial model

- Your funds stay in your own Hyperliquid account. OMX has no withdrawal or
  transfer code path.
- You create an API / agent wallet in your own account. It can place and
  cancel orders but cannot withdraw. OMX signs with that key only.
- OMX never asks for your MAIN wallet key or seed phrase. The builder-fee
  approval (if any) is signed by you in your own wallet.
- The config holds the agent key's file PATH or env var NAME, never the key.
  A config containing anything shaped like a private key is rejected, and so
  is a command-line argument shaped like one.

## Install

```
pip install omx-hyperliquid          # PyPI; gives the `omx`, `omx-mcp` and `omx-hyperliquid` commands
```

From a checkout (development, tests):

```
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"   # Windows; .venv/bin/python elsewhere
.venv/Scripts/python -m pytest -q tests
```

The commands below also work as `python -m omx.cli ...` from a checkout.
Requires Python 3.11+ (`tomllib`; developed on 3.12). The MCP server uses the
official `mcp` Python SDK 2.x. The live Hyperliquid venue also needs
`eth_account` for signing.

## Quick start (testnet)

OMX runs as one long-lived process, `omx daemon`, which owns the engine (the
only thing that talks to Hyperliquid and holds the agent key), the audit-log
writer and the approval executor. The CLI and the MCP server are thin clients
that talk to it through files in `state_dir/ipc/`.

1. Create an agent wallet at app.hyperliquid-testnet.xyz (API page) and save
   its key in a file only your user can read, e.g. `secrets/hl_agent_testnet.key`.
2. Write the config and answer the eligibility questions:
   ```
   python -m omx.cli --config omx.toml init --account 0xYOUR_MAIN_ADDRESS --key-path secrets/hl_agent_testnet.key
   ```
   `config.example.toml` shows every field. Eligibility must be renewed every
   `eligibility_ttl_days` (default and maximum 30): `omx eligibility`.
3. Start the daemon in its own terminal and leave it running while you trade:
   ```
   python -m omx.cli --config omx.toml daemon
   ```
   It steps every working intent every 200 ms (TWAP/POV/chase slices are sent
   only while it runs). Only one daemon can run per `state_dir`, and only one
   per account (a per-account lock in `%LOCALAPPDATA%\omx\locks`, or
   `lock_dir` in the config). It refuses to
   start without a valid eligibility attestation; there is no bypass for
   trading. It re-checks eligibility every 10 minutes and turns cancel-only
   when the check fails. `daemon --cancel-only` starts without the check and
   only cancels resting OMX orders (for an expired or blocked user).
   Approvals, `unkill`, `eligibility` and `audit repair` sent to the daemon
   are shown in its terminal with what they would do and the request id the
   CLI printed; they run only after you type the `confirm <code>` line it
   prints there. Keep that terminal interactive (a real console, not `< NUL`
   or a pipe; see `docs/SECURITY.md`).
4. In another terminal, dry run, then send:
   ```
   python -m omx.cli --config omx.toml preview ETH buy 0.05 --algo twap --duration-s 300 --slices 5
   python -m omx.cli --config omx.toml submit  ETH buy 0.05 --algo twap --duration-s 300 --slices 5
   python -m omx.cli --config omx.toml status <intent_id>
   ```
   `preview`, `submit`, `status`, `cancel` and `approvals approve` need the
   daemon; without it (heartbeat older than 5 s) they stop with a message
   telling you to start `omx daemon`. After a daemon restart `status` of an
   older intent answers `unknown` (check the audit log and the venue), never
   `rejected`.
5. Stop everything at once: `python -m omx.cli --config omx.toml kill --reason "..."`.
   It always writes the `state_dir/KILL` file first (no new orders from any
   OMX process), then cancels resting OMX orders through the daemon, or, if no
   daemon is running, through a one-shot cancel-only engine it builds for this
   (no eligibility check). Either way it exits with code 3 and a `WARNING` if
   the sweep failed or an order could not be cancelled.
   `--all` cancels every open order on the account, not only OMX ones; the
   scope is stored in the KILL file and honoured by the next daemon. Clearing
   it (`unkill`) asks you to type `I UNDERSTAND`. `submit` prints the intent id;
   after a timeout check it with `status <id>` and retry with `--intent-id <id>`
   (the daemon refuses an id it already took).
6. AI agents: `python -m omx.cli --config omx.toml mcp` (usually started by
   your MCP client, see `docs/AGENTS.md`). It holds no key and no engine and
   needs the daemon running.

Stopping the daemon (Ctrl+C) does not set the kill switch and cancels
nothing: resting OMX orders stay on the venue and sliced intents stop sending.
On the next start the engine cancels orphan OMX orders from the previous run.
To cancel immediately, use `omx kill`.

Other commands: `cancel`, `approvals list|approve|reject`, `audit verify`,
`audit repair`, `fees`, `mcp`, `eligibility`, `approve-builder`, `doctor`. `audit verify` tells a torn last
line from a crash (`TRUNCATED_TAIL`, exit 4) apart from tampering (`BROKEN`,
exit 3); `audit repair` moves only a torn tail aside, after you type
`REPAIR AUDIT`.

## Controls

Every child order passes all of these before it can leave your machine:

1. Coin allowlist
2. Liquidity floor (24 h traded notional) and resting depth near mid
3. Price band vs mid, intent limit price, per-intent slippage cap
4. Per-coin worst-case notional (position plus all same-side resting orders)
5. Account-wide worst-case notional
6. Worst-case leverage vs equity
7. Daily loss limit (deposits and withdrawals kept out of P&L)
8. Market-data freshness
9. Kill switch (a file in `state_dir`; any process can trip it; `omx kill` also cancels resting OMX orders)
10. Hash-chained audit log (`omx audit verify`)

Plus a per-child notional cap, reduce-only checks, and for AI agents: an
on/off switch, an agent coin subset, a per-intent human-approval threshold, a
daily agent notional budget, approvals that expire, and no ability to unkill
or change limits. See `docs/SECURITY.md`, `docs/FEES.md`, `docs/AGENTS.md`.

## Builder fee

OMX is open source (MIT) and costs nothing to install or run. The maintainers
are paid through an optional Hyperliquid builder fee on mainnet fills. When `omx/config.py` names a default builder for the network
(`DEFAULT_BUILDER`), `omx init` sets it at 1 bp (0.01 % of filled notional).
`--no-builder` sends no builder code, `--fee-tenths-bps` sets a lower fee, and
`--builder-address` uses your own. Nothing is charged until you approve the
fee from your own wallet (`omx approve-builder`), and you can revoke it at any
time. `omx fees` shows the exact rate and the exact max rate you would
approve, before you approve anything. Details in `docs/FEES.md`.

**Enforcement.** With `[builder] address` and `fee_tenths_bps > 0` set, the
engine itself attaches `{"b": address, "f": fee}` to every order it sends.
Intents (CLI, MCP, approvals) have no builder field, so no caller can set or
remove it; a child order carrying any other builder is refused. The builder
address must differ from the account and from the agent wallet. The daemon
reads `maxBuilderFee` for (account, builder) at start and every 10 minutes
(every 60 s while blocked). While the approved maximum is below the configured
fee it is in `BUILDER_NOT_APPROVED`: new intents are refused with that reason
and the hint `omx approve-builder`; working intents finish or can be
cancelled; `omx kill` always works.

**Approve in your browser, no key in OMX:**

```
python -m omx.cli --config omx.toml approve-builder                       # approves the configured fee
python -m omx.cli --config omx.toml approve-builder --max-fee-rate 0.02%  # a higher maximum
python -m omx.cli --config omx.toml approve-builder --revoke              # maxFeeRate 0%
```

It serves one page on `127.0.0.1` (random port and path) and opens your
browser (`--no-browser` only prints the URL; `--timeout-s` stops it, default
900). The page uses your browser wallet (`window.ethereum`): it checks that the
selected account is `account_address`, asks the wallet to sign
`HyperliquidTransaction:ApproveBuilderFee`, sends the signed approval from the
browser straight to Hyperliquid's `/exchange` and re-reads `maxBuilderFee`.
OMX's Python process never receives a key or the signature.

**Check the setup:** `python -m omx.cli --config omx.toml doctor` prints
OK / FAIL (with the fix) / SKIP for: config, key file, agent address (never the
key), endpoint, account exists, account mode, account value, agent approved for
the account, builder approval vs configured fee, builder account value >= 100
USDC, builder account mode, eligibility, daemon running. It only reads `/info`,
and only when you run it.

**Account modes:** OMX reads the account's Hyperliquid `userAbstraction` at
start and every 10 minutes. Standard accounts (`disabled` / `default`) use the
perp account value as equity. Unified accounts (`unifiedAccount`, the default
for users) keep one USDC balance in the spot clearinghouse, so OMX takes equity
from the spot USDC `total`. The docs do not say whether `total` already holds
the open positions' unrealized P&L; a testnet measurement (2026-11-05, 9 of 9
intervals: `total` moved with the unrealized P&L to 1e-6) shows it does, so the
equity is `total` itself. Positions always come from the
perp clearinghouse state. `portfolioMargin`, `dexAbstraction` or an unreadable
mode refuse new intents with `ACCOUNT_MODE_UNSUPPORTED` (kill and cancels still
work). Switching to Manual / Standard is optional; builder addresses must be
standard to accrue fees. `python tools/measure_account_mode.py --config omx.toml
--samples 10 --interval 30` (read-only, no key) prints both candidate equities
and, with an open position whose P&L moves, which one the venue uses.

**Test wallets:** `python tools/new_test_wallet.py --out secrets/testnet_builder.key`
creates a new throwaway key (never overwrites a file, restricts it to your
user) and prints only its address. Use it for test wallets only (a testnet
builder, a Base Sepolia payer), never for real funds.
