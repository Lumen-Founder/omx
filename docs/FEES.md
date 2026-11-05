# Fees

OMX itself has no subscription or account fee. Two fees can apply to a fill:

1. **Hyperliquid's exchange fee** (taker fee, or maker fee/rebate), set by
   Hyperliquid's fee schedule and your volume tier. OMX does not change it.
2. **An optional builder fee**, only if the config sets one. The default is
   `fee_tenths_bps = 0`: no builder code is sent and no builder fee is charged.

## Builder fee math

The config field `builder.fee_tenths_bps` is `f`, in tenths of a basis point.

```
fee rate (bps)    = f / 10
fee rate (%)      = f / 1000
builder fee (USD) = fill notional (USD) * f / 100,000
```

`f` is an integer from 0 to 100 (100 = 10 bps = 0.1 %, Hyperliquid's ceiling
for perps). The fee is charged by Hyperliquid on each fill, on top of the
exchange fee, and paid to the builder address in the config.

| f | bps | % (max rate you approve) | 1,000 USD fill | 10,000 USD fill | 100,000 USD fill |
|---|---|---|---|---|---|
| 10 | 1 | 0.01% | 0.10 USD | 1.00 USD | 10.00 USD |
| 20 | 2 | 0.02% | 0.20 USD | 2.00 USD | 20.00 USD |

Examples:

- 1 bps, buy 0.5 BTC filled at 60,000 USD: notional 30,000 USD, builder fee
  30,000 * 10 / 100,000 = **3.00 USD**.
- 2 bps, a TWAP of 4 slices of 2,500 USD each: each fill pays
  2,500 * 20 / 100,000 = 0.50 USD, **2.00 USD** in total. Slicing does not
  change the total; it is proportional to filled notional.
- A round trip (open then close 10,000 USD) at 1 bps pays the fee on both
  fills: **2.00 USD**.
- Unfilled or cancelled orders pay no builder fee.

## Approval and revocation

- Hyperliquid charges a builder fee only up to a maximum rate that you
  approved for that builder address. The approval (`ApproveBuilderFee`) is
  signed by your **main** wallet; agent wallets cannot sign it and OMX never
  does it for you.
- The rate you approve is a percent string. For `f = 10` it is exactly
  `0.01%`; for `f = 20`, `0.02%`. `omx fees` prints the exact string for your
  config.
- If OMX's configured rate is above what you approved, Hyperliquid rejects the
  orders; OMX cannot charge more than you approved. The daemon checks this
  itself (`maxBuilderFee`, at start and every 10 minutes, every 60 s while
  blocked) and refuses new intents with `BUILDER_NOT_APPROVED` until the
  approval covers the configured fee; working intents and `omx kill` are not
  affected.
- `omx approve-builder` is the approval flow: a page on `127.0.0.1` where your
  browser wallet signs `ApproveBuilderFee` (max rate = the configured fee, or
  `--max-fee-rate 0.02%`) and the browser sends it to Hyperliquid. OMX never
  sees your key or the signature.
- To revoke: `omx approve-builder --revoke` signs max rate `0%` for the same
  builder. After that, orders carrying that builder code are rejected; set
  `fee_tenths_bps = 0` to keep trading without a builder code.
- `omx doctor` shows the approved maximum next to the configured fee.

## Enforcement

With a builder configured, every order OMX sends carries
`{"b": builder, "f": fee_tenths_bps}`, attached by the engine; callers cannot
set or remove it. Each fill reports the builder fee separately from the
exchange fee: Hyperliquid's fill `fee` includes `builderFee`, OMX books
`fee - builderFee` as the exchange fee and `builderFee` as the builder fee (the
simulator computes `notional * f / 100,000`). Hyperliquid pays builder fees only
to a builder address holding at least 100 USDC of perp account value; `omx
doctor` checks it.

## You see the fee first

- `omx fees` explains the configured fee in plain words with an example and
  the exact max rate to approve.
- `omx init` shows the same disclosure before the setup checklist.
- `omx preview`, `omx submit` and `omx approvals approve` print the disclosure
  next to the order before anything is sent; `submit` and `approve` then ask
  for confirmation.
- The MCP tools `preview_order` and `policy_limits` return the fee rate and an
  upper bound of the builder fee for the intent.
