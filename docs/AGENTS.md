# AI agents (MCP)

OMX exposes an MCP server on stdio so an AI agent (Claude, or any MCP client)
can quote, preview and submit orders inside the limits you set. The agent
never sees keys and cannot change limits.

## Enable

In the config:

```toml
[agent]
enabled = true
approval_above_usd = 1000.0   # per-intent notional above this waits for a human
daily_budget_usd = 5000.0     # agent notional per UTC day; beyond it every intent waits
allowed_coins = ["BTC", "ETH"] # subset of policy.allowed_coins
approval_ttl_s = 600           # pending approvals expire after this
```

The server refuses to start unless `omx eligibility` has passed and
`agent.enabled = true`.

## The daemon must run

The MCP server is a thin client. It holds no engine, no key and no audit-log
writer; every tool call is forwarded to `omx daemon` through files in
`state_dir/ipc/`, and the daemon runs it in its single engine thread. Start the
daemon yourself, in its own terminal, before the agent works:

```
python -m omx.cli --config omx.toml daemon
```

If the daemon is not running (heartbeat older than 5 s), every tool returns
`DAEMON_NOT_RUNNING`, except `kill_switch`, which still writes the shared
`state_dir/KILL` file so nothing new can be sent; resting orders are then
cancelled when a human runs `omx kill` or starts the daemon. A call the daemon
does not answer in time returns `DAEMON_TIMEOUT` with `executed: false` (the
request was withdrawn, never run) or `executed: null` (outcome unknown).

**Always pass `client_intent_id` to `submit_order`, and reuse the same value
when you retry** (after `DAEMON_TIMEOUT`, a dropped connection, or any error
whose outcome you do not know). Use a fresh id per new order: 8 to 40
characters of `A-Z a-z 0-9 _ -`. The intent id becomes
`ag-<client_intent_id>`, so you can also ask `order_status` for it. The daemon
claims the id in `state_dir/intent_ids.jsonl` (the same ledger as human
submits, kept across daemon restarts) before anything is sent or queued; a
second `submit_order` with that id sends nothing and returns the existing
intent: `status: "duplicate"`, its `intent_id` and `report` (state), or its
`approval_id` and `approval_state` when it waits for a human. After a daemon
restart the report state is `unknown` (the outcome is in the audit log and on
the venue); never treat that as a reason to submit under a new id. The
arguments of the retry are ignored. Without `client_intent_id` a retry is a
new order.

## Connect

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "omx": {
      "command": "C:/path/to/OMX/.venv/Scripts/python.exe",
      "args": ["-m", "omx.mcp_server", "--config", "C:/path/to/OMX/omx.toml"],
      "cwd": "C:/path/to/OMX"
    }
  }
}
```

Claude Code:

```
claude mcp add omx -- C:/path/to/OMX/.venv/Scripts/python.exe -m omx.mcp_server --config C:/path/to/OMX/omx.toml
```

Other clients: run `python -m omx.mcp_server --config <path>` as a stdio
server. `python -m omx.cli --config <path> mcp` does the same.

`omx-mcp --inspect` (or `omx-hyperliquid --inspect`) starts with no config,
daemon or key: it lists the tools and refuses every call with `NOT_CONFIGURED`.
MCP directories use it (the `Dockerfile` runs it); it cannot trade.

`omx-mcp --demo` (or `omx-hyperliquid --demo`, or `omx demo`) also needs no
config, daemon or key, but answers every call. It runs the real engine,
gateway, policy, approval threshold, agent budget, kill switch and audit log in
one process on the local simulator (`omx/venue/sim.py`), stepped every 200 ms
like the daemon, so a TWAP fills over time. It never builds the Hyperliquid
adapter or a signer and opens no network connection. Every answer carries
`"demo": true` and a `demo_note`. There is no approval tool, so an intent above
the threshold stays `pending` and expires; the message says so. State is kept
in a temporary directory deleted on exit, nothing in the working directory.
Use it to learn the tool surface; results say nothing about real fills.

## Tools

| Tool | Effect |
|---|---|
| `get_quote(coin)` | Best bid/ask, mid, data age, depth within the policy band |
| `preview_order(...)` | Dry run: engine plan and policy decisions, notional bound, builder-fee bound, whether a human must approve. Sends nothing. |
| `submit_order(...)` | Sends the intent, or queues it for a human and returns an `approval_id` |
| `approval_status(approval_id)` | pending / approved / rejected / expired / executed / failed / unknown_after_crash |
| `order_status(intent_id)` | Fill progress, average price, cost vs arrival mid, fees, block reasons |
| `cancel_order(intent_id)` | Cancels an intent created under the same client name only |
| `kill_switch(reason)` | Stops all new orders and cancels resting ones |
| `account_summary()` | Equity, day P&L, positions, open orders, remaining agent budget |
| `policy_limits()` | Read-only view of limits, agent thresholds and builder fee |

Order arguments: `coin`, `side` (buy|sell), `size` (base units), `algo`
(market|limit|twap|pov|chase), `limit_px`, `duration_s`, `slices`, `pov_rate`,
`post_only`, `reduce_only`, `max_slippage_bps` (at most the policy price band),
`note` (max 200 chars); `submit_order` also takes `client_intent_id` (see
above). Every result is JSON with `ok` and `block_reasons`.

There is deliberately no tool to unkill, approve, or change policy, builder
fee or keys, and the daemon refuses those operations from an agent client
(`ROLE_REFUSED`). The agent cannot unkill, cannot approve its own queued
intents and cannot change limits; limits come from the config the daemon
loaded at start (changing them means a human edits the config and restarts
the daemon). A kill reason or any request that contains a private-key-shaped
string is refused.

Trust boundary: the IPC files are protected only by your OS user's file
permissions, and the `client` label in a request file is self-declared. An
agent that also has a shell or file-write tool running as the same OS user
(e.g. a coding agent) can write request files and run `omx` commands as if it
were you. **The real boundary against such an agent is running it under a
different OS user** (one that cannot read or write `state_dir`, the config or
the key file). Give trading agents MCP access only.

On top of that, OMX adds two checks (R2):

- **Console confirmation.** The daemon never runs a human-privileged request
  (approvals approve/reject, unkill, eligibility record, audit repair) from an
  IPC file alone. It prints the request in its own terminal with what it would
  do (for an approval: coin, side, size, notional bound, algo and the agent
  that asked; for unkill: the kill reason and scope) and the request id the CLI
  printed, and runs it only after a human types the exact line it shows there
  (`confirm <code>`; the code appears only in that terminal; `deny <code>`
  refuses). A daemon without an interactive console (stdin is not a real
  console: a service, a pipe, a file or `< NUL`) refuses these requests at once
  with `HUMAN_CONSOLE_REQUIRED`.
  Agent tools keep working over IPC. Run locally (no daemon), `unkill`,
  `approvals reject`, `eligibility` and `audit repair` need an interactive
  terminal.
- **Replay protection.** Each request id runs at most once: the daemon records
  processed ids with their deadline in `state_dir/ipc/processed.jsonl` (also
  across restarts) and answers `REPLAYED` to a copy. Deadlines more than 15
  minutes ahead are refused, which bounds that file.

These raise the bar; they do not stop a same-user program that can also type
into the daemon's terminal. Human `submit`/`cancel` requests are not
console-confirmed: a same-user program can still send orders labelled
`human:cli`, inside the policy limits.

## Source tagging

Every agent intent carries `source = "agent:<client name>"`, taken from the
MCP client's `initialize` info (sanitised, max 40 chars), and the engine's
audit log records it. The name is self-reported: a label, not an identity
check. Agent intent ids start with `ag-`; human CLI ids start with `hu-`. The
daemon records which client name created each intent; `cancel_order` works
only for intents created under the same name (also after a human approved
them). Two clients reporting the same name share that right. The record is
kept in the daemon's memory; after a daemon restart agents cannot cancel
older intents (`omx kill` still works), and `order_status` of such an intent
answers `UNKNOWN_INTENT_ID` (unknown, not rejected: it may have filled).

## Approval thresholds

The daemon checks and reserves the budget in one step and gives the
reservation back if the engine rejects the intent, so parallel tool calls
cannot spend more than the budget. For each `submit_order` OMX computes an upper bound of the intent's notional:
`size * mid * (1 + max_slippage_bps / 10,000)` (capped at `limit_px` for
buys). The intent goes to the human queue when either:

- the bound is above `approval_above_usd` (`ABOVE_APPROVAL_THRESHOLD`), or
- today's agent notional plus the bound would exceed `daily_budget_usd`
  (`AGENT_DAILY_BUDGET_EXCEEDED`).

The human sees and decides:

```
python -m omx.cli --config omx.toml approvals list
python -m omx.cli --config omx.toml approvals approve ap-1234abcd5678
python -m omx.cli --config omx.toml approvals reject  ap-1234abcd5678 --note "too large"
```

`approve` shows the intent and the fee disclosure, asks for confirmation, and
sends the decision to the daemon, which shows it in its own console and submits
it once after you type the confirm line there (approvals are executed by the
daemon only, so `approve` needs it running). If the daemon dies between
approving and recording the result, the next daemon marks that approval
`unknown_after_crash` and never runs it again; check the intent with
`omx status <intent_id>`. Approved intents also count toward the agent budget. A
pending approval expires after `approval_ttl_s` and can no longer be approved.
Every queue event (queued, approved, rejected, expired, executed, failed) is
written to the audit log. Approval does not bypass the policy: the engine
still checks every child order.

## Kill switch

Agents may trip the kill switch (`kill_switch(reason)`); the reason is logged
with the agent tag. Only a human can clear it:

```
python -m omx.cli --config omx.toml unkill     # asks you to type I UNDERSTAND
```

The human can also trip it from any terminal with `omx kill --reason ...`; it
writes the `state_dir/KILL` file first (effective even when the venue is
unreachable) and then cancels resting OMX orders through the daemon, or through
a one-shot engine when no daemon is running. An agent's `kill_switch` cancels
OMX orders only (scope `omx`), never the user's other orders.
