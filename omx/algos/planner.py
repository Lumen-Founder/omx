"""Execution algorithms: turn an Intent into child orders over time.

plan() is a pure function of (intent, algo state, context). It proposes at most one new
child per call plus any cancels; the engine applies the returned state only when the
proposed child was actually sent (a blocked or failed child leaves the state unchanged,
so the deficit is retried at the next opportunity).

Sizing is deficit based: target cumulative quantity - filled - still working. Partial fills,
expired IOC remainders and confirmed cancels therefore roll into the next child naturally,
and cancel latency is respected because cancel-pending children still count as working.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional, Union

from ..types import Algo, Intent, Side

EPS = 1e-9
POV_MIN_INTERVAL_MS = 1_000
# Hyperliquid docs (Order types, TWAP): running time 5 minutes .. 7 days, $100 minimum total, suborders at
# least 30 s apart, each limited to 3 % slippage; a suborder that falls behind may grow up to 3x normal size.
NATIVE_TWAP_MIN_S = 5 * 60
NATIVE_TWAP_MAX_S = 7 * 86_400
NATIVE_TWAP_MIN_USD = 100.0
NATIVE_TWAP_SUBORDER_S = 30
NATIVE_TWAP_CATCHUP = 3.0
NATIVE_TWAP_SLIPPAGE_BPS = 300.0


@dataclass(frozen=True)
class Working:
    child_id: str
    px: float
    remaining: float
    tif: str
    cancel_pending: bool = False


@dataclass(frozen=True)
class Ctx:
    now_ms: int
    start_ms: int
    arrival_mid: float
    mid: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    tick: float
    sz_decimals: int
    filled: float = 0.0
    working: tuple[Working, ...] = ()
    market_volume: float = 0.0          # cumulative non-own traded base volume since start
    min_notional_usd: float = 10.0


@dataclass(frozen=True)
class AlgoState:
    children: int = 0                   # children sent so far
    slices_sent: int = 0                # TWAP slice index reached
    last_child_ms: Optional[int] = None


@dataclass(frozen=True)
class Place:
    size: float
    px: float
    tif: str                            # "Ioc" | "Gtc" | "Alo"


@dataclass(frozen=True)
class Cancel:
    child_id: str
    reason: str


Action = Union[Place, Cancel]


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    state: AlgoState
    done: bool = False


# ------------------------------------------------------------------ rounding
def _decimals(tick: float) -> int:
    return max(0, -math.floor(math.log10(tick) + EPS)) + 2


def round_px(px: float, tick: float, mode: str) -> float:
    """mode: 'down' | 'up' | 'near'. Result is an exact multiple of tick."""
    x = px / tick
    n = math.floor(x + EPS) if mode == "down" else math.ceil(x - EPS) if mode == "up" else round(x)
    return round(n * tick, _decimals(tick))


def round_sz(sz: float, sz_decimals: int) -> float:
    return max(0.0, math.floor(sz * 10 ** sz_decimals + EPS) / 10 ** sz_decimals)


def cap_px(intent: Intent, ctx: Ctx) -> float:
    """Aggressive price cap: arrival mid +/- max_slippage, also vs current mid, never past limit_px."""
    s = intent.max_slippage_bps / 1e4
    if intent.side is Side.BUY:
        c = ctx.arrival_mid * (1 + s)
        if ctx.mid:
            c = min(c, ctx.mid * (1 + s))
        if intent.limit_px is not None:
            c = min(c, intent.limit_px)
        return round_px(c, ctx.tick, "down")
    c = ctx.arrival_mid * (1 - s)
    if ctx.mid:
        c = max(c, ctx.mid * (1 - s))
    if intent.limit_px is not None:
        c = max(c, intent.limit_px)
    return round_px(c, ctx.tick, "up")


def touch_px(intent: Intent, ctx: Ctx) -> Optional[float]:
    """Passive price at our side's touch, clamped to limit_px."""
    t = ctx.best_bid if intent.side is Side.BUY else ctx.best_ask
    if t is None:
        return None
    if intent.limit_px is not None:
        t = min(t, intent.limit_px) if intent.side is Side.BUY else max(t, intent.limit_px)
    return round_px(t, ctx.tick, "down" if intent.side is Side.BUY else "up")


# ------------------------------------------------------------------ helpers
def _working_sz(ctx: Ctx) -> float:
    return sum(w.remaining for w in ctx.working)


def _remaining(intent: Intent, ctx: Ctx) -> float:
    return max(0.0, intent.size - ctx.filled)


def _complete(intent: Intent, ctx: Ctx) -> bool:
    return _remaining(intent, ctx) < 10 ** -ctx.sz_decimals - EPS


def _sendable(sz: float, px: float, ctx: Ctx, intent: Intent) -> bool:
    return sz > EPS and (sz * px >= ctx.min_notional_usd or intent.reduce_only)


def _child(intent: Intent, st: AlgoState, ctx: Ctx, sz: float, px: Optional[float], tif: str,
           extra: tuple = (), **upd) -> Plan:
    sz = round_sz(sz, ctx.sz_decimals)
    if px is None or not _sendable(sz, px, ctx, intent):
        return Plan(extra, st)
    st2 = replace(st, children=st.children + 1, last_child_ms=ctx.now_ms, **upd)
    return Plan(extra + (Place(sz, px, tif),), st2)


def _cancel_all(ctx: Ctx, reason: str) -> tuple[Cancel, ...]:
    return tuple(Cancel(w.child_id, reason) for w in ctx.working if not w.cancel_pending)


def _repeg(intent: Intent, ctx: Ctx, target: Optional[float]) -> tuple[Cancel, ...]:
    """Cancel resting passive children that sit at least one tick away from the target price."""
    if target is None:
        return ()
    return tuple(Cancel(w.child_id, "REPRICE") for w in ctx.working
                 if not w.cancel_pending and w.tif != "Ioc" and abs(w.px - target) >= ctx.tick - EPS)


def _end_ms(intent: Intent, ctx: Ctx) -> Optional[int]:
    return ctx.start_ms + intent.duration_s * 1000 if intent.duration_s > 0 else None


def _finish(st: AlgoState, ctx: Ctx, reason: str) -> Plan:
    if ctx.working:
        return Plan(_cancel_all(ctx, reason), st)
    return Plan((), st, done=True)


# ------------------------------------------------------------------ algos
def _market(intent: Intent, st: AlgoState, ctx: Ctx) -> Plan:
    if st.children == 0:
        return _child(intent, st, ctx, _remaining(intent, ctx), cap_px(intent, ctx), "Ioc")
    return Plan((), st, done=not ctx.working)


def _limit(intent: Intent, st: AlgoState, ctx: Ctx) -> Plan:
    end = _end_ms(intent, ctx)
    if st.children and end is not None and ctx.now_ms >= end:
        return _finish(st, ctx, "DURATION_END")
    if st.children == 0:
        px = round_px(intent.limit_px, ctx.tick, "down" if intent.side is Side.BUY else "up")
        return _child(intent, st, ctx, _remaining(intent, ctx), px, "Alo" if intent.post_only else "Gtc")
    return Plan((), st, done=not ctx.working)


def _twap(intent: Intent, st: AlgoState, ctx: Ctx) -> Plan:
    n = max(1, intent.slices)
    dur = max(0, intent.duration_s) * 1000
    interval = dur / n
    end = ctx.start_ms + dur
    if _complete(intent, ctx) or (ctx.now_ms >= end and (st.slices_sent >= n or ctx.now_ms >= end + max(interval, 1000))):
        return _finish(st, ctx, "DURATION_END")
    due = n if interval <= 0 else min(n, int((ctx.now_ms - ctx.start_ms) // interval) + 1)
    if st.slices_sent >= due:
        return Plan((), st)
    target = intent.size * due / n
    deficit = min(target, intent.size) - ctx.filled - _working_sz(ctx)
    if intent.post_only:
        px = touch_px(intent, ctx)
        cancels = _repeg(intent, ctx, px)
        plan = _child(intent, st, ctx, deficit, px, "Alo", cancels, slices_sent=due)
    else:
        plan = _child(intent, st, ctx, deficit, cap_px(intent, ctx), "Ioc", slices_sent=due)
    if not any(isinstance(a, Place) for a in plan.actions):
        # nothing to send for this slice (deficit already working / below minimum): move on
        return Plan(plan.actions, replace(st, slices_sent=due))
    return plan


def _pov(intent: Intent, st: AlgoState, ctx: Ctx) -> Plan:
    end = _end_ms(intent, ctx)
    if _complete(intent, ctx) or (end is not None and ctx.now_ms >= end):
        return _finish(st, ctx, "DURATION_END")
    px = touch_px(intent, ctx) if intent.post_only else cap_px(intent, ctx)
    cancels = _repeg(intent, ctx, px) if intent.post_only else ()
    if st.last_child_ms is not None and ctx.now_ms - st.last_child_ms < POV_MIN_INTERVAL_MS:
        return Plan(cancels, st)
    target = min(intent.size, max(0.0, intent.pov_rate) * ctx.market_volume)
    deficit = target - ctx.filled - _working_sz(ctx)
    if intent.slices > 1:                          # bound one child to size / slices
        deficit = min(deficit, intent.size / intent.slices)
    return _child(intent, st, ctx, deficit, px, "Alo" if intent.post_only else "Ioc", cancels)


def _chase(intent: Intent, st: AlgoState, ctx: Ctx) -> Plan:
    end = _end_ms(intent, ctx)
    if _complete(intent, ctx) or (end is not None and ctx.now_ms >= end):
        return _finish(st, ctx, "DURATION_END")
    px = touch_px(intent, ctx)
    cancels = _repeg(intent, ctx, px)
    if ctx.working:                                # wait for the cancel to take effect first
        return Plan(cancels, st)
    return _child(intent, st, ctx, _remaining(intent, ctx), px, "Alo")


_ALGOS = {Algo.MARKET: _market, Algo.LIMIT: _limit, Algo.TWAP: _twap, Algo.POV: _pov, Algo.CHASE: _chase}


def validate(intent: Intent) -> Optional[str]:
    """Static checks before an intent is accepted; returns a reason or None."""
    if intent.size <= 0:
        return "SIZE_NOT_POSITIVE"
    if intent.algo is Algo.LIMIT and intent.limit_px is None:
        return "LIMIT_NEEDS_LIMIT_PX"
    if intent.algo is Algo.TWAP and (intent.slices < 1 or intent.duration_s < 0):
        return "TWAP_NEEDS_SLICES"
    if intent.algo is Algo.POV and not 0 < intent.pov_rate <= 1:
        return "POV_RATE_OUT_OF_RANGE"
    if intent.algo is Algo.NATIVE_TWAP:
        if not NATIVE_TWAP_MIN_S <= intent.duration_s <= NATIVE_TWAP_MAX_S or intent.duration_s % 60:
            return "NATIVE_TWAP_DURATION: whole minutes, 5 minutes to 7 days"
        if intent.limit_px is not None or intent.post_only:
            return "NATIVE_TWAP_NO_LIMIT_PX: Hyperliquid's twapOrder has no limit price or post-only"
    if intent.max_slippage_bps < 0:
        return "NEGATIVE_SLIPPAGE_CAP"
    return None


def plan(intent: Intent, st: AlgoState, ctx: Ctx) -> Plan:
    if intent.algo is Algo.NATIVE_TWAP:          # the protocol slices it; the engine sends it once
        raise ValueError("native_twap has no OMX child plan")
    return _ALGOS[intent.algo](intent, st, ctx)


def schedule(intent: Intent, ctx: Ctx) -> list[dict]:
    """Static preview of the planned children (no fills assumed, current book)."""
    if intent.algo is Algo.MARKET:
        return [{"t_ms": 0, "size": round_sz(intent.size, ctx.sz_decimals), "px": cap_px(intent, ctx), "tif": "Ioc"}]
    if intent.algo is Algo.LIMIT:
        px = round_px(intent.limit_px, ctx.tick, "down" if intent.side is Side.BUY else "up")
        return [{"t_ms": 0, "size": round_sz(intent.size, ctx.sz_decimals), "px": px,
                 "tif": "Alo" if intent.post_only else "Gtc"}]
    if intent.algo is Algo.TWAP:
        n = max(1, intent.slices)
        step = intent.duration_s * 1000 / n
        px = touch_px(intent, ctx) if intent.post_only else cap_px(intent, ctx)
        out, done = [], 0.0
        for k in range(n):
            sz = round_sz(intent.size * (k + 1) / n - done, ctx.sz_decimals)
            done += sz
            out.append({"t_ms": int(k * step), "size": sz, "px": px, "tif": "Alo" if intent.post_only else "Ioc"})
        return out
    if intent.algo is Algo.NATIVE_TWAP:
        n = max(1, intent.duration_s // NATIVE_TWAP_SUBORDER_S)
        px = round_px(ctx.mid, ctx.tick, "near") if ctx.mid else None
        return [{"t_ms": 0, "size": round_sz(intent.size, ctx.sz_decimals), "px": px, "tif": "Twap",
                 "note": f"one protocol TWAP: about {n} market suborders 30 s apart, each up to "
                         f"{NATIVE_TWAP_SLIPPAGE_BPS:g} bps from the price then; px is the mid now"}]
    if intent.algo is Algo.CHASE:
        return [{"t_ms": 0, "size": round_sz(intent.size, ctx.sz_decimals), "px": touch_px(intent, ctx), "tif": "Alo"}]
    # POV: sizes depend on future volume; show the largest single child the bound allows
    cap = intent.size / intent.slices if intent.slices > 1 else intent.size
    px = touch_px(intent, ctx) if intent.post_only else cap_px(intent, ctx)
    return [{"t_ms": None, "size": round_sz(cap, ctx.sz_decimals), "px": px,
             "tif": "Alo" if intent.post_only else "Ioc", "note": "size depends on observed volume"}]
