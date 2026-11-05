"""Deterministic local simulator implementing the Venue protocol.

Measured inputs (our Hyperliquid ALO path / HL base fee tier):
  place latency 212 ms, cancel latency 478 ms, taker 4.5 bps, maker 1.5 bps.

Everything else is a MODEL ASSUMPTION, not a market fact:
  - mid is a driftless log random walk (vol_bps_sqrt_s per sqrt(second)), sampled every step_ms;
  - the displayed L2 book is regenerated around mid every step (fixed spread in ticks, N levels,
    size growing linearly with depth); depletion by trades lasts until the next regeneration;
  - synthetic taker trades arrive as a Poisson process with exponential sizes and a side bias;
    in toxic mode every taker trade moves mid in its own direction (adverse selection);
  - our resting orders are invisible to the displayed book; a resting order's queue ahead is the
    displayed size at its price when it becomes live (capped later by the displayed size), it
    fills only from taker volume printed at its price beyond that queue (never pro-rata), and
    fully when price trades through its level;
  - our aggressive orders walk only the displayed book (no self-trade, no hidden liquidity,
    no market impact beyond depletion of the current step);
  - actions that become due inside one step are one "block": cancels, then ALO, then IOC/GTC;
  - no margin, funding, liquidation or 5-significant-figure price rule.

Action expiry (R3-4): like HL's expiresAfter, every place carries expires = now + order_expiry_ms;
a request that reaches the venue after that is discarded and leaves no order (order_status
"unknown"). Every VenueAmbiguous carries expires_after_ms.

Test hooks: fail_next_places (definite error), lose_next_place_responses (accepted, then the
response is lost -> VenueAmbiguous), drop_next_places (never arrives -> VenueAmbiguous),
delay_next_places (arrives after a delay, or never if it expired on the way -> VenueAmbiguous),
deposit / withdraw (external flows), place_foreign (an order without an OMX cloid).

Builder fee: set_builder((address, f)) (the engine does this) makes every order carry that builder;
an order whose ChildOrder.builder differs is refused (BUILDER_MISMATCH). Each fill reports
builder_fee_usd = notional * f / 1e5 (f in tenths of a bps) separately from the venue fee.
approve_builder_fee(address, f) is the user's approval as maxBuilderFee reports it (default 0); with
SimConfig.require_builder_approval an order whose f exceeds it is rejected like on Hyperliquid.
set_builder_fee(f) is the older fee-only form (no address).
"""
from __future__ import annotations

import bisect
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..types import AccountState, BookLevel, ChildOrder, Fill, MarketSnapshot, OpenOrder, Position, Side
from .base import VenueAborted, VenueAmbiguous, VenueError, cloid_for, is_perp_coin  # noqa: F401

EPS = 1e-9


@dataclass
class CoinSpec:
    coin: str
    mid: float
    tick: float
    sz_decimals: int
    vol_bps_sqrt_s: float = 1.0      # sd of log mid per sqrt(second), bps
    spread_ticks: int = 1
    levels: int = 10
    level_sz: float = 5.0            # base size at the touch
    level_growth: float = 0.5        # level k holds level_sz * (1 + growth * k)
    trade_rate_hz: float = 3.0       # Poisson arrivals of synthetic taker trades
    trade_sz_mean: float = 0.5       # exponential sizes, base units
    buy_prob: float = 0.5            # side bias of synthetic takers
    toxic_bps: float = 0.0           # post-trade mid move in the trade direction, per mean-size trade
    day_ntl_usd: float = 1e9


def default_coins() -> tuple[CoinSpec, ...]:
    return (CoinSpec("BTC", 100_000.0, 1.0, 5, level_sz=0.2, trade_sz_mean=0.02),
            CoinSpec("ETH", 3_000.0, 0.1, 4),
            CoinSpec("SOL", 150.0, 0.01, 2, level_sz=100.0, trade_sz_mean=10.0))


@dataclass
class SimConfig:
    seed: int = 7
    start_ms: int = 1_800_000_000_000
    step_ms: int = 100
    place_latency_ms: int = 212
    cancel_latency_ms: int = 478
    taker_fee_bps: float = 4.5
    maker_fee_bps: float = 1.5
    builder_fee_tenths_bps: int = 0
    start_equity_usd: float = 10_000.0
    min_notional_usd: float = 10.0
    stale_ms: int = 0
    history_ms: int = 600_000
    order_expiry_ms: int = 3_000         # expiresAfter of each place request (R3-4)
    require_builder_approval: bool = False   # reject orders whose builder fee exceeds the approval


@dataclass(frozen=True)
class Trade:
    coin: str
    ts_ms: int
    px: float
    sz: float
    side: Side                       # taker side
    own: bool = False                # our order took part (maker or taker)


@dataclass
class _Order:
    oid: str
    child: ChildOrder
    px_idx: int
    rem: float
    seq: int
    status: str = "pending"          # pending | live | filled | cancelled | rejected | expired
    queue: float = 0.0
    cancel_requested: bool = False
    reason: str = ""
    cloid: str = ""
    builder: Optional[tuple] = None      # (address, f) this order carries


_PRIO = {"cancel": 0, "Alo": 1}


class SimVenue:
    name = "sim"

    def __init__(self, cfg: Optional[SimConfig] = None, coins: Optional[tuple[CoinSpec, ...]] = None):
        self.cfg = cfg or SimConfig()
        self.specs = {s.coin: s for s in (coins or default_coins())}
        self.rng = random.Random(self.cfg.seed)
        self.stale_ms = self.cfg.stale_ms
        self.builder_fee_tenths_bps = self.cfg.builder_fee_tenths_bps
        self.builder: Optional[tuple] = None
        self._builder_approved: dict[str, int] = {}
        self.builder_checks = 0
        self._now = self.cfg.start_ms
        self._seq = 0
        self._mid = {c: s.mid for c, s in self.specs.items()}
        self._book: dict[str, tuple[list, list]] = {}
        self._orders: dict[str, _Order] = {}
        self._child_oid: dict[str, str] = {}
        self._due: list[tuple[int, int, int, str, str]] = []   # (eff_ms, prio, seq, kind, oid)
        self._fills: list[Fill] = []
        self._fill_ts: list[int] = []
        self._tape: dict[str, list[Trade]] = {c: [] for c in self.specs}
        self._tape_ts: dict[str, list[int]] = {c: [] for c in self.specs}
        self._mid_ts: dict[str, list[int]] = {c: [] for c in self.specs}
        self._mid_px: dict[str, list[float]] = {c: [] for c in self.specs}
        self._hist: dict[str, deque] = {c: deque(maxlen=max(2, self.cfg.history_ms // self.cfg.step_ms + 2))
                                        for c in self.specs}
        self.cash = self.cfg.start_equity_usd
        self._pos: dict[str, list] = {}           # coin -> [size, entry_px]
        self.place_calls = 0
        self.cancel_calls = 0
        self._fail_places = 0
        self._lose_responses = 0
        self._drop_places = 0
        self._delay_places: list[int] = []
        self._inflight: list[tuple[int, int, ChildOrder, str]] = []   # (arrive ms, expires ms, child, cloid)
        self._flows: list[tuple[int, float]] = []
        self.events: deque = deque(maxlen=20_000)  # (ts, kind, child_id) processing log
        for c in sorted(self.specs):
            self._regen(c)
            self._record(c)

    # ---------------------------------------------------------------- helpers
    def _px(self, coin: str, idx: int) -> float:
        t = self.specs[coin].tick
        return round(idx * t, max(0, -math.floor(math.log10(t))) + 2)

    def _lot(self, coin: str) -> float:
        return 10.0 ** -self.specs[coin].sz_decimals

    def _floor_lot(self, coin: str, x: float) -> float:
        d = self.specs[coin].sz_decimals
        return max(0.0, math.floor(x * 10 ** d + EPS) / 10 ** d)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _regen(self, coin: str) -> None:
        s = self.specs[coin]
        bb = math.floor(self._mid[coin] / s.tick - s.spread_ticks / 2.0 + EPS)
        ba = bb + s.spread_ticks
        szs = [round(s.level_sz * (1 + s.level_growth * k), s.sz_decimals) for k in range(s.levels)]
        self._book[coin] = ([[bb - k, z] for k, z in enumerate(szs)], [[ba + k, z] for k, z in enumerate(szs)])

    def _record(self, coin: str) -> None:
        bids, asks = self._book[coin]
        self._hist[coin].append((self._now, tuple((i, z) for i, z in bids if z > EPS),
                                 tuple((i, z) for i, z in asks if z > EPS), self._mid[coin]))
        self._mid_ts[coin].append(self._now)
        self._mid_px[coin].append(self._mid[coin])

    def _best(self, coin: str) -> tuple[Optional[int], Optional[int]]:
        bids, asks = self._book[coin]
        bb = next((i for i, z in bids if z > EPS), None)
        ba = next((i for i, z in asks if z > EPS), None)
        return bb, ba

    def _live(self, coin: str, side: Side) -> list[_Order]:
        return sorted((o for o in self._orders.values()
                       if o.status == "live" and o.child.coin == coin and o.child.side is side and o.rem > EPS),
                      key=lambda o: (o.px_idx * -side.sign, o.seq))

    # ---------------------------------------------------------------- fills / account
    def _fill(self, o: _Order, sz: float, px_idx: int, crossed: bool) -> None:
        c = o.child.coin
        px = self._px(c, px_idx)
        ntl = sz * px
        fee = ntl * (self.cfg.taker_fee_bps if crossed else self.cfg.maker_fee_bps) / 1e4
        f = o.builder[1] if o.builder is not None else self.builder_fee_tenths_bps
        bfee = ntl * f / 1e5
        self.cash -= fee + bfee
        q = sz * o.child.side.sign
        pos, entry = self._pos.get(c, [0.0, 0.0])
        new = round(pos + q, 12)
        if abs(pos) < EPS or (pos > 0) == (q > 0):
            entry = (pos * entry + q * px) / new if abs(new) > EPS else 0.0
        else:
            closed = min(abs(q), abs(pos))
            self.cash += closed * (px - entry) * (1 if pos > 0 else -1)
            if abs(q) > abs(pos) + EPS:
                entry = px
            elif abs(new) < EPS:
                entry = 0.0
        self._pos[c] = [new, entry]
        o.rem = round(o.rem - sz, self.specs[c].sz_decimals)
        if o.rem <= EPS:
            o.rem = 0.0
            o.status = "filled"
        self._fills.append(Fill(o.child.child_id, c, o.child.side, sz, px, fee, bfee, self._now, crossed,
                                fill_id=f"t{len(self._fills) + 1}"))
        self._fill_ts.append(self._now)
        if crossed:
            self._print(c, px, sz, o.child.side, True)

    def _print(self, coin: str, px: float, sz: float, side: Side, own: bool) -> None:
        if sz <= EPS:
            return
        self._tape[coin].append(Trade(coin, self._now, px, sz, side, own))
        self._tape_ts[coin].append(self._now)

    def _through(self, coin: str) -> None:
        """Price traded through a resting level: fill the remainder at the order's price."""
        bb, ba = self._best(coin)
        for o in self._live(coin, Side.BUY):
            if ba is not None and ba <= o.px_idx:
                self._print(coin, self._px(coin, o.px_idx), o.rem, Side.SELL, True)
                self._fill(o, o.rem, o.px_idx, False)
        for o in self._live(coin, Side.SELL):
            if bb is not None and bb >= o.px_idx:
                self._print(coin, self._px(coin, o.px_idx), o.rem, Side.BUY, True)
                self._fill(o, o.rem, o.px_idx, False)

    # ---------------------------------------------------------------- matching
    def _take(self, o: _Order) -> None:
        """Aggressive part of IOC/GTC: walk the displayed book up to the order price."""
        c = o.child.coin
        bids, asks = self._book[c]
        levels = asks if o.child.side is Side.BUY else bids
        for lv in levels:
            if o.rem <= EPS:
                break
            if (o.child.side is Side.BUY and lv[0] > o.px_idx) or (o.child.side is Side.SELL and lv[0] < o.px_idx):
                break
            f = self._floor_lot(c, min(o.rem, lv[1]))
            if f <= EPS:
                continue
            lv[1] = round(lv[1] - f, 12)
            self._fill(o, f, lv[0], True)

    def _rest(self, o: _Order) -> None:
        bids, asks = self._book[o.child.coin]
        same = bids if o.child.side is Side.BUY else asks
        o.queue = next((z for i, z in same if i == o.px_idx), 0.0)
        o.status = "live"

    def _activate(self, o: _Order) -> None:
        c, side, tif = o.child.coin, o.child.side, o.child.tif
        if o.child.reduce_only:
            pos = self._pos.get(c, [0.0, 0.0])[0]
            if abs(pos) < EPS or (pos > 0) == (side is Side.BUY):
                o.status, o.reason = "rejected", "REDUCE_ONLY"
                return
            o.rem = min(o.rem, self._floor_lot(c, abs(pos)))
        bb, ba = self._best(c)
        crosses = (side is Side.BUY and ba is not None and o.px_idx >= ba) or \
                  (side is Side.SELL and bb is not None and o.px_idx <= bb)
        if tif == "Alo":
            if crosses:
                o.status, o.reason = "rejected", "ALO_WOULD_CROSS"
            else:
                self._rest(o)
            return
        if crosses:
            self._take(o)
        if o.rem <= EPS:
            return
        if tif == "Ioc":
            o.status, o.reason = "expired", "IOC_REMAINDER"
        else:
            self._rest(o)

    def _taker_trade(self, coin: str, side: Side, size: float) -> None:
        """One synthetic taker trade against the displayed book plus our resting orders."""
        bids, asks = self._book[coin]
        syn = asks if side is Side.BUY else bids
        ours = self._live(coin, Side.SELL if side is Side.BUY else Side.BUY)
        prices = sorted({i for i, z in syn if z > EPS} | {o.px_idx for o in ours}, key=lambda i: i * side.sign)
        left = size
        for p in prices:
            if left <= EPS:
                break
            lv = next((x for x in syn if x[0] == p), None)
            d = lv[1] if lv else 0.0
            at = [o for o in ours if o.px_idx == p and o.rem > EPS]
            total = d + sum(o.rem for o in at)
            if total <= EPS:
                continue
            if left >= total - EPS:          # level swept: everyone at this price fills
                mine = sum(o.rem for o in at)
                for o in at:
                    self._fill(o, o.rem, p, False)
                if lv:
                    lv[1] = 0.0
                self._print(coin, self._px(coin, p), d, side, False)
                self._print(coin, self._px(coin, p), mine, side, True)
                left -= total
                continue
            v, used = left, 0.0
            for o in sorted(at, key=lambda o: o.seq):
                f = self._floor_lot(coin, min(o.rem, max(0.0, v - min(o.queue, d) - used)))
                o.queue = max(0.0, o.queue - v)
                if f > EPS:
                    self._fill(o, f, p, False)
                    used += f
            if lv:
                lv[1] = max(0.0, round(lv[1] - (v - used), 12))
            self._print(coin, self._px(coin, p), v - used, side, False)
            self._print(coin, self._px(coin, p), used, side, True)
            left = 0.0
        s = self.specs[coin]
        if s.toxic_bps:
            self._mid[coin] *= math.exp(side.sign * s.toxic_bps / 1e4 * size / s.trade_sz_mean)

    def _poisson(self, lam: float) -> int:
        lim, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= lim:
                return k
            k += 1

    def _arrivals(self) -> None:
        """Delayed place requests that reach the venue now; an expired one leaves no order."""
        due = [x for x in self._inflight if x[0] <= self._now]
        if not due:
            return
        self._inflight = [x for x in self._inflight if x[0] > self._now]
        for arrive, expires, child, cloid in due:
            if arrive > expires:
                self.events.append((self._now, "expired_action", child.child_id))
            elif child.child_id in self._child_oid:
                self.events.append((self._now, "duplicate_cloid", child.child_id))
            else:
                self._accept(child, cloid)

    def _process_due(self) -> None:
        self._arrivals()
        due = [d for d in self._due if d[0] <= self._now]
        if not due:
            return
        self._due = [d for d in self._due if d[0] > self._now]
        for _, _, _, kind, oid in sorted(due, key=lambda d: (d[1], d[2])):
            o = self._orders[oid]
            if kind == "cancel":
                self.events.append((self._now, "cancel", o.child.child_id))
                if o.status in ("pending", "live"):
                    o.status, o.reason = "cancelled", "USER_CANCEL"
            elif o.status == "pending":
                self.events.append((self._now, o.child.tif, o.child.child_id))
                self._activate(o)

    def _tick(self, dt: int) -> None:
        coins = sorted(self.specs)
        for c in coins:
            s = self.specs[c]
            self._mid[c] *= math.exp(s.vol_bps_sqrt_s / 1e4 * math.sqrt(dt / 1000.0) * self.rng.gauss(0.0, 1.0))
            self._regen(c)
            self._through(c)
        self._process_due()
        for c in coins:
            s = self.specs[c]
            before = self._mid[c]
            for _ in range(self._poisson(s.trade_rate_hz * dt / 1000.0)):
                side = Side.BUY if self.rng.random() < s.buy_prob else Side.SELL
                sz = max(self._lot(c), self._floor_lot(c, self.rng.expovariate(1.0 / s.trade_sz_mean)))
                self._taker_trade(c, side, sz)
            if self._mid[c] != before:
                self._regen(c)
                self._through(c)
            self._record(c)

    # ---------------------------------------------------------------- Venue protocol
    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        end = self._now + int(ms)
        while self._now < end:
            dt = min(self.cfg.step_ms, end - self._now)
            self._now += dt
            self._tick(dt)

    def snapshot(self, coin: str) -> MarketSnapshot:
        if coin not in self.specs:
            raise VenueError(f"unknown coin {coin}")
        hist = self._hist[coin]
        rec = hist[-1]
        if self.stale_ms > 0:
            target = self._now - self.stale_ms
            rec = next((h for h in reversed(hist) if h[0] <= target), hist[0])
        ts, bids, asks, mid = rec
        return MarketSnapshot(coin, ts, tuple(BookLevel(self._px(coin, i), z) for i, z in bids),
                              tuple(BookLevel(self._px(coin, i), z) for i, z in asks),
                              mark_px=mid, day_ntl_usd=self.specs[coin].day_ntl_usd)

    def equity(self) -> float:
        return self.cash + sum(sz * (self._mid[c] - e) for c, (sz, e) in self._pos.items())

    def account(self) -> AccountState:
        pos = tuple(Position(c, sz, e) for c, (sz, e) in sorted(self._pos.items()) if abs(sz) > EPS)
        oo = tuple(OpenOrder(o.child.child_id, o.child.coin, o.child.side, o.rem, o.child.px, o.child.reduce_only,
                             o.oid, o.cloid)
                   for o in sorted(self._orders.values(), key=lambda o: o.seq) if o.status in ("pending", "live"))
        return AccountState(self._now, self.equity(), pos, oo)

    def place(self, child: ChildOrder, abort: Optional[Callable[[], bool]] = None, _cloid: Optional[str] = None) -> str:
        if abort is not None and abort():
            raise VenueAborted("ABORTED_BEFORE_SEND")
        self.place_calls += 1
        expires = self._now + self.cfg.order_expiry_ms
        cloid = (child.cloid or cloid_for(child.child_id)) if _cloid is None else _cloid
        if self._fail_places > 0:
            self._fail_places -= 1
            raise VenueError("injected venue error")
        if self._drop_places > 0:
            self._drop_places -= 1
            raise VenueAmbiguous("injected: request lost before the venue", expires)
        if self._delay_places:
            self._validate(child)
            self._inflight.append((self._now + self._delay_places.pop(0), expires, child, cloid))
            raise VenueAmbiguous("injected: request delayed in transit", expires)
        oid = self._accept(child, cloid)
        if self._lose_responses > 0:
            self._lose_responses -= 1
            raise VenueAmbiguous("injected: accepted, response lost", expires)
        return oid

    def _validate(self, child: ChildOrder) -> None:
        s = self.specs.get(child.coin)
        if s is None:
            raise VenueError(f"unknown coin {child.coin}")
        if child.child_id in self._child_oid:
            raise VenueError(f"duplicate child id {child.child_id}")
        if child.tif not in ("Ioc", "Gtc", "Alo"):
            raise VenueError(f"bad tif {child.tif}")
        if child.size <= 0 or abs(child.size * 10 ** s.sz_decimals - round(child.size * 10 ** s.sz_decimals)) > 1e-6:
            raise VenueError(f"bad size {child.size}")
        idx = round(child.px / s.tick)
        if child.px <= 0 or abs(child.px / s.tick - idx) > 1e-6:
            raise VenueError(f"price {child.px} not on tick {s.tick}")
        if child.size * child.px < self.cfg.min_notional_usd and not child.reduce_only:
            raise VenueError("below minimum notional")
        b = self._builder_for(child)
        if b is not None and self.cfg.require_builder_approval and self._builder_approved.get(b[0], 0) < b[1]:
            raise VenueError("Builder fee has not been approved.")

    def _builder_for(self, child: ChildOrder) -> Optional[tuple]:
        theirs = getattr(child, "builder", None)
        if theirs is not None and (str(theirs[0]).lower(), int(theirs[1])) != self.builder:
            raise VenueError(f"BUILDER_MISMATCH: child carries {theirs}, the venue is configured with {self.builder}")
        return self.builder

    def _accept(self, child: ChildOrder, cloid: str) -> str:
        self._validate(child)
        s = self.specs[child.coin]
        oid = f"s{len(self._orders) + 1}"
        seq = self._next_seq()
        self._orders[oid] = _Order(oid, child, round(child.px / s.tick), child.size, seq, cloid=cloid,
                                   builder=self._builder_for(child))
        self._child_oid[child.child_id] = oid
        self._due.append((self._now + self.cfg.place_latency_ms, _PRIO.get(child.tif, 2), seq, "place", oid))
        return oid

    def place_foreign(self, child: ChildOrder) -> str:
        """Test hook: an order placed outside OMX (no OMX cloid)."""
        return self.place(child, _cloid="")

    def cancel(self, coin: str, venue_oid: str) -> bool:
        self.cancel_calls += 1
        o = self._orders.get(venue_oid)
        if o is None or o.child.coin != coin or o.status not in ("pending", "live"):
            return False
        if not o.cancel_requested:
            o.cancel_requested = True
            self._due.append((self._now + self.cfg.cancel_latency_ms, 0, self._next_seq(), "cancel", venue_oid))
        return True

    def fills_since(self, ts_ms: int) -> list[Fill]:
        return self._fills[bisect.bisect_left(self._fill_ts, ts_ms):]

    def tick_size(self, coin: str) -> float:
        return self.specs[coin].tick

    def size_decimals(self, coin: str) -> int:
        return self.specs[coin].sz_decimals

    # ---------------------------------------------------------------- optional extras used by engine / TCA
    def fee_bps(self) -> dict:
        return {"taker": self.cfg.taker_fee_bps, "maker": self.cfg.maker_fee_bps}

    def set_builder_fee(self, tenths_bps: int) -> None:
        self.builder_fee_tenths_bps = int(tenths_bps)

    def set_builder(self, builder: Optional[tuple]) -> None:
        """(address, f) that every order carries from now on; None = no builder code."""
        if builder is None:
            self.builder, self.builder_fee_tenths_bps = None, 0
            return
        addr, fee = builder
        self.builder = (str(addr).lower(), int(fee))
        self.builder_fee_tenths_bps = self.builder[1]

    def approve_builder_fee(self, builder: str, tenths_bps: int) -> None:
        """Test hook: the user's main wallet approved this max fee for the builder (0 = revoked)."""
        self._builder_approved[str(builder).lower()] = int(tenths_bps)

    def max_builder_fee(self, builder: Optional[str] = None) -> int:
        """Like /info maxBuilderFee: approved max in tenths of a bps, 0 = not approved."""
        self.builder_checks += 1
        b = (builder or (self.builder or ("",))[0]).lower()
        return self._builder_approved.get(b, 0)

    def order_status(self, key: str) -> dict:
        """By child_id (contract) or by venue oid. "state" follows the Venue contract; "status"
        is the simulator's own finer status (pending | live | filled | cancelled | rejected | expired)."""
        o = self._orders.get(self._child_oid.get(key, key))
        if o is None:
            return {"state": "unknown", "filled_sz": 0.0, "oid": None, "status": "unknown"}
        state = {"pending": "open", "live": "open", "expired": "cancelled"}.get(o.status, o.status)
        return {"state": state, "filled_sz": round(o.child.size - o.rem, 12), "oid": o.oid,
                "status": o.status, "reason": o.reason, "remaining": o.rem, "queue": o.queue,
                "builder": o.builder}

    def mark_prices(self) -> dict[str, float]:
        return dict(self._mid)

    def external_flow_events(self, start_ms: int) -> list[tuple[int, float]]:
        return [(t, u) for t, u in self._flows if t >= start_ms]

    def external_flows_usd(self, start_ms: int) -> float:
        return sum(u for _, u in self.external_flow_events(start_ms))

    def trades_since(self, coin: str, ts_ms: int) -> list[Trade]:
        return self._tape[coin][bisect.bisect_left(self._tape_ts[coin], ts_ms):]

    def trades_between(self, coin: str, t0: int, t1: int) -> list[Trade]:
        ts = self._tape_ts[coin]
        return self._tape[coin][bisect.bisect_left(ts, t0):bisect.bisect_right(ts, t1)]

    def mid_at(self, coin: str, ts_ms: int) -> Optional[float]:
        """Mid at or before ts_ms; None for the future (markouts must wait)."""
        if ts_ms > self._now:
            return None
        i = bisect.bisect_right(self._mid_ts[coin], ts_ms) - 1
        return self._mid_px[coin][i] if i >= 0 else None

    def fail_next_places(self, n: int) -> None:
        self._fail_places = int(n)

    def lose_next_place_responses(self, n: int) -> None:
        """The venue accepts the next n orders but the caller never gets the response."""
        self._lose_responses = int(n)

    def drop_next_places(self, n: int) -> None:
        """The next n place requests never reach the venue; the caller sees an ambiguous error."""
        self._drop_places = int(n)

    def delay_next_places(self, *delays_ms: int) -> None:
        """The next place requests reach the venue after these delays (the caller sees an ambiguous
        error at once); a request arriving after its expiresAfter is discarded."""
        self._delay_places.extend(int(d) for d in delays_ms)

    def perp_coins(self) -> frozenset:
        return frozenset(self.specs)

    def deposit(self, usd: float) -> None:
        self.cash += float(usd)
        self._flows.append((self._now, float(usd)))

    def withdraw(self, usd: float) -> None:
        self.cash -= float(usd)
        self._flows.append((self._now, -float(usd)))

    def inject_trade(self, coin: str, side: Side, size: float) -> None:
        """Test hook: one synthetic taker trade now (toxic move included); book refreshed after."""
        before = self._mid[coin]
        self._taker_trade(coin, side, size)
        if self._mid[coin] != before:
            self._regen(coin)
            self._through(coin)

    def shock_mid(self, coin: str, bps: float) -> None:
        """Test hook: jump mid by bps now (book regenerated, resting levels traded through)."""
        self._mid[coin] *= math.exp(bps / 1e4)
        self._regen(coin)
        self._through(coin)
        self._record(coin)
