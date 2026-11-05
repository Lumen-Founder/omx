# Paper mode

`omx-mcp --paper`, `omx-hyperliquid --paper` and `omx paper` run the normal OMX
MCP tools with simulated fills against **live Hyperliquid public order books**.
No key, no account, no config, no daemon, no real orders.

```
omx-hyperliquid --paper                       # paper account starts at 10,000 USD
omx-hyperliquid --paper --paper-equity 2500   # another starting balance
omx paper --paper-equity 2500                 # the same from the CLI
```

`--paper`, `--demo` and `--inspect` are mutually exclusive; `--paper-equity`
needs `--paper`. The server is named `omx-paper`.

## What is real and what is simulated

| Part | Paper mode |
|---|---|
| Engine, algos (market, limit, TWAP, chase), policy checks, approval threshold, agent budget, `client_intent_id` ledger, kill switch, audit chain | The production code, default `PolicyConfig` and agent limits |
| Order books, 24 h notional, mark price | Hyperliquid **mainnet** public `/info` (`l2Book`, `metaAndAssetCtxs`, `allMids`) |
| Account (cash, positions, orders, fills) | Local, in this process only |
| Fills | A model, described below |
| Builder fee | None |

Default policy (`PolicyConfig()`): agents may trade BTC, ETH and SOL; 24 h
notional at least 50M USD; resting depth at least 250k USD within 25 bps on the
side taken; child price within 100 bps of mid; book at most 3 s old; child at
most 5,000 USD; per coin 10,000 USD; account 25,000 USD; leverage 3; daily loss
500 USD. Liquidity, depth and data age are evaluated on the real books. Agent
intents above 1,000 USD, or past the 5,000 USD daily agent budget, wait for a
human, and paper mode has no approval tool, so they stay pending and expire.

## Why it cannot trade for real

- The venue is `omx.venue.paper.PaperVenue`. Paper mode never builds
  `HyperliquidVenue` or a signer and reads no config, key file or key
  environment variable.
- Every request passes `PublicInfoOnly`: only `POST /info`, only `l2Book`
  (`type`, `coin`), `allMids` (`type`) and `metaAndAssetCtxs` (`type`). Any
  other path (such as `/exchange`) or field (such as `user`) is refused before
  anything is sent. There is nothing to sign and no account to read.
- The account address is a placeholder, the network is `paper`, there is no
  builder code.
- State, approvals, the kill file and the audit log live in a temporary
  directory deleted on exit. Nothing is written to the working directory.
- Every tool answer carries `"paper": true` and
  `"paper_note": "Simulated fills against live Hyperliquid public order books. No key, no account, no real orders."`

## Market data and request load

Hyperliquid allows 1200 request weight per minute per IP; `l2Book` and
`allMids` weigh 2, `metaAndAssetCtxs` 20 (HL docs, "Rate limits and user
limits").

- `l2Book` for a coin only when it is quoted, traded or has a working order, at
  most once per second. A book received more than 3 s ago is not served: the
  coin answers `MARKET_DATA_UNAVAILABLE:<coin>`.
- `allMids` at most every 5 s, only while the paper account has a position or
  an order (marks for equity and the account exposure check).
- `metaAndAssetCtxs` (size decimals, 24 h notional, mark) at most once a
  minute; if a refresh fails the previous copy stays in use.
- A failed request starts an exponential backoff for that request (1 s doubling
  to 30 s) during which nothing is sent; a weight bucket is a second cap.
- Idle, nothing is sent. With the three default coins all busy the worst case
  is about 400 weight per minute, a third of the limit.
- HTTPS, 3 s timeout, `User-Agent: omx-paper`. No identifying data is sent.

The book's time is Hyperliquid's; the policy compares it with the local clock,
as the live engine does. A local clock more than about 2 s off makes orders
block with `STALE_DATA`.

## Fill model (assumptions)

- **Fees:** Hyperliquid perps base tier (tier 0, no staking discount, no
  referral): taker 0.045 %, maker 0.015 %.
  Source: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees>.
- **Latency:** an order takes effect 212 ms after it is sent, a cancel 478 ms
  (the values the simulator uses, measured on our Hyperliquid ALO path). The
  order meets the book read at that moment, which is at most 1 s old.
- **Grid:** prices move to the venue grid toward the passive side and sizes are
  floored to the coin's size decimals, as the live adapter does. Orders under
  10 USD (not reduce-only) are refused, like on Hyperliquid. The engine's
  planner also waits until a child is worth 10 USD, so a TWAP whose slices are
  smaller sends fewer, larger children and may end `partial`.
- **Taker:** Ioc (and the crossing part of Gtc) walks the displayed levels up to
  its limit price, each level at its own price. The remainder of an Ioc
  expires (`IOC_REMAINDER`); a Gtc remainder rests. Liquidity we took from a
  book stays taken until a newer book arrives; the newer book is then taken as
  it is (no lasting market impact, no hidden liquidity, no other trader
  reacting to us).
- **Post-only:** an Alo that would cross is rejected (`ALO_WOULD_CROSS`).
- **Resting orders:** we assume we are **last** in the queue at our price and
  that nobody ahead of us cancels. A resting order fills only when the book
  trades **through** its price (best ask strictly below a buy, best bid strictly
  above a sell), then in full at its own price with the maker fee. A book that
  only touches our price is not a fill. Moves that happen and revert between
  two book reads are missed. Both make paper maker fills rarer than real ones
  can be.
- **Not modelled:** funding, margin and liquidation, self-trade prevention,
  the trades feed (so `algo=pov` is refused with `POV_NEEDS_TRADE_FEED`),
  account modes, deposits and withdrawals.

## Limits

- Results say how an order would have met the displayed book; they are not
  evidence of real execution quality. Large orders relative to the book, maker
  queue position and fast markets are where paper and real fills differ most.
- Only Hyperliquid mainnet default-dex perps; spot and other dexes are not
  covered.
- If Hyperliquid's public API is unreachable, quotes and orders answer
  `MARKET_DATA_UNAVAILABLE:<coin>` and resting orders do not fill until data
  returns; the server keeps running.
- One process, one paper account; it is gone when the process exits.

## Moving to real trading

Start on Hyperliquid testnet: create an API (agent) wallet in your own account,
run `omx init --account <your address> --key-path <agent key file>
--enable-agent`, `omx approve-builder` (or `--no-builder`), `omx daemon` in its
own terminal, then `omx-hyperliquid --config omx.toml`. See the README quick
start.
