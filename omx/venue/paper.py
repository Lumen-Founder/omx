"""Paper venue: simulated fills against Hyperliquid's live PUBLIC order books.

PaperVenue implements the same Venue protocol as omx.venue.sim.SimVenue and the Hyperliquid
adapter, so the production Engine runs on it unchanged. The account (cash, positions, orders,
fills) exists only in this process. Market data comes from HLPublicFeed, which reads three
public, unauthenticated /info request types and nothing else.

Why this cannot trade for real:
  - every request goes through PublicInfoOnly, which refuses any path other than "/info" and any
    request type other than l2Book / allMids / metaAndAssetCtxs (no "user" field is ever accepted),
    so there is no /exchange call, no account read and nothing to sign;
  - this module builds no signer and no HyperliquidVenue; from omx.venue.hyperliquid it uses only
    plain functions and the HTTP transport class (round_px, round_sz, RequestsTransport, TokenBucket);
  - orders, fills and balances are Python objects in this process.

Data reads (HL docs, "Rate limits and user limits": 1200 weight per minute per IP; l2Book and
allMids weigh 2, metaAndAssetCtxs 20):
  - l2Book per coin, only when that coin is quoted, traded or has a working order, at most once per
    BOOK_MIN_S; a book received more than BOOK_STALE_S ago is not served (MARKET_DATA_UNAVAILABLE);
  - allMids at most once per MIDS_MIN_S, only while the paper account has a position or an order
    (marks for equity / the policy's account exposure);
  - metaAndAssetCtxs (szDecimals, 24 h notional, mark) at most once per META_TTL_S; an older copy
    keeps being used if a refresh fails (szDecimals rarely change);
  - every failure starts an exponential backoff for that request (BACKOFF_BASE_S doubling up to
    BACKOFF_MAX_S) during which nothing is sent; the weight bucket (WEIGHT_PER_MINUTE) is a second cap.
  With the default three agent coins the worst case is about 360 + 24 + 20 = ~400 weight per minute,
  a third of the per-IP limit; idle, it sends nothing.

Fill model (MODEL ASSUMPTIONS, not Hyperliquid's matching engine):
  - fees: Hyperliquid perps base tier (tier 0, no staking discount, no referral): taker 0.045 %,
    maker 0.015 %. Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees (checked
    2026-11-05). No builder fee.
  - latency: a place takes effect PLACE_LATENCY_MS after it is sent, a cancel CANCEL_LATENCY_MS (the
    latencies the simulator uses, measured on our Hyperliquid ALO path); the order meets the book read
    at that time (at most BOOK_MIN_S old).
  - prices are moved to the venue grid toward the passive side and sizes floored to szDecimals,
    exactly as the live adapter does; orders under 10 USD (not reduce-only) are refused like on HL.
  - aggressive (Ioc / Gtc crossing) orders walk the displayed levels up to their limit price, each
    level at its own price, taker fee. Liquidity we took from a book stays taken until a NEWER book
    (different HL time) arrives; then the book is assumed refilled (no lasting market impact, no
    hidden liquidity, no other traders reacting). Ioc remainder: expired (IOC_REMAINDER). Gtc
    remainder: rests.
  - Alo that would cross: rejected (ALO_WOULD_CROSS), like HL.
  - resting orders: we assume we are LAST in the queue at our price and that the queue never
    shrinks by cancels, so a resting order fills only when the book trades THROUGH its price: best
    ask strictly below a buy's price, best bid strictly above a sell's price. It then fills in full
    at its own price, maker fee. Touching the price is not a fill. Moves that happen and revert
    between two book reads (about one second apart) are missed (conservative: fewer fills).
  - no funding, margin, liquidation, self-trade or trades feed (so no POV algo: the engine refuses it
    with POV_NEEDS_TRADE_FEED).
"""
from __future__ import annotations

import bisect
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..types import AccountState, BookLevel, ChildOrder, Fill, MarketSnapshot, OpenOrder, Position, Side
from .base import VenueAborted, VenueError, VenueUnknownAsset, cloid_for
from .hyperliquid import MAINNET_API_URL, RequestsTransport, TokenBucket, round_px, round_sz

EPS = 1e-9
PUBLIC_INFO_TYPES = {"l2Book": frozenset({"type", "coin"}), "allMids": frozenset({"type"}),
                     "metaAndAssetCtxs": frozenset({"type"})}
INFO_WEIGHT = {"l2Book": 2, "allMids": 2, "metaAndAssetCtxs": 20}
WEIGHT_PER_MINUTE = 1200
BOOK_MIN_S = 1.0
BOOK_STALE_S = 3.0              # = PolicyConfig.max_data_age_ms; older books are not served
MIDS_MIN_S = 5.0
META_TTL_S = 60.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
HTTP_TIMEOUT_S = 3.0
USER_AGENT = "omx-paper"
TAKER_FEE_BPS = 4.5             # HL perps base tier taker 0.045 % (docs: trading/fees)
MAKER_FEE_BPS = 1.5             # HL perps base tier maker 0.015 %
PLACE_LATENCY_MS = 212
CANCEL_LATENCY_MS = 478
MIN_NOTIONAL_USD = 10.0


class PaperGuardError(VenueError):
    """A request the paper feed must never send (not /info, or not a public market-data type)."""


class MarketDataUnavailable(VenueError, ValueError):
    """No fresh public market data for a coin. A ValueError too, so the agent gateway answers it
    as a block reason (MARKET_DATA_UNAVAILABLE:<coin>) instead of raising."""


class PublicInfoOnly:
    """Transport wrapper: POST /info with a public market-data request, nothing else."""

    def __init__(self, transport: Callable[[str, dict], Any]):
        self._transport = transport

    def __call__(self, path: str, payload: dict) -> Any:
        if path != "/info":
            raise PaperGuardError(f"PAPER_GUARD: only /info is allowed, not {path!r}")
        kind = payload.get("type") if isinstance(payload, dict) else None
        keys = PUBLIC_INFO_TYPES.get(kind)
        if keys is None or frozenset(payload) != keys:
            raise PaperGuardError(f"PAPER_GUARD: request {payload!r} is not a public market-data read")
        return self._transport(path, payload)


def default_transport() -> PublicInfoOnly:
    """Hyperliquid MAINNET public /info over HTTPS, short timeout, generic User-Agent."""
    t = RequestsTransport(MAINNET_API_URL, timeout_s=HTTP_TIMEOUT_S)
    t.session.headers["User-Agent"] = USER_AGENT
    return PublicInfoOnly(t)


@dataclass
class _Slot:
    value: Any = None
    at: float = -1e18             # local receive time (clock seconds) of value
    fails: int = 0
    retry_at: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class Book:
    ts_ms: int                    # Hyperliquid's time of the book
    bids: tuple                   # ((px, sz), ...) best first
    asks: tuple
    recv_s: float                 # local clock when it arrived

    @property
    def mid(self) -> Optional[float]:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0 if self.bids and self.asks else None


class HLPublicFeed:
    """Cached, rate-capped reads of Hyperliquid public market data. clock() is in seconds."""

    def __init__(self, transport: Optional[Callable[[str, dict], Any]] = None,
                 clock: Callable[[], float] = time.time, book_min_s: float = BOOK_MIN_S,
                 book_stale_s: float = BOOK_STALE_S, mids_min_s: float = MIDS_MIN_S, meta_ttl_s: float = META_TTL_S,
                 limiter: Optional[TokenBucket] = None):
        self.transport = PublicInfoOnly(transport) if transport is not None else default_transport()
        self.clock = clock
        self.book_min_s, self.book_stale_s = book_min_s, book_stale_s
        self.mids_min_s, self.meta_ttl_s = mids_min_s, meta_ttl_s
        self.limiter = limiter or TokenBucket(WEIGHT_PER_MINUTE, 60.0, clock=time.monotonic)
        self._slots: dict[str, _Slot] = {}
        self.requests: dict[str, int] = {}          # request type -> count sent
        self.weight_sent = 0

    # ---------------------------------------------------------------- fetch with cache + backoff
    def _get(self, key: str, payload: dict, min_s: float, parse: Callable[[Any, float], Any]) -> _Slot:
        s = self._slots.setdefault(key, _Slot())
        now = self.clock()
        if s.value is not None and now - s.at < min_s:
            return s
        if now < s.retry_at:
            return s                                  # backing off: serve what we have
        w = INFO_WEIGHT[payload["type"]]
        self.limiter.acquire(w)
        self.requests[payload["type"]] = self.requests.get(payload["type"], 0) + 1
        self.weight_sent += w
        try:
            raw = self.transport("/info", payload)
            val = parse(raw, now)
        except PaperGuardError:
            raise
        except Exception as e:                        # network, HTTP, malformed answer: back off
            s.fails += 1
            s.retry_at = now + min(BACKOFF_BASE_S * 2 ** (s.fails - 1), BACKOFF_MAX_S)
            s.error = f"{type(e).__name__}: {e}"[:200]
            return s
        s.value, s.at, s.fails, s.retry_at, s.error = val, now, 0, 0.0, ""
        return s

    @staticmethod
    def _parse_meta(raw: Any, now: float) -> dict:
        meta, ctxs = raw
        out = {}
        for a, c in zip(meta["universe"], ctxs):
            out[a["name"]] = {"szd": int(a["szDecimals"]),
                              "day_ntl": float(c["dayNtlVlm"]) if c.get("dayNtlVlm") is not None else None,
                              "mark": float(c["markPx"]) if c.get("markPx") is not None else None}
        if not out:
            raise ValueError("empty perp meta")
        return out

    @staticmethod
    def _parse_book(raw: Any, now: float) -> Book:
        bids, asks = raw["levels"]
        lv = lambda rows: tuple((float(r["px"]), float(r["sz"])) for r in rows)     # noqa: E731
        return Book(int(raw["time"]), lv(bids), lv(asks), now)

    @staticmethod
    def _parse_mids(raw: Any, now: float) -> dict:
        return {k: float(v) for k, v in raw.items() if not k.startswith("@") and "/" not in k}

    # ---------------------------------------------------------------- reads
    def meta(self) -> dict:
        s = self._get("meta", {"type": "metaAndAssetCtxs"}, self.meta_ttl_s, self._parse_meta)
        if s.value is None:
            raise MarketDataUnavailable(f"MARKET_DATA_UNAVAILABLE:meta ({s.error or 'no data'})")
        return s.value

    def coin_meta(self, coin: str) -> dict:
        m = self.meta()
        if coin not in m:
            raise VenueUnknownAsset(f"UNKNOWN_COIN:{coin}")
        return m[coin]

    def book(self, coin: str) -> Book:
        s = self._get("book:" + coin, {"type": "l2Book", "coin": coin}, self.book_min_s, self._parse_book)
        if s.value is None or self.clock() - s.value.recv_s > self.book_stale_s:
            raise MarketDataUnavailable(f"MARKET_DATA_UNAVAILABLE:{coin}")
        return s.value

    def cached_book(self, coin: str) -> Optional[Book]:
        return self._slots.get("book:" + coin, _Slot()).value

    def refresh_mids(self) -> None:
        self._get("mids", {"type": "allMids"}, self.mids_min_s, self._parse_mids)

    def latest_mids(self) -> dict:
        """Newest known mid per coin from allMids or a book, whichever arrived later. No request."""
        ms = self._slots.get("mids")
        out: dict[str, tuple[float, float]] = {}
        if ms is not None and ms.value:
            out = {c: (px, ms.at) for c, px in ms.value.items()}
        for k, s in self._slots.items():
            if k.startswith("book:") and s.value is not None and s.value.mid is not None:
                c = k[5:]
                if c not in out or s.value.recv_s >= out[c][1]:
                    out[c] = (s.value.mid, s.value.recv_s)
        return {c: v[0] for c, v in out.items()}

    def status(self) -> dict:
        return {k: {"age_s": round(self.clock() - s.at, 3) if s.value is not None else None,
                    "fails": s.fails, "error": s.error} for k, s in self._slots.items()}


@dataclass
class PaperConfig:
    start_equity_usd: float = 10_000.0
    taker_fee_bps: float = TAKER_FEE_BPS
    maker_fee_bps: float = MAKER_FEE_BPS
    place_latency_ms: int = PLACE_LATENCY_MS
    cancel_latency_ms: int = CANCEL_LATENCY_MS
    min_notional_usd: float = MIN_NOTIONAL_USD


@dataclass
class _Order:
    oid: str
    child: ChildOrder
    px: float
    rem: float
    seq: int
    due_ms: int
    cloid: str
    status: str = "pending"          # pending | live | filled | cancelled | rejected | expired
    reason: str = ""
    cancel_due_ms: Optional[int] = None


class PaperVenue:
    """The Venue protocol on a local paper account; books from an HLPublicFeed."""
    name = "paper"
    builder = None                   # no builder code on paper (the engine checks this attribute)

    def __init__(self, feed: HLPublicFeed, cfg: Optional[PaperConfig] = None,
                 clock_ms: Optional[Callable[[], int]] = None):
        self.feed = feed
        self.cfg = cfg or PaperConfig()
        self.clock_ms = clock_ms or (lambda: int(feed.clock() * 1000))
        self.cash = float(self.cfg.start_equity_usd)
        self._pos: dict[str, list] = {}           # coin -> [size, entry_px]
        self._orders: dict[str, _Order] = {}
        self._child_oid: dict[str, str] = {}
        self._fills: list[Fill] = []
        self._fill_ts: list[int] = []
        self._used: dict[str, tuple[int, dict]] = {}   # coin -> (book ts, {px: size we took})
        self._seq = 0
        self.place_calls = 0
        self.cancel_calls = 0
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- helpers
    def _floor(self, coin: str, x: float) -> float:
        return float(round_sz(max(0.0, x), self.size_decimals(coin)))

    def _book_levels(self, coin: str, book: Book) -> tuple[list, list]:
        """The book minus what we already took from this same book (same HL time)."""
        ts, used = self._used.get(coin, (None, {}))
        if ts != book.ts_ms:
            used = {}
            self._used[coin] = (book.ts_ms, used)
        side = lambda rows, k: [[px, max(0.0, sz - used.get((k, px), 0.0))] for px, sz in rows]   # noqa: E731
        return side(book.bids, "b"), side(book.asks, "a")

    def _fill(self, o: _Order, sz: float, px: float, crossed: bool) -> None:
        c = o.child.coin
        ntl = sz * px
        fee = ntl * (self.cfg.taker_fee_bps if crossed else self.cfg.maker_fee_bps) / 1e4
        self.cash -= fee
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
        o.rem = round(o.rem - sz, 12)
        if o.rem <= EPS:
            o.rem, o.status = 0.0, "filled"
        now = self.clock_ms()
        self._fills.append(Fill(o.child.child_id, c, o.child.side, sz, px, fee, 0.0, now, crossed,
                                fill_id=f"p{len(self._fills) + 1}"))
        self._fill_ts.append(now)

    def _take(self, o: _Order, book: Book) -> None:
        """Walk the displayed opposite side up to the order price."""
        c = o.child.coin
        bids, asks = self._book_levels(c, book)
        levels, key = (asks, "a") if o.child.side is Side.BUY else (bids, "b")
        used = self._used[c][1]
        for px, avail in levels:
            if o.rem <= EPS:
                break
            if (o.child.side is Side.BUY and px > o.px + EPS) or (o.child.side is Side.SELL and px < o.px - EPS):
                break
            f = self._floor(c, min(o.rem, avail))
            if f <= EPS:
                continue
            used[(key, px)] = used.get((key, px), 0.0) + f
            self._fill(o, f, px, True)

    def _activate(self, o: _Order) -> None:
        c, side, tif = o.child.coin, o.child.side, o.child.tif
        try:
            book = self.feed.book(c)
        except VenueError as e:
            o.status, o.reason = "rejected", str(e).split(" ")[0]
            return
        if o.child.reduce_only:
            pos = self._pos.get(c, [0.0, 0.0])[0]
            if abs(pos) < EPS or (pos > 0) == (side is Side.BUY):
                o.status, o.reason = "rejected", "REDUCE_ONLY"
                return
            o.rem = min(o.rem, self._floor(c, abs(pos)))
        bids, asks = self._book_levels(c, book)
        bb = next((px for px, z in bids if z > EPS), None)
        ba = next((px for px, z in asks if z > EPS), None)
        crosses = (side is Side.BUY and ba is not None and o.px >= ba - EPS) or \
                  (side is Side.SELL and bb is not None and o.px <= bb + EPS)
        if tif == "Alo":
            if crosses:
                o.status, o.reason = "rejected", "ALO_WOULD_CROSS"
            else:
                o.status = "live"
            return
        if crosses:
            self._take(o, book)
        if o.rem <= EPS:
            return
        if tif == "Ioc":
            o.status, o.reason = "expired", "IOC_REMAINDER"
        else:
            o.status = "live"

    def _through(self, coin: str, live: list) -> None:
        """Resting orders the book traded through (strictly) fill in full at their own price."""
        try:
            book = self.feed.book(coin)
        except VenueError:
            return                                   # no data: nothing fills (conservative)
        bb = book.bids[0][0] if book.bids else None
        ba = book.asks[0][0] if book.asks else None
        for o in live:
            if o.status != "live" or o.rem <= EPS:
                continue
            if (o.child.side is Side.BUY and ba is not None and ba < o.px - EPS) or \
               (o.child.side is Side.SELL and bb is not None and bb > o.px + EPS):
                self._fill(o, o.rem, o.px, False)

    def sync(self) -> None:
        """Apply everything due by now: cancels, then arriving orders, then trade-through checks."""
        with self._lock:
            now = self.clock_ms()
            for o in sorted(self._orders.values(), key=lambda o: o.seq):
                if o.cancel_due_ms is not None and o.cancel_due_ms <= now and o.status in ("pending", "live"):
                    o.status, o.reason = "cancelled", "USER_CANCEL"
            for o in sorted(self._orders.values(), key=lambda o: (o.due_ms, o.seq)):
                if o.status == "pending" and o.due_ms <= now:
                    self._activate(o)
            live: dict[str, list] = {}
            for o in self._orders.values():
                if o.status == "live":
                    live.setdefault(o.child.coin, []).append(o)
            for c, orders in sorted(live.items()):
                self._through(c, sorted(orders, key=lambda o: o.seq))

    # ---------------------------------------------------------------- Venue protocol
    def now_ms(self) -> int:
        return self.clock_ms()

    def snapshot(self, coin: str) -> MarketSnapshot:
        try:
            m = self.feed.coin_meta(coin)
            b = self.feed.book(coin)
        except MarketDataUnavailable:
            raise MarketDataUnavailable(f"MARKET_DATA_UNAVAILABLE:{coin}") from None
        return MarketSnapshot(coin, b.ts_ms, tuple(BookLevel(px, sz) for px, sz in b.bids),
                              tuple(BookLevel(px, sz) for px, sz in b.asks), mark_px=m["mark"],
                              day_ntl_usd=m["day_ntl"])

    def equity(self) -> float:
        mids = self.feed.latest_mids()
        return self.cash + sum(sz * (mids.get(c, e) - e) for c, (sz, e) in self._pos.items() if abs(sz) > EPS)

    def account(self) -> AccountState:
        self.sync()
        pos = tuple(Position(c, sz, e) for c, (sz, e) in sorted(self._pos.items()) if abs(sz) > EPS)
        oo = tuple(OpenOrder(o.child.child_id, o.child.coin, o.child.side, o.rem, o.px, o.child.reduce_only,
                             o.oid, o.cloid)
                   for o in sorted(self._orders.values(), key=lambda o: o.seq) if o.status in ("pending", "live"))
        return AccountState(self.clock_ms(), self.equity(), pos, oo)

    def _validate(self, child: ChildOrder) -> tuple[float, float]:
        if child.child_id in self._child_oid:
            raise VenueError(f"duplicate child id {child.child_id}")
        if child.tif not in ("Ioc", "Gtc", "Alo"):
            raise VenueError(f"UNSUPPORTED_TIF:{child.tif}")
        if child.builder is not None:
            raise VenueError("BUILDER_MISMATCH: the paper venue has no builder")
        szd = self.feed.coin_meta(child.coin)["szd"]           # MARKET_DATA_UNAVAILABLE / UNKNOWN_COIN
        is_buy = child.side is Side.BUY
        px = float(round_px(child.px, szd, mode="down" if is_buy else "up"))   # as the live adapter
        sz = float(round_sz(child.size, szd))
        if sz <= 0:
            raise VenueError(f"SIZE_ROUNDS_TO_ZERO:{child.size}@{szd}dp")
        if sz * px < self.cfg.min_notional_usd and not child.reduce_only:
            raise VenueError("Order must have minimum value of $10.")
        return px, sz

    def place(self, child: ChildOrder, abort: Optional[Callable[[], bool]] = None) -> str:
        with self._lock:
            if abort is not None and abort():
                raise VenueAborted("ABORTED_BEFORE_SEND")
            self.place_calls += 1
            px, sz = self._validate(child)
            self._seq += 1
            oid = f"p{self._seq}"
            self._orders[oid] = _Order(oid, child, px, sz, self._seq, self.clock_ms() + self.cfg.place_latency_ms,
                                       child.cloid or cloid_for(child.child_id))
            self._child_oid[child.child_id] = oid
            return oid

    def cancel(self, coin: str, venue_oid: str) -> bool:
        with self._lock:
            self.cancel_calls += 1
            o = self._orders.get(venue_oid)
            if o is None or o.child.coin != coin or o.status not in ("pending", "live"):
                return False
            if o.cancel_due_ms is None:
                o.cancel_due_ms = self.clock_ms() + self.cfg.cancel_latency_ms
            return True

    def order_status(self, key: str) -> dict:
        self.sync()
        o = self._orders.get(self._child_oid.get(key, key))
        if o is None:
            return {"state": "unknown", "filled_sz": 0.0, "oid": None, "status": "unknown"}
        state = {"pending": "open", "live": "open", "expired": "cancelled"}.get(o.status, o.status)
        return {"state": state, "filled_sz": round(o.child.size - o.rem, 12), "oid": o.oid, "status": o.status,
                "reason": o.reason, "remaining": o.rem}

    def fills_since(self, ts_ms: int) -> list[Fill]:
        self.sync()
        return self._fills[bisect.bisect_left(self._fill_ts, ts_ms):]

    def mark_prices(self) -> dict[str, float]:
        """Newest known mids; allMids is re-read (rate-capped) only while there is exposure."""
        if any(abs(sz) > EPS for sz, _ in self._pos.values()) or \
                any(o.status in ("pending", "live") for o in self._orders.values()):
            try:
                self.feed.refresh_mids()
            except VenueError:
                pass
        return self.feed.latest_mids()

    def external_flow_events(self, start_ms: int) -> list:
        return []

    def external_flows_usd(self, start_ms: int) -> float:
        return 0.0

    def size_decimals(self, coin: str) -> int:
        return self.feed.coin_meta(coin)["szd"]

    def tick_size(self, coin: str) -> float:
        """Grid step at the current price: 5 significant figures, at most 6 - szDecimals decimals
        (the live adapter's rule)."""
        szd = self.size_decimals(coin)
        ref = self.feed.latest_mids().get(coin) or self.feed.coin_meta(coin)["mark"]
        q_dec = 10.0 ** -(6 - szd)
        if not ref:
            return q_dec
        return max(10.0 ** (math.floor(math.log10(ref)) - 4), q_dec)

    def perp_coins(self) -> frozenset:
        try:
            return frozenset(self.feed.meta())
        except VenueError:
            return frozenset()

    def fee_bps(self) -> dict:
        return {"taker": self.cfg.taker_fee_bps, "maker": self.cfg.maker_fee_bps}

    def set_builder(self, builder: Optional[tuple]) -> None:
        if builder is not None:
            raise VenueError("the paper venue has no builder code")
