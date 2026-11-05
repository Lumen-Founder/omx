# Security

## What OMX holds

| Item | Where | OMX access |
|---|---|---|
| Funds | Your Hyperliquid account | None. No withdraw / transfer code path. |
| Main wallet key | Your wallet | Never asked for, never read. |
| Agent (API) wallet key | A file or env var you choose | Read at runtime by the venue adapter to sign orders. |
| Config | `omx.toml` | Public address, key PATH or env var NAME, limits. |
| State | `state_dir` | Approvals, agent budget, eligibility history, KILL flag, audit log. |

## Threat model

**A leaked agent key cannot withdraw, but it can still trade badly.** Anyone
holding it can place orders on your account directly through the Hyperliquid
API, bypassing OMX entirely. OMX's limits only bind orders that go through
OMX. So:

- Treat the agent key as a trading credential. If you suspect a leak, revoke
  the agent wallet in your Hyperliquid account (API page) first, then trip the
  kill switch. Revocation is the only control that stops an attacker who is
  not using OMX.
- Keep account equity on the venue at what you are prepared to have traded
  badly. OMX's leverage cap is a cap on OMX's orders, not on the account.

**A misbehaving agent or script that goes through OMX** is bounded by the
controls below.

| Risk | Control |
|---|---|
| Trades a thin or unexpected coin | Coin allowlist; agent coin subset; liquidity floor and depth floor |
| Fat-finger price | Price band vs mid; intent limit price; slippage cap per intent |
| Oversized position | Per-child cap; per-coin and account worst-case caps that count resting orders as filled |
| Excess leverage | Worst-case leverage vs equity |
| Runaway losses | Daily loss limit (flows excluded from P&L) |
| Stale data | Data-age limit blocks orders on old books |
| Agent spends too much | Per-intent approval threshold, daily agent budget, approvals expire (default 10 min) |
| Agent loosens its own limits | No MCP tool can change policy, builder fee, keys, or clear the kill switch |
| Agent cancels a human's hedge | Agents may cancel only agent-created intents (`ag-` ids) |
| Something is wrong now | Kill switch: any process can create `state_dir/KILL`; the engine cancels resting children and blocks new ones until a human types `I UNDERSTAND` (and confirms in the daemon console) |
| Tampering with history | Hash-chained audit log, one chain even with several writers (see below) |
| Restricted person uses OMX | Eligibility attestation, re-asked every 30 days, kept as append-only history (see below) |

The MCP client name in the `agent:<name>` tag is self-reported by the client.
It is a label for the audit log, not authentication. Anyone who can start the
MCP server process has the agent's permissions; anyone who can run the CLI as
your user has the human's.

## Same-user processes and the daemon IPC

`omx daemon` takes requests as files in `state_dir/ipc/`. Any program running
as your OS user can write such a file and claim to be `human:cli`. **The real
boundary against an AI agent (or any program) that has a shell or file access
is running it under a different OS user** that cannot read or write
`state_dir`, the config or the agent key file. OMX adds, on top:

| Check | Effect |
|---|---|
| Console confirmation | approvals approve/reject, unkill, eligibility record and audit repair run only after a human types `confirm <code>` in the daemon's own terminal. The prompt shows what the request would do, from the daemon's own state (approval: coin, side, size, notional bound, algo, source agent; unkill: kill reason, scope, who set it) and the request id that the CLI printed when it sent it, so a look-alike request from another program can be told apart; the code is shown only in the daemon's terminal. Without an interactive console they are refused at once (`HUMAN_CONSOLE_REQUIRED`). On Windows a console means `GetFileType == FILE_TYPE_CHAR` and `GetConsoleMode` succeeding, so `omx daemon < NUL` (scheduled task, service wrapper), pipes (also Git Bash / mintty) and files do not count |
| Replay protection | processed request ids are kept with their deadline in `ipc/processed.jsonl`; a copy of a request is refused (`REPLAYED`), also after a restart; deadlines over 15 min ahead are refused |
| Duplicate intent ids | the CLI makes the intent id; the daemon refuses an id it already took (`DUPLICATE_INTENT_ID`, kept in `state_dir/intent_ids.jsonl`), so retrying after a timeout cannot double-order. Agents pass `client_intent_id` (intent id `ag-<client_intent_id>`) to the same ledger; a repeat returns the existing intent instead of a new order |
| Status after a restart | intents live in the daemon's memory; `omx status <id>` for an id the running daemon does not know answers state `unknown` (with a hint to check the audit log and the venue when the id was submitted to an earlier run), never `rejected` |
| Crash during approval | an approval left `approved` without a result becomes `unknown_after_crash` at the next daemon start and is never executed again |

Not covered: human `submit`, `cancel` and `kill` requests are not
console-confirmed (kill reduces risk; a forged submit is still bound by the
policy limits), and a program that can type into the daemon's terminal can
confirm.

## Kill and cancel-only

Cancelling must work for a user whose eligibility has expired or who is now
blocked. `omx kill` with no daemon builds a one-shot engine with
`from_config(cfg, purpose="kill")`: no eligibility gate, cancel-only (submit and
preview refused with `CANCEL_ONLY`). `omx daemon --cancel-only` starts the same
way and keeps cancelling OMX orders (orphans of earlier runs included) while
refusing new intents. A normal daemon re-checks eligibility every 10 minutes;
when the check fails it cancels its working intents and resting OMX orders,
refuses new ones for the rest of its life and logs `eligibility_lost`.

The KILL file stores its scope (`omx` or `all`) and reason; a stored `all`
is never narrowed by a later `omx` kill, and every engine that reads the file
(daemon, one-shot) uses the stored scope. A one-shot kill whose venue sweep
fails exits with code 3 and a warning: the kill file blocks new orders, but
resting orders may still be live.

A daemon kill reports the same way: when the daemon's sweep failed
(`venue_sweep_error`, `cancel_errors`) or orders were skipped
(`cancels_skipped`), `omx kill` exits with code 3 and prints `WARNING:`.

Orphans: OMX orders of this OMX instance on the account that the running
engine does not own are cancelled at start and again every 60 s, so an order
of a crashed run that lands late does not rest unowned.

## Two OMX instances on one account

Every OMX cloid is `0x4f4d5801` (OMX, version 1) + a 4-byte instance tag
(sha256 of the absolute `state_dir` path) + 8 bytes of sha256(child id). The
orphan sweep cancels only orders that carry this instance's tag, so a second
OMX setup (another `state_dir`) on the same account does not cancel the first
one's working orders. A kill is still a kill: scope `omx` cancels every order
with the OMX prefix, whichever instance placed it; scope `all` every order.
Orders placed by OMX before this cloid layout (R3) carry no valid instance tag
and are not swept as orphans; `omx kill` cancels them.

The daemon also holds an exclusive per-account lock file,
`<lock_dir>/<network>-<account>.lock` (`lock_dir` in the config, else
`$OMX_LOCK_DIR`, else `%LOCALAPPDATA%\omx\locks` on Windows or
`~/.local/state/omx/locks`), next to the per-`state_dir` lock: a second daemon
for the same account refuses to start, whatever its `state_dir`. The OS
releases both when the process dies. `omx kill` without a daemon does not take
the lock (cancelling must always work).

## Spot and other-dex orders

Hyperliquid lists the account's spot open orders (`@107`, `PURR/USDC`) with
the perp ones. OMX's risk checks are perp checks: spot orders, other-dex
orders (`dex:COIN`) and coins outside the perp meta are left out of exposure,
marks and `NO_MARK` (OMX trades only default-dex perps). `account_summary`
lists them under `non_perp_open_orders`. `omx kill --all` cancels spot orders
too (asset id 10000 + spot index; named pairs are looked up in `spotMeta`).
An order whose asset id OMX cannot build (another dex) is logged once as
`cancel_skipped`, never retried in a loop, and `omx kill` warns (exit 3):
cancel it in the Hyperliquid UI.

## Action expiry and resending

Every order and cancel action is signed with `expiresAfter = now +
order_expiry_ms` (config, default 15 000 ms, 2 000..60 000), the
`expires_after` field of the official SDK (`Exchange.set_expires_after`):
Hyperliquid rejects the action once that time has passed, so a request that is
delayed or retransmitted on the way cannot land later. When a place has an
ambiguous outcome (timeout, connection error, 5xx), the child stays in the
exposure checks and is reconciled from `orderStatus` by cloid. It is resent
(same cloid) only when BOTH hold: its `expiresAfter` plus a 2 s margin for
clock skew has passed, and `orderStatus` still does not know it. If the status
shows it (open, filled, cancelled, rejected), it is reconciled from that and not
resent. Your PC's clock should be synced: a clock far behind Hyperliquid's
shortens the margin.

## Hyperliquid native TWAP

Off by default (`policy.allow_native_twap = false`). With it on, an intent with
algo `native_twap` becomes one `twapOrder` on your account. The suborders are
sent by Hyperliquid, not by OMX, so the per-child checks do not run on them:
OMX checks the whole order once at full size and an estimated largest
suborder, and the full size stays in the exposure checks while it runs. Each
suborder may fill up to 3 % from the price at that moment; OMX cannot narrow
that. An agent's native TWAP always needs a human approval. If the answer to
the start request is lost, OMX has no TWAP id: it keeps the full size in
exposure for the running time and reports `NATIVE_TWAP_OUTCOME_UNKNOWN`, but it
cannot cancel that TWAP; check and stop it in the Hyperliquid app. Started
TWAPs are recorded in `state_dir/native_twaps.jsonl`, and the next start or a
kill cancels those this process does not own. Tested against the local
simulator and recorded exchange answers only; not yet run on testnet.

## Daily loss limit and deposits

Equity is read every few seconds, the ledger (deposits, withdrawals,
transfers) less often. Between ledger reads OMX takes the lower of two day-P&L
readings: with the last known flows, and with the whole equity rise since the
lowest equity seen since that ledger read counted as a deposit. A deposit made
after a loss therefore never lifts day P&L above what the last ledger read
supports, and the daily loss limit stays on; a real gain shows up at the next
ledger read. A rise above the equity of the last ledger read, or of more than
max(1 USD, 0.5 %) above that low, triggers an early ledger read (at most one
per 10 s, stretched with the other reads while orders work).

## Key storage

- Use a dedicated agent wallet per machine and per network (testnet and
  mainnet separately). Never reuse the main wallet key as an agent key.
- Store the key in a file readable only by your OS user, outside synced or
  shared folders (cloud drives, repos). `secrets/` and `*.key` are git-ignored.
- Or keep it in an environment variable set by your OS keychain or a secret
  manager, and put only the variable NAME in the config.
- Never paste a key into the config, a command line, a chat with an AI agent,
  or an issue. OMX rejects config files and arguments that contain anything
  shaped like a private key (`0x` + 64 hex, or a bare 64-hex run).
- Audit records refuse fields named like secrets and never contain keys or
  signatures.

## Builder fee approval and the approval page

- The builder fee approval (`ApproveBuilderFee`) can only be signed by your
  MAIN wallet. `omx approve-builder` never asks for it: it serves one static
  page on `127.0.0.1` (random port, random path, loopback `Host` header only,
  GET only) and your browser wallet signs there. The page refuses to ask for a
  signature unless the wallet's account is `account_address`, and sends the
  signed approval from the browser directly to Hyperliquid's `/exchange`. The
  Python process receives no key and no signature and makes no network request.
- The page has no input field. Its Content-Security-Policy allows only its own
  inline script (nonce) and connections to the configured network's API.
- The typed data is built field for field like hyperliquid-python-sdk 0.20.0's
  `sign_approve_builder_fee`, except `signatureChainId` is the wallet's current
  chain (the SDK hard-codes `0x66eee`; Hyperliquid accepts any chain there,
  `hyperliquidChain` prevents replay across Mainnet/Testnet). Check the network
  badge, builder and max rate the page and your wallet show before signing.
- Builder enforcement fails closed: an engine with a fee but no builder address
  refuses to start, a venue with a different builder than the engine refuses to
  start, a child order with another builder is refused, and an unreadable
  `maxBuilderFee` at daemon start counts as not approved.
- Account modes fail closed: an account mode other than standard
  (`disabled` / `default`) or unified (`unifiedAccount`), or one that cannot be
  read, refuses new intents and blocks every child order with
  `ACCOUNT_MODE_UNSUPPORTED`; kill and cancels still run. A unified account's
  equity is spot USDC `total`, which a testnet measurement (2026-11-05) showed
  already holds the unrealized P&L. Without that measurement the fallback is the
  lower of `total` and `total + unrealized P&L`, flagged
  `EQUITY_BASIS_UNVERIFIED` (never the higher reading). When the equity basis
  changes during a day (mode switch, or a day start from an older version), a
  rise across the change is booked as a non-P&L flow and a fall stays in day
  P&L, so a switch can only tighten the daily loss limit.
- `tools/measure_account_mode.py` reads only the config's network and account
  address and makes `/info` reads; it never opens the key file and never signs.
- `omx doctor` derives the agent address from the configured key file in your
  own process to compare it with the account and the builder; it prints the
  address, never the key. `tools/new_test_wallet.py` prints only the address of
  the key it writes, never the key, and never overwrites a file.

## Verifying the audit log

```
python -m omx.cli --config omx.toml audit verify
```

Each line is canonical JSON holding the sha256 of the previous line. Any
edit, deletion, insertion or reordering makes `verify` report `BROKEN` with
the first bad line (exit code 3). The chain proves internal consistency; to
prove the log was not replaced wholesale, copy the latest line hash somewhere
you control (for example print `omx audit verify` output into a separate log
daily).

**Several writers.** The daemon, the CLI and the MCP server may each hold an
`AuditLog` on the same file. Every append takes an exclusive OS lock on the
sidecar file `<audit_path>.lock` (`msvcrt.locking` on Windows, `flock`
elsewhere) and re-reads the last line's hash inside the lock, so objects,
threads and processes all extend one chain. Do not delete the `.lock` file
while OMX is running; it holds no data.

**Crashes.** Records that must survive a crash before the next venue action
(`child_sent`, `child_ack`, `fill`, `cancel`, `kill`, approvals, ...) are
fsync'd. A power loss during a non-durable write can leave a partial final
line. `verify` reports that as `TRUNCATED_TAIL` (a crash, not tampering), and
only for the final line; a bad line anywhere else is still `BROKEN`.
`AuditLog.repair(path)` moves the partial bytes to `<audit_path>.tail-<unix_ms>`
(kept, never deleted) and appends a durable `repair` record with the sidecar
name, offset, length and sha256. The next append does the same repair
automatically, so the `repair` record is how you see that a crash happened.
A tampered log can also be made to look truncated by cutting its last line;
the external copy of the latest hash described above is what catches that.

## Eligibility record

`omx eligibility` (and `omx init`) asks separately where you **reside** and
where you are **physically located now**, and, for Canada and Ukraine, the
province/region for both; a blank region there blocks (Ontario and the
occupied Ukrainian regions are restricted). It mirrors the Hyperliquid Terms of
Use, sections 1.6-1.9 and 3.1.5. Using a VPN or proxy, or a false statement, to
get around a block is a breach; OMX offers no way around one.

Every attempt, passing or blocked, is appended as one line to
`state_dir/eligibility_history.jsonl` and, when an audit log is attached, as a
durable `eligibility_attempt` audit record. Nothing is overwritten: a later
passing answer is a new line, and the earlier block stays on record and is
shown to the user. Orders are allowed only when the newest attempt is a pass
that has not expired (30 days by default), was made under the current Terms
version, states the current physical location, and still passes the current
rules; a blocked attempt newer than the last pass refuses, and so does a record
dated in the future.

**Cooldown after a block.** A passing answer given within 24 hours
(`BLOCK_COOLDOWN_S`) of the latest blocked attempt never counts, even after the
24 hours are over: the user must answer again after the cooldown
(`ELIGIBILITY_COOLDOWN`). Every block restarts it, and it is measured from the
latest block time in the history, so setting the clock back does not shorten
it. The onboarding screen says when a new answer will count.

**Current location is mandatory.** Any attempt saved without the current
physical location, including through the non-interactive `save_eligibility`
call, is stored as blocked (`CURRENT_LOCATION_REQUIRED`), and `require_valid`
refuses a pass record that lacks it. `check()` on its own still accepts an
`Attestation` without it, for compatibility.
`eligibility.require_valid(state_dir, now_s)` raises `EligibilityError` with
the reasons; the engine factory calls it before building an engine. An
eligibility file from before this version (`eligibility.json`) is read only:
a block in it still blocks, a pass in it must be re-attested.

## Reporting

Report suspected vulnerabilities privately to the maintainers rather than in a
public issue: on GitHub, open the repository's **Security** tab and choose
**Report a vulnerability** (private vulnerability reporting is enabled). Please
include the version, the steps to reproduce and the impact. Never include a
private key or seed phrase in a report.
