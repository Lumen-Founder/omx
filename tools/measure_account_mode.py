"""Measure a Hyperliquid account's mode and which equity basis a unified account really has.

READ-ONLY and KEY-FREE: it reads only the config (network, account address) and makes /info reads
(userAbstraction, spotClearinghouseState, clearinghouseState). It never opens the agent key, never
signs and never sends an order. Network access happens only when a human runs it.

Purpose: OMX takes a unified account's equity as min(spot USDC total, total + unrealized P&L) and
marks it EQUITY_BASIS_UNVERIFIED, because the docs do not say whether spot `total` already holds the
unrealized P&L of perp positions (see omx/account_mode.py). With a small open perp position on a
TESTNET account in unified mode, run it with several samples while the price moves:

    .venv\\Scripts\\python.exe -B tools\\measure_account_mode.py --config omx.toml --samples 10 --interval 30

Between samples whose summed unrealized P&L moved, it compares the change of the USDC `total` with the
change of the unrealized P&L: a ratio near 1 means `total` already holds it (basis spot_usdc_total),
near 0 means it does not (basis spot_usdc_total_plus_upnl). A funding payment or a fill between two
samples also moves `total`; repeat if the verdict is inconclusive.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

try:
    from omx import account_mode as AM
except ImportError:                               # run from a checkout without `pip install -e .`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from omx import account_mode as AM

MIN_UPNL_MOVE = 0.0005                            # USDC; smaller moves are not used for the verdict. HL prints
                                                  # 6 decimals, so a 0.0005 move still gives the ratio to 0.4 %
SAME, NONE_ = (0.8, 1.2), (-0.2, 0.2)


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def sample(info: Callable[[dict], object], user: str) -> dict:
    mode = info({"type": "userAbstraction", "user": user})
    spot = info({"type": "spotClearinghouseState", "user": user}) or {}
    perp = info({"type": "clearinghouseState", "user": user}) or {}
    total, hold = AM.usdc_balance(spot)
    upnl = AM.perp_upnl(perp)
    return {"t_ms": int(time.time() * 1000), "mode": mode, "spot": spot, "perp": perp,
            "usdc_total": total, "usdc_hold": hold, "upnl": upnl,
            "candidates": {AM.BASIS_SPOT_TOTAL: total, AM.BASIS_SPOT_PLUS_UPNL: total + upnl}}


def show(s: dict, out: TextIO, raw: bool) -> None:
    w = out.write
    w(f"\n=== sample at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(s['t_ms'] / 1000))} ===\n")
    w(f"userAbstraction: {s['mode']!r}\n")
    w("spot balances (spotClearinghouseState):\n")
    for b in (s["spot"].get("balances") or []):
        mark = "   <- USDC" if b.get("coin") == "USDC" else ""
        w(f"  {str(b.get('coin')):<10} total {b.get('total')}  hold {b.get('hold')}  entryNtl {b.get('entryNtl')}{mark}\n")
    p = s["perp"]
    for k in ("marginSummary", "crossMarginSummary"):
        w(f"perp {k}: {json.dumps(p.get(k), sort_keys=True)}\n")
    w(f"perp crossMaintenanceMarginUsed: {p.get('crossMaintenanceMarginUsed')}  withdrawable: {p.get('withdrawable')}\n")
    w("positions (clearinghouseState.assetPositions):\n")
    n = 0
    for ap in p.get("assetPositions") or []:
        q = ap.get("position") or {}
        if _f(q.get("szi")) == 0:
            continue
        n += 1
        lev = q.get("leverage") or {}
        w(f"  {q.get('coin')}: szi {q.get('szi')}  entryPx {q.get('entryPx')}  positionValue {q.get('positionValue')}  "
          f"unrealizedPnl {q.get('unrealizedPnl')}  marginUsed {q.get('marginUsed')}  "
          f"leverage {lev.get('type')} {lev.get('value')}\n")
    if not n:
        w("  (none: open a small position so the basis can be measured)\n")
    c = s["candidates"]
    w(f"USDC total {s['usdc_total']:.6f}  hold {s['usdc_hold']:.6f}  sum unrealizedPnl {s['upnl']:.6f}\n")
    w(f"candidate equity A ({AM.BASIS_SPOT_TOTAL}): {c[AM.BASIS_SPOT_TOTAL]:.6f}\n")
    w(f"candidate equity B ({AM.BASIS_SPOT_PLUS_UPNL}): {c[AM.BASIS_SPOT_PLUS_UPNL]:.6f}\n")
    if s["mode"] in AM.UNIFIED_MODES:
        u = AM.unified_equity(s["spot"], s["perp"])
        w(f"OMX now uses: {u['basis']} = {u['equity_usd']:.6f} {list(u['flags'])}\n")
    elif s["mode"] in AM.STANDARD_MODES:
        w(f"OMX now uses: {AM.BASIS_PERP} = {(p.get('marginSummary') or {}).get('accountValue')} (standard account)\n")
    else:
        w(f"OMX refuses new orders in this mode ({AM.ACCOUNT_MODE_UNSUPPORTED})\n")
    if raw:
        w("raw spotClearinghouseState: " + json.dumps(s["spot"], sort_keys=True) + "\n")
        w("raw clearinghouseState: " + json.dumps(s["perp"], sort_keys=True) + "\n")


def verdict(samples: Sequence[dict], min_move: float = MIN_UPNL_MOVE) -> tuple[str, list]:
    """(basis or "INCONCLUSIVE", [(d_upnl, d_total, ratio), ...]) from consecutive unified samples."""
    pairs = []
    for a, b in zip(samples, samples[1:]):
        if a["mode"] not in AM.UNIFIED_MODES or b["mode"] not in AM.UNIFIED_MODES:
            continue
        du, dt = b["upnl"] - a["upnl"], b["usdc_total"] - a["usdc_total"]
        if abs(du) >= min_move:
            pairs.append((du, dt, dt / du))
    if not pairs:
        return "INCONCLUSIVE", pairs
    rs = [r for _, _, r in pairs]
    if all(SAME[0] <= r <= SAME[1] for r in rs):
        return AM.BASIS_SPOT_TOTAL, pairs
    if all(NONE_[0] <= r <= NONE_[1] for r in rs):
        return AM.BASIS_SPOT_PLUS_UPNL, pairs
    return "INCONCLUSIVE", pairs


def main(argv: Optional[Sequence[str]] = None, transport: Optional[Callable[[str, dict], object]] = None,
         out: TextIO = sys.stdout, sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help="OMX config (only network and account_address are used)")
    ap.add_argument("--samples", type=int, default=1, help="number of reads (>= 2 for a verdict)")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between reads")
    ap.add_argument("--min-move", type=float, default=MIN_UPNL_MOVE,
                    help="smallest change of unrealized P&L (USDC) used for the verdict")
    ap.add_argument("--raw", action="store_true", help="also print the raw responses")
    ns = ap.parse_args(argv)
    if not 1 <= ns.samples <= 100 or not 1.0 <= ns.interval <= 3600:
        out.write("samples must be 1..100 and interval 1..3600 s\n")
        return 2
    from omx.config import load
    from omx.venue.hyperliquid import MAINNET_API_URL, TESTNET_API_URL, RequestsTransport
    cfg = load(ns.config)                         # never opens the key file
    url = MAINNET_API_URL if cfg.network == "mainnet" else TESTNET_API_URL
    t = transport or RequestsTransport(url)
    info = lambda payload: t("/info", payload)    # noqa: E731
    user = cfg.account_address.lower()
    out.write(f"read-only /info reads of {user} on {cfg.network} ({url}); no key is read, nothing is signed\n")
    got = []
    for i in range(ns.samples):
        if i:
            sleep(ns.interval)
        s = sample(info, user)
        got.append(s)
        show(s, out, ns.raw)
    if ns.samples >= 2:
        basis, pairs = verdict(got, ns.min_move)
        out.write("\n=== verdict ===\n")
        for du, dt, r in pairs:
            out.write(f"  d unrealizedPnl {du:+.6f}  d USDC total {dt:+.6f}  ratio {r:+.3f}\n")
        if basis == AM.BASIS_SPOT_TOTAL:
            out.write(f"USDC total moves with unrealized P&L: basis {basis} (candidate A).\n")
        elif basis == AM.BASIS_SPOT_PLUS_UPNL:
            out.write(f"USDC total does not move with unrealized P&L: basis {basis} (candidate B).\n")
        else:
            out.write("INCONCLUSIVE: need samples whose unrealized P&L moved by at least "
                      f"{ns.min_move} USDC with no fill or funding payment in between; run again.\n")
        if basis != "INCONCLUSIVE":
            out.write("Send this output to the maintainer; omx/account_mode.py VERIFIED_UNIFIED_BASIS is set "
                      "from it and EQUITY_BASIS_UNVERIFIED goes away.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
