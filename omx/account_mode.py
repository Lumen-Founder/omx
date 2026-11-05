"""Hyperliquid account abstraction modes and the account equity OMX uses for its policy.

/info {"type": "userAbstraction", "user": ...} answers one of
  "disabled" / "default"  -> standard: separate perp and spot balances (Manual / Standard)
  "unifiedAccount"        -> one balance per asset collateralizes cross positions and spot
  "portfolioMargin"       -> HYPE / BTC / USDC / USDT unified as collateral
  "dexAbstraction"        -> discontinued mode
OMX v1 supports standard and unified accounts. Portfolio margin and DEX abstraction (and any
answer not in this list) refuse new intents with ACCOUNT_MODE_UNSUPPORTED; kill and cancels still run.

Equity basis of a unified account -- what the docs say (read 2026-11-05):
  * Trading / Account abstraction modes: "For API users, unified account and portfolio margin show
    all balances and holds in the spot clearinghouse state. Individual perp dex user states are not
    meaningful."
  * Info endpoint / Spot, spotClearinghouseState: "Under unified account or portfolio margin, this is
    the source of truth for trading account balance across spot and perps." The page does not define
    the fields `total`, `hold` or `entryNtl`.
  * Info endpoint / Perpetuals, clearinghouseState: "Under unified account or portfolio margin, use
    spot balances endpoint instead".
  * Unified Account Ratio (computeUnifiedAccountRatio): available = spot `total` of the collateral
    token - sum(marginUsed of isolated positions); ratio = sum(crossMaintenanceMarginUsed) / available.
So the spot USDC `total` is the account's collateral balance, and isolated margin sits inside it.
Whether the unrealized P&L of open perp positions is inside `total` is NOT stated anywhere. The ratio
formula, read like the standard-mode ratio (maintenance margin / account value, which includes
unrealized P&L), hints that it is; "balance" could equally mean the cash balance before unrealized
P&L (as perp `totalRawUsd` is). The docs do not settle it, so OMX takes the conservative choice:

    equity = min(spot USDC total, spot USDC total + sum(unrealizedPnl of perp positions))

and marks the snapshot EQUITY_BASIS_UNVERIFIED. With losing positions this counts the loss even if
`total` already holds it (equity understated, never overstated); with winning positions it ignores
the gain. tools/measure_account_mode.py on testnet (an open position whose unrealized P&L moves)
settles it: set VERIFIED_UNIFIED_BASIS to the measured basis and the flag goes away.
"""
from __future__ import annotations

import math
from typing import Optional

STANDARD_MODES = frozenset({"disabled", "default"})
UNIFIED_MODES = frozenset({"unifiedAccount"})
SUPPORTED_MODES = STANDARD_MODES | UNIFIED_MODES
KNOWN_UNSUPPORTED = frozenset({"portfolioMargin", "dexAbstraction"})
UNKNOWN_MODE = "unknown"                 # the mode could not be read (fail closed: unsupported)

ACCOUNT_MODE_UNSUPPORTED = "ACCOUNT_MODE_UNSUPPORTED"
EQUITY_BASIS_UNVERIFIED = "EQUITY_BASIS_UNVERIFIED"

BASIS_PERP = "perp_account_value"                     # clearinghouseState.marginSummary.accountValue
BASIS_SPOT_TOTAL = "spot_usdc_total"                  # candidate A: total already holds unrealized P&L
BASIS_SPOT_PLUS_UPNL = "spot_usdc_total_plus_upnl"    # candidate B: total is cash before unrealized P&L
BASIS_UNIFIED_CONSERVATIVE = "unified_min_of_candidates"

# Set to BASIS_SPOT_TOTAL or BASIS_SPOT_PLUS_UPNL once a testnet measurement settles it.
VERIFIED_UNIFIED_BASIS: Optional[str] = None


def is_supported(mode: Optional[str]) -> bool:
    return mode in SUPPORTED_MODES


def mode_block_reason(mode: Optional[str]) -> Optional[str]:
    """None for a supported mode, else the refusal text for new intents / children."""
    if is_supported(mode):
        return None
    return f"{ACCOUNT_MODE_UNSUPPORTED}:{mode}"


def _num(x, what: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{what} is not a number: {x!r}") from None
    if not math.isfinite(v):
        raise ValueError(f"{what} is not finite: {x!r}")
    return v


def usdc_balance(spot_state) -> tuple[float, float]:
    """(total, hold) of the USDC row of spotClearinghouseState (0, 0 when there is none)."""
    rows = (spot_state or {}).get("balances") if isinstance(spot_state, dict) else None
    if rows is None:
        raise ValueError("spotClearinghouseState has no balances list")
    for r in rows:
        if r.get("coin") == "USDC":
            return _num(r.get("total"), "USDC total"), _num(r.get("hold", 0.0), "USDC hold")
    return 0.0, 0.0


def perp_upnl(perp_state) -> float:
    """Sum of unrealizedPnl over the default-dex perp positions of clearinghouseState."""
    total = 0.0
    for ap in (perp_state or {}).get("assetPositions", []) or []:
        p = ap.get("position") or {}
        if float(p.get("szi") or 0.0) == 0:
            continue
        total += _num(p.get("unrealizedPnl") or 0.0, f"unrealizedPnl of {p.get('coin')}")
    return total


def unified_equity(spot_state, perp_state, verified: Optional[str] = None) -> dict:
    """Account equity of a unified account and how it was chosen (see the module doc).

    verified: None = use VERIFIED_UNIFIED_BASIS (module constant)."""
    verified = VERIFIED_UNIFIED_BASIS if verified is None else verified
    total, hold = usdc_balance(spot_state)
    upnl = perp_upnl(perp_state)
    a, b = total, total + upnl
    if verified == BASIS_SPOT_TOTAL:
        equity, basis, flags = a, BASIS_SPOT_TOTAL, ()
    elif verified == BASIS_SPOT_PLUS_UPNL:
        equity, basis, flags = b, BASIS_SPOT_PLUS_UPNL, ()
    else:
        equity, basis, flags = min(a, b), BASIS_UNIFIED_CONSERVATIVE, (EQUITY_BASIS_UNVERIFIED,)
    return {"equity_usd": equity, "basis": basis, "flags": flags, "spot_usdc_total": total,
            "spot_usdc_hold": hold, "perp_upnl_usd": upnl,
            "candidates": {BASIS_SPOT_TOTAL: a, BASIS_SPOT_PLUS_UPNL: b}}
