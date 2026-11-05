"""Transaction cost analysis for one intent.

Sign convention: shortfall / vs-VWAP numbers are costs, + = worse for the user.
Markouts are the opposite: + = mid moved in the user's favour after the fill, so a
negative markout on passive fills is the footprint of adverse selection.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from .types import Fill, Intent, Side

DEFAULT_HORIZONS_MS = (5_000, 60_000)


def avg_px(fills: Iterable[Fill]) -> Optional[float]:
    fills = list(fills)
    q = sum(f.size for f in fills)
    return sum(f.size * f.px for f in fills) / q if q > 0 else None


def shortfall_bps(side: Side, fills: Iterable[Fill], arrival_mid: Optional[float],
                  include_fees: bool = True) -> Optional[float]:
    """Implementation shortfall vs arrival mid on the filled quantity, fees included by default."""
    fills = list(fills)
    q = sum(f.size for f in fills)
    if q <= 0 or not arrival_mid:
        return None
    cost = side.sign * (sum(f.size * f.px for f in fills) - q * arrival_mid)
    if include_fees:
        cost += sum(f.fee_usd + f.builder_fee_usd for f in fills)
    return cost / (q * arrival_mid) * 1e4


def _vwap(trades, exclude_own: bool) -> Optional[float]:
    q = n = 0.0
    for t in trades or ():
        if exclude_own and getattr(t, "own", False):
            continue
        q += t.sz
        n += t.sz * t.px
    return n / q if q > 0 else None


def markouts(fills: Iterable[Fill], side: Side, mid_at: Optional[Callable[[str, int], Optional[float]]],
             horizons_ms=DEFAULT_HORIZONS_MS) -> dict:
    """Notional-weighted post-fill markout per horizon; fills whose horizon is in the future are skipped."""
    out = {}
    fills = list(fills)
    for h in horizons_ms:
        w = acc = 0.0
        n = 0
        for f in fills:
            m = mid_at(f.coin, f.ts_ms + h) if mid_at else None
            if m is None:
                continue
            ntl = f.size * f.px
            acc += ntl * side.sign * (m - f.px) / f.px * 1e4
            w += ntl
            n += 1
        out[str(h)] = {"bps": acc / w if w > 0 else None, "n_fills": n, "n_pending": len(fills) - n}
    return out


def build_report(intent: Intent, fills: Iterable[Fill], arrival_mid: Optional[float], arrival_ms: int,
                 end_ms: Optional[int] = None, trades=None, mid_at=None,
                 horizons_ms=DEFAULT_HORIZONS_MS, children_sent: int = 0, children_blocked: int = 0) -> dict:
    fills = sorted(fills, key=lambda f: f.ts_ms)
    filled = sum(f.size for f in fills)
    ap = avg_px(fills)
    exch = sum(f.fee_usd for f in fills)
    bld = sum(f.builder_fee_usd for f in fills)
    maker_ntl = sum(f.size * f.px for f in fills if not f.crossed)
    taker_ntl = sum(f.size * f.px for f in fills if f.crossed)
    ntl = maker_ntl + taker_ntl
    vwap = _vwap(trades, exclude_own=True)
    last = fills[-1].ts_ms if fills else None
    rep = {
        "intent_id": intent.intent_id, "coin": intent.coin, "side": intent.side.value, "algo": intent.algo.value,
        "size": intent.size, "filled_size": filled, "fill_ratio": filled / intent.size if intent.size else None,
        "avg_px": ap, "arrival_mid": arrival_mid,
        "shortfall_bps": shortfall_bps(intent.side, fills, arrival_mid, include_fees=True),
        "price_shortfall_bps": shortfall_bps(intent.side, fills, arrival_mid, include_fees=False),
        "fee_bps": (exch + bld) / ntl * 1e4 if ntl > 0 else None,
        "interval_vwap": vwap,
        "vs_vwap_bps": intent.side.sign * (ap - vwap) / vwap * 1e4 if ap and vwap else None,
        "fees": {"exchange_usd": exch, "builder_usd": bld, "total_usd": exch + bld,
                 "maker_notional_usd": maker_ntl, "taker_notional_usd": taker_ntl,
                 "maker_share": maker_ntl / ntl if ntl > 0 else None},
        "n_fills": len(fills), "children_sent": children_sent, "children_blocked": children_blocked,
        "arrival_ms": arrival_ms, "end_ms": end_ms,
        "time_to_complete_ms": (last - arrival_ms) if last is not None else None,
        "markouts": markouts(fills, intent.side, mid_at, horizons_ms),
    }
    return rep


def summary(rep: dict) -> str:
    def f(x, nd=2):
        return "n/a" if x is None else f"{x:.{nd}f}"
    mk = ", ".join(f"{int(h) // 1000}s {f(v['bps'])}bps" for h, v in rep["markouts"].items())
    return (f"{rep['intent_id']} {rep['side']} {rep['coin']} {rep['algo']}: filled {rep['filled_size']:g}/{rep['size']:g} "
            f"({f((rep['fill_ratio'] or 0) * 100, 1)}%) avg {f(rep['avg_px'], 4)} vs arrival {f(rep['arrival_mid'], 4)}; "
            f"shortfall {f(rep['shortfall_bps'])}bps (price {f(rep['price_shortfall_bps'])}, fees {f(rep['fee_bps'])}); "
            f"vs VWAP {f(rep['vs_vwap_bps'])}bps; fees exch {rep['fees']['exchange_usd']:.4f} + builder "
            f"{rep['fees']['builder_usd']:.4f} USD, maker share {f(rep['fees']['maker_share'], 2)}; "
            f"time {rep['time_to_complete_ms']} ms; markout {mk}")
