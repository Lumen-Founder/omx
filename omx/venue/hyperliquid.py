"""Hyperliquid perps adapter for the Venue protocol.

The adapter builds order and cancel actions exactly as hyperliquid-python-sdk
0.20.0 does (hyperliquid/exchange.py bulk_orders / bulk_cancel and
hyperliquid/utils/signing.py order_request_to_order_wire,
order_wires_to_order_action), so the msgpack bytes and therefore the signed
hash match the official client. Signing goes through an injected Signer; the
adapter never sees key material.

The intended signer is an agent (API) wallet approved by the user's main
wallet. Agent wallets can place and cancel orders for the account but cannot
withdraw or transfer funds, and cannot approve builder fees (see omx.builder).

All network access goes through one `transport(path, payload) -> dict`
callable, so tests inject a fake and never touch the network. Requests pass a
token bucket sized to Hyperliquid's documented REST limit (1200 weight per
minute per IP) and back off on HTTP 429.
"""
from __future__ import annotations

import time
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from os import PathLike
from pathlib import Path
from typing import Callable, Optional, Protocol

from .. import account_mode as AM
from ..builder import PERP_MAX_TENTHS_BPS, is_address, parse_max_builder_fee
from ..types import AccountState, BookLevel, ChildOrder, Fill, MarketSnapshot, OpenOrder, Position, Side
from .base import (PlaceAck, VenueAborted, VenueAmbiguous, VenueError, VenueUnknownAsset, cloid_for,  # noqa: F401
                   is_omx_cloid)

MAINNET_API_URL = "https://api.hyperliquid.xyz"
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
PERP_MAX_DECIMALS = 6
SPOT_MAX_DECIMALS = 8
MAX_SIG_FIGS = 5
FILLS_PAGE_MAX = 2000                 # userFillsByTime returns at most this many rows
ORDER_EXPIRY_MS = 15_000              # R3-4: every signed action carries expiresAfter = now + this
UNKNOWN_RETRY_MS = 60_000             # an unknown coin re-reads the meta at most this often
SPOT_ASSET_OFFSET = 10_000            # HL asset id of a spot pair = 10000 + its spotMeta index
ACCOUNT_MODE_TTL_MS = 600_000         # userAbstraction is re-read at most this often (weight 20)
ACCOUNT_MODE_RETRY_MS = 60_000        # after a failed read: "unknown" (fail closed), retried after this

# Documented REST weights (HL docs, "Rate limits and user limits").
WEIGHT_PER_MINUTE = 1200
INFO_LIGHT = frozenset({"l2Book", "allMids", "clearinghouseState", "orderStatus",
                        "spotClearinghouseState", "exchangeStatus"})          # weight 2
INFO_DEFAULT_WEIGHT = 20
INFO_PER_ITEMS = frozenset({"userFills", "userFillsByTime", "historicalOrders", "fundingHistory",
                            "userFunding", "candleSnapshot", "recentTrades"})  # +1 per 20 items returned

Transport = Callable[[str, dict], dict]


class RateLimited(VenueError):
    """Raised by a transport when the venue answers HTTP 429."""


def info_weight(req_type: str) -> int:
    return 2 if req_type in INFO_LIGHT else INFO_DEFAULT_WEIGHT


def exchange_weight(batch_len: int) -> int:
    return 1 + batch_len // 40


class TokenBucket:
    """Weight bucket refilled continuously at capacity/period. Clock and sleep are injectable."""

    def __init__(self, capacity: float = WEIGHT_PER_MINUTE, period_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.capacity = float(capacity)
        self.rate = capacity / period_s
        self.clock, self.sleep = clock, sleep
        self.tokens = float(capacity)
        self.last = clock()
        self.blocked_until = 0.0

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def acquire(self, weight: float) -> float:
        """Block until `weight` is available, then take it. Returns seconds slept."""
        if weight > self.capacity:
            raise ValueError(f"weight {weight} exceeds bucket capacity {self.capacity}")
        slept = 0.0
        wait = self.blocked_until - self.clock()
        if wait > 0:
            self.sleep(wait)
            slept += wait
        self._refill()
        if self.tokens < weight:
            wait = (weight - self.tokens) / self.rate
            self.sleep(wait)
            slept += wait
            self._refill()
        self.tokens -= weight
        return slept

    def charge(self, weight: float) -> None:
        """Take weight known only after the response (may go negative; later calls wait)."""
        self._refill()
        self.tokens -= weight

    def penalize(self, seconds: float) -> None:
        """After a 429: empty the bucket and refuse everything for `seconds`."""
        self._refill()
        self.tokens = min(self.tokens, 0.0)
        self.blocked_until = max(self.blocked_until, self.clock() + seconds)


# --- signing ---------------------------------------------------------------

class Signer(Protocol):
    address: str

    def sign_l1_action(self, action: dict, nonce: int, vault_address: Optional[str],
                       is_mainnet: bool, expires_after: Optional[int] = None) -> dict: ...


class LocalKeySigner:
    """Signs L1 actions with a key held only inside an eth_account LocalAccount.

    Use an agent (API) wallet key: it can trade but cannot withdraw or transfer.
    The key file is read once, when from_key_file is called; the raw text is not
    kept, logged or shown in repr, and the object refuses to be pickled.
    """
    __slots__ = ("_account",)

    def __init__(self, account):
        self._account = account

    @classmethod
    def from_key_file(cls, path: str | PathLike) -> "LocalKeySigner":
        from eth_account import Account
        text = Path(path).read_text(encoding="utf-8").strip()
        try:
            acct = Account.from_key(text)
        except Exception:
            raise ValueError("key file does not hold a valid hex private key") from None
        finally:
            text = None
        return cls(acct)

    @classmethod
    def from_env(cls, name: str) -> "LocalKeySigner":
        """Agent key from an environment variable set by the user's own shell."""
        import os
        from eth_account import Account
        text = (os.environ.get(name) or "").strip()
        if not text:
            raise ValueError(f"environment variable {name} is empty or unset")
        try:
            acct = Account.from_key(text)
        except Exception:
            raise ValueError(f"environment variable {name} does not hold a valid hex private key") from None
        finally:
            text = None
        return cls(acct)

    @property
    def address(self) -> str:
        return self._account.address

    def sign_l1_action(self, action: dict, nonce: int, vault_address: Optional[str], is_mainnet: bool,
                       expires_after: Optional[int] = None) -> dict:
        from hyperliquid.utils.signing import sign_l1_action
        return sign_l1_action(self._account, action, vault_address, nonce, expires_after, is_mainnet)

    def __repr__(self) -> str:
        return f"LocalKeySigner(address={self.address})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("LocalKeySigner cannot be pickled")


# --- wire formatting -------------------------------------------------------

def wire_str(d: Decimal) -> str:
    """Same text the SDK's float_to_wire produces: no exponent, no trailing zeros."""
    s = f"{d.normalize():f}"
    return "0" if s == "-0" else s


_MODES = {"nearest": ROUND_HALF_EVEN, "down": ROUND_FLOOR, "up": ROUND_CEILING}


def round_px(px: float, sz_decimals: int, is_spot: bool = False, mode: str = "nearest") -> Decimal:
    """Venue price grid: at most 5 significant figures and at most
    (6 - szDecimals) decimals for perps (8 - szDecimals for spot)."""
    d = Decimal(repr(float(px)))
    if not d.is_finite() or d <= 0:
        raise VenueError(f"INVALID_PRICE:{px}")
    max_dec = (SPOT_MAX_DECIMALS if is_spot else PERP_MAX_DECIMALS) - sz_decimals
    q = max(Decimal(1).scaleb(d.adjusted() - (MAX_SIG_FIGS - 1)), Decimal(1).scaleb(-max_dec))
    return (d / q).to_integral_value(rounding=_MODES[mode]) * q


def round_sz(sz: float, sz_decimals: int) -> Decimal:
    """Size rounded down to szDecimals (never send more than was approved)."""
    return Decimal(repr(float(sz))).quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_FLOOR)


# --- default transport -----------------------------------------------------

class RequestsTransport:
    def __init__(self, base_url: str, timeout_s: float = 10.0):
        import requests
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def __call__(self, path: str, payload: dict) -> dict:
        r = self.session.post(self.base_url + path, json=payload, timeout=self.timeout_s)
        if r.status_code == 429:
            raise RateLimited("HTTP 429")
        if r.status_code >= 500:                     # the venue may have processed it
            raise VenueAmbiguous(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise VenueError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise VenueError(f"non-JSON response: {r.text[:300]}") from None


# --- venue -----------------------------------------------------------------

class HyperliquidVenue:
    name = "hyperliquid"

    def __init__(self, base_url: str, account_address: str, signer: Signer,
                 builder: Optional[tuple[str, int]] = None, transport: Optional[Transport] = None, *,
                 vault_address: Optional[str] = None, limiter: Optional[TokenBucket] = None,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep, ctx_ttl_ms: int = 5_000,
                 max_retries: int = 3, backoff_s: float = 1.0, allow_main_wallet_signer: bool = False,
                 order_expiry_ms: int = ORDER_EXPIRY_MS):
        self.base_url = base_url.rstrip("/")
        self.is_mainnet = self.base_url == MAINNET_API_URL
        if not is_address(account_address):
            raise VenueError("malformed account address")
        if signer.address.lower() == account_address.lower() and not allow_main_wallet_signer:
            raise VenueError("signer is the main wallet; use an agent (API) wallet, which cannot withdraw")
        self.account_address = account_address.lower()
        self.signer = signer
        self.builder = self._check_builder(builder)
        if self.builder is not None and self.builder["b"] in (self.account_address, signer.address.lower()):
            raise VenueError("builder address must differ from the account and from the agent (API) wallet")
        self.transport = transport or RequestsTransport(self.base_url)
        self.vault_address = vault_address
        self.limiter = limiter or TokenBucket(sleep=sleep)
        self.clock_ms = clock_ms
        self.sleep = sleep
        self.ctx_ttl_ms = ctx_ttl_ms
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.order_expiry_ms = int(order_expiry_ms)
        self._assets: dict[str, tuple[int, int]] = {}    # coin -> (asset index, szDecimals)
        self._unknown: dict[str, int] = {}               # coin -> ms of the meta read that did not list it
        self._spot: Optional[dict[str, int]] = None      # spot pair name -> spotMeta index (read on demand)
        self._cloid_of: dict[str, str] = {}              # child_id -> cloid sent
        self._last_expires: Optional[int] = None
        self._ctx: dict[str, dict] = {}
        self._ctx_at = -10**15
        self._last_nonce = 0
        self._child_by_cloid: dict[str, str] = {}
        self._child_by_oid: dict[int, str] = {}
        self.account_mode_ttl_ms = ACCOUNT_MODE_TTL_MS
        self._mode: Optional[str] = None                 # last userAbstraction answer (or "unknown")
        self._mode_at = -10**15                          # ms of that read (or of the failed attempt)

    @staticmethod
    def _check_builder(builder) -> Optional[dict]:
        if builder is None:
            return None
        addr, fee = builder
        if not is_address(addr):
            raise VenueError(f"malformed builder address: {addr!r}")
        if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
            raise VenueError(f"builder fee must be a non-negative int in tenths of a bps: {fee!r}")
        if fee > PERP_MAX_TENTHS_BPS:
            raise VenueError(f"builder fee {fee} tenths bps exceeds the perps cap {PERP_MAX_TENTHS_BPS} (0.1 %)")
        return {"b": addr.lower(), "f": fee}

    # transport with rate limiting

    def _info(self, payload: dict) -> object:
        self.limiter.acquire(info_weight(payload["type"]))
        for attempt in range(self.max_retries + 1):
            try:
                return self.transport("/info", payload)
            except RateLimited:
                self.limiter.penalize(min(self.backoff_s * 2 ** attempt, 60.0))
                if attempt == self.max_retries:
                    raise VenueError(f"RATE_LIMITED:info:{payload['type']}") from None
                self.limiter.acquire(info_weight(payload["type"]))
        raise AssertionError("unreachable")

    def _next_nonce(self) -> int:
        self._last_nonce = max(self.clock_ms(), self._last_nonce + 1)
        return self._last_nonce

    def _exchange(self, action: dict, batch_len: int, abort: Optional[Callable[[], bool]] = None) -> dict:
        """Sign and post once. A 429 is not retried: the engine decides what to do next.

        The action is signed after the rate-limit wait with expiresAfter = now + order_expiry_ms
        (R3-4, as the SDK's Exchange.set_expires_after: HL rejects the action after that time), so
        a request that is retransmitted or delayed cannot land after the engine has given up on it.
        abort() is checked after signing, immediately before the send. Timeouts, connection errors
        and 5xx raise VenueAmbiguous carrying expires_after_ms: the action may have landed."""
        self.limiter.acquire(exchange_weight(batch_len))
        nonce = self._next_nonce()
        expires = self.clock_ms() + self.order_expiry_ms
        self._last_expires = expires
        signature = self.signer.sign_l1_action(action, nonce, self.vault_address, self.is_mainnet,
                                               expires_after=expires)
        body = {"action": action, "nonce": nonce, "signature": signature,
                "vaultAddress": self.vault_address, "expiresAfter": expires}
        if abort is not None and abort():
            raise VenueAborted("ABORTED_BEFORE_SEND")
        try:
            res = self.transport("/exchange", body)
        except RateLimited:
            self.limiter.penalize(self.backoff_s)
            raise VenueError("RATE_LIMITED:exchange") from None
        except VenueAmbiguous as e:                  # 5xx
            raise VenueAmbiguous(str(e), expires) from None
        except VenueError:
            raise                                    # a definite 4xx
        except Exception as e:                       # timeout / connection reset: outcome unknown
            raise VenueAmbiguous(f"AMBIGUOUS:{type(e).__name__}", expires) from None
        if not isinstance(res, dict) or res.get("status") != "ok":
            msg = res.get("response") if isinstance(res, dict) else res
            raise VenueError(f"VENUE_REJECTED:{msg}")
        return res

    # metadata

    def _refresh_ctx(self, force: bool = False) -> None:
        now = self.clock_ms()
        if not force and self._assets and now - self._ctx_at < self.ctx_ttl_ms:
            return
        meta, ctxs = self._info({"type": "metaAndAssetCtxs"})
        assets, ctx = {}, {}
        for i, (a, c) in enumerate(zip(meta["universe"], ctxs)):
            assets[a["name"]] = (i, int(a["szDecimals"]))
            ctx[a["name"]] = c
        self._assets, self._ctx, self._ctx_at = assets, ctx, now

    def _asset(self, coin: str) -> tuple[int, int]:
        """Perp asset id and szDecimals. An unknown coin re-reads the meta at most once per
        UNKNOWN_RETRY_MS (no refetch loop); other-dex coins ("dex:COIN") are never looked up."""
        if coin not in self._assets:
            seen = self._unknown.get(coin)
            if ":" in coin or (seen is not None and self.clock_ms() - seen < UNKNOWN_RETRY_MS):
                raise VenueUnknownAsset(f"UNKNOWN_COIN:{coin}")
            self._refresh_ctx(force=True)
            if coin not in self._assets:
                self._unknown[coin] = self.clock_ms()
                raise VenueUnknownAsset(f"UNKNOWN_COIN:{coin}")
        return self._assets[coin]

    def perp_coins(self) -> frozenset:
        """Coins of the cached perp meta (empty before the first meta read)."""
        return frozenset(self._assets)

    def _cancel_asset(self, coin: str) -> int:
        """Asset id for a cancel: perps from the meta; spot "@<i>" = 10000 + i; a named spot pair
        ("PURR/USDC") from spotMeta (read once, weight 20)."""
        if coin.startswith("@") and coin[1:].isdigit():
            return SPOT_ASSET_OFFSET + int(coin[1:])
        if "/" in coin:
            if self._spot is None:
                seen = self._unknown.get("spotMeta")
                if seen is not None and self.clock_ms() - seen < UNKNOWN_RETRY_MS:
                    raise VenueUnknownAsset(f"UNKNOWN_SPOT:{coin}")
                self._unknown["spotMeta"] = self.clock_ms()
                meta = self._info({"type": "spotMeta"})
                self._spot = {u["name"]: int(u["index"]) for u in (meta or {}).get("universe", [])}
            if coin not in self._spot:
                raise VenueUnknownAsset(f"UNKNOWN_SPOT:{coin}")
            return SPOT_ASSET_OFFSET + self._spot[coin]
        return self._asset(coin)[0]

    def size_decimals(self, coin: str) -> int:
        return self._asset(coin)[1]

    def tick_size(self, coin: str) -> float:
        """HL has no fixed tick: the grid depends on price. This is the tick at the
        cached mark price (falls back to the decimals cap when no mark is known)."""
        szd = self.size_decimals(coin)
        mark = self._ctx.get(coin, {}).get("markPx")
        q_dec = Decimal(1).scaleb(-(PERP_MAX_DECIMALS - szd))
        if not mark:
            return float(q_dec)
        d = Decimal(str(mark))
        return float(max(Decimal(1).scaleb(d.adjusted() - (MAX_SIG_FIGS - 1)), q_dec))

    # Venue protocol

    def now_ms(self) -> int:
        return self.clock_ms()

    def snapshot(self, coin: str) -> MarketSnapshot:
        """l2Book (weight 2) plus the CACHED perp meta (R3-9): metaAndAssetCtxs is re-read only by
        mark_prices() on the engine's marks schedule, or once for a coin the cache does not list."""
        if coin not in self._ctx:
            self._asset(coin)
        if coin not in self._ctx:
            raise VenueUnknownAsset(f"UNKNOWN_COIN:{coin}")
        book = self._info({"type": "l2Book", "coin": coin})
        bids_raw, asks_raw = book["levels"]
        lv = lambda rows: tuple(BookLevel(float(r["px"]), float(r["sz"])) for r in rows)
        c = self._ctx[coin]
        mark = c.get("markPx")
        return MarketSnapshot(coin=coin, ts_ms=int(book["time"]), bids=lv(bids_raw), asks=lv(asks_raw),
                              mark_px=float(mark) if mark is not None else None,
                              day_ntl_usd=float(c["dayNtlVlm"]) if c.get("dayNtlVlm") is not None else None)

    def mark_prices(self) -> dict[str, float]:
        """Mark price of every listed coin (metaAndAssetCtxs, cached for ctx_ttl_ms)."""
        self._refresh_ctx()
        return {c: float(x["markPx"]) for c, x in self._ctx.items() if x.get("markPx") is not None}

    def _child_id(self, cloid: Optional[str], oid: Optional[int]) -> str:
        if cloid and cloid in self._child_by_cloid:
            return self._child_by_cloid[cloid]
        if oid is not None and oid in self._child_by_oid:
            return self._child_by_oid[oid]
        return cloid or f"oid:{oid}"

    def account(self) -> AccountState:
        """account_core() + frontendOpenOrders (weight 20)."""
        core = self.account_core()
        return replace(core, open_orders=self.open_orders())

    def account_mode(self, force: bool = False) -> str:
        """userAbstraction of the account (weight 20), read at the first call and then at most once
        per account_mode_ttl_ms (10 min). A failed or malformed read gives "unknown" (unsupported: new
        intents are refused) and is retried after ACCOUNT_MODE_RETRY_MS; it never raises, so the
        account read and therefore kill / cancels keep working."""
        now = self.clock_ms()
        ttl = ACCOUNT_MODE_RETRY_MS if self._mode == AM.UNKNOWN_MODE else self.account_mode_ttl_ms
        if not force and self._mode is not None and now - self._mode_at < ttl:
            return self._mode
        try:
            mode = self._info({"type": "userAbstraction", "user": self.account_address})
            mode = mode if isinstance(mode, str) and mode else AM.UNKNOWN_MODE
        except Exception:
            mode = AM.UNKNOWN_MODE
        self._mode, self._mode_at = mode, now
        return mode

    def account_core(self) -> AccountState:
        """Equity and positions only; open_orders is empty. The engine's poll schedule reads this
        and open_orders() at different rates.

        Positions always come from clearinghouseState (weight 2). Equity by account mode
        (omx.account_mode, which also holds the doc text behind the unified basis): standard
        ("disabled" / "default") = perp marginSummary.accountValue; "unifiedAccount" = spot USDC total
        (spotClearinghouseState, weight 2), which holds the positions' unrealized P&L (measured
        2026-11-05; VERIFIED_UNIFIED_BASIS); any other mode = perp accountValue flagged ACCOUNT_MODE_UNSUPPORTED
        (the policy then blocks every child; the read itself still succeeds so kill keeps working)."""
        mode = self.account_mode()
        cs = self._info({"type": "clearinghouseState", "user": self.account_address})
        positions = []
        for ap in cs.get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if szi == 0:
                continue
            lev = p.get("leverage") or {}
            positions.append(Position(coin=p["coin"], size=szi, entry_px=float(p.get("entryPx") or 0.0),
                                      leverage=float(lev.get("value", 1.0))))
        if mode in AM.UNIFIED_MODES:
            spot = self._info({"type": "spotClearinghouseState", "user": self.account_address})
            try:
                u = AM.unified_equity(spot, cs)
            except ValueError as e:
                raise VenueError(f"MALFORMED_SPOT_STATE:{e}") from None
            equity, basis, flags = u["equity_usd"], u["basis"], tuple(u["flags"])
        else:
            equity, basis = float(cs["marginSummary"]["accountValue"]), AM.BASIS_PERP
            flags = () if mode in AM.STANDARD_MODES else (AM.ACCOUNT_MODE_UNSUPPORTED,)
        return AccountState(ts_ms=int(cs.get("time") or self.clock_ms()), equity_usd=equity,
                            positions=tuple(positions), open_orders=(), account_mode=mode,
                            equity_basis=basis, equity_flags=flags)

    def open_orders(self) -> tuple:
        """frontendOpenOrders (weight 20)."""
        oo = self._info({"type": "frontendOpenOrders", "user": self.account_address})
        # frontendOpenOrders carries reduceOnly; if it is ever missing, False counts the
        # order as risk-increasing, the conservative reading for the exposure checks.
        return tuple(OpenOrder(child_id=self._child_id(o.get("cloid"), o.get("oid")), coin=o["coin"],
                               side=Side.BUY if o["side"] == "B" else Side.SELL, size=float(o["sz"]),
                               px=float(o["limitPx"]), reduce_only=bool(o.get("reduceOnly", False)),
                               oid=str(o["oid"]) if o.get("oid") is not None else "",
                               cloid=o.get("cloid") or "")
                     for o in oo)

    def build_order_action(self, child: ChildOrder) -> dict:
        """Unsigned order action, field for field as the SDK's bulk_orders builds it."""
        if child.tif not in ("Ioc", "Gtc", "Alo"):
            raise VenueError(f"UNSUPPORTED_TIF:{child.tif}")
        asset, szd = self._asset(child.coin)
        is_buy = child.side is Side.BUY
        # Round toward the passive side so the wire price is never worse than the
        # price the policy approved.
        px = round_px(child.px, szd, mode="down" if is_buy else "up")
        sz = round_sz(child.size, szd)
        if sz <= 0:
            raise VenueError(f"SIZE_ROUNDS_TO_ZERO:{child.size}@{szd}dp")
        mine = (self.builder["b"], self.builder["f"]) if self.builder else None
        theirs = getattr(child, "builder", None)
        if theirs is not None and (str(theirs[0]).lower(), theirs[1]) != mine:
            # fail closed: an order never goes out with a builder other than the configured one
            raise VenueError(f"BUILDER_MISMATCH: child carries {theirs}, the venue is configured with {mine}")
        wire = {"a": asset, "b": is_buy, "p": wire_str(px), "s": wire_str(sz), "r": bool(child.reduce_only),
                "t": {"limit": {"tif": child.tif}}, "c": child.cloid or cloid_for(child.child_id)}
        action = {"type": "order", "orders": [wire], "grouping": "na"}
        if self.builder:                     # always attached when configured: a caller cannot remove it
            action["builder"] = dict(self.builder)
        return action

    def max_builder_fee(self, builder: Optional[str] = None) -> int:
        """/info maxBuilderFee for (this account, builder): the approved maximum in tenths of a bps
        (0 = not approved). Raises on any other answer (fail closed)."""
        b = (builder or (self.builder or {}).get("b") or "").lower()
        if not is_address(b):
            raise VenueError("no builder address to check")
        return parse_max_builder_fee(self._info({"type": "maxBuilderFee", "user": self.account_address,
                                                 "builder": b}))

    def place(self, child: ChildOrder, abort: Optional[Callable[[], bool]] = None) -> str:
        """Returns a PlaceAck: the oid, plus filled_sz / avg_px when the status is "filled"."""
        action = self.build_order_action(child)
        cloid = action["orders"][0]["c"]
        self._child_by_cloid[cloid] = child.child_id
        self._cloid_of[child.child_id] = cloid
        res = self._exchange(action, 1, abort)
        statuses = res["response"]["data"]["statuses"]
        if len(statuses) != 1:
            raise VenueAmbiguous(f"UNEXPECTED_STATUSES:{statuses}", self._last_expires)
        st = statuses[0]
        if isinstance(st, dict) and "error" in st:
            raise VenueError(st["error"])
        if isinstance(st, dict) and "resting" in st:
            oid = int(st["resting"]["oid"])
            self._child_by_oid[oid] = child.child_id
            return PlaceAck(str(oid))
        if isinstance(st, dict) and "filled" in st:
            f = st["filled"]
            oid = int(f["oid"])
            self._child_by_oid[oid] = child.child_id
            return PlaceAck(str(oid), float(f.get("totalSz") or 0.0),
                            float(f["avgPx"]) if f.get("avgPx") is not None else None)
        raise VenueAmbiguous(f"UNKNOWN_STATUS:{st}", self._last_expires)   # accepted in a form we do not model

    def order_status(self, child_id: str) -> dict:
        """orderStatus by cloid (weight 2). state: open | filled | cancelled | rejected | unknown."""
        cloid = self._cloid_of.get(child_id) or cloid_for(child_id)
        res = self._info({"type": "orderStatus", "user": self.account_address, "oid": cloid})
        if not isinstance(res, dict) or res.get("status") != "order":
            return {"state": "unknown", "filled_sz": 0.0, "oid": None, "reason": str(res)[:100]}
        o = res.get("order") or {}
        inner = o.get("order") or {}
        s = str(o.get("status", ""))
        low = s.lower()
        if s in ("open", "triggered"):
            state = "open"
        elif s == "filled":
            state = "filled"
        elif low.endswith("rejected"):
            state = "rejected"
        elif low.endswith("canceled") or low.endswith("cancelled"):
            state = "cancelled"
        else:
            state = "unknown"
        oid = inner.get("oid")
        if oid is not None:
            self._child_by_oid[int(oid)] = child_id
        orig, rem = float(inner.get("origSz") or 0.0), float(inner.get("sz") or 0.0)
        return {"state": state, "filled_sz": max(0.0, orig - rem) if orig else 0.0,
                "oid": str(oid) if oid is not None else None, "reason": s}

    def external_flow_events(self, start_ms: int) -> list[tuple[int, float]]:
        """(time, usd) for each ledger entry since start_ms that moves money into (+) or out of (-)
        the perp account: deposits, withdrawals, internal / sub-account transfers, spot<->perp
        class transfers, vault deposits / withdrawals (userNonFundingLedgerUpdates)."""
        rows = self._info({"type": "userNonFundingLedgerUpdates", "user": self.account_address,
                           "startTime": int(start_ms), "endTime": None})
        out = []
        for r in rows or []:
            usd = self._flow_usd(r.get("delta") or {})
            if usd and int(r["time"]) >= int(start_ms):
                out.append((int(r["time"]), usd))
        return out

    def external_flows_usd(self, start_ms: int) -> float:
        return sum(u for _, u in self.external_flow_events(start_ms))

    def _flow_usd(self, d: dict) -> float:
        t, me = d.get("type"), self.account_address
        usdc = float(d.get("usdc") or 0.0)
        if t == "deposit":
            return usdc
        if t == "withdraw":
            return -usdc                     # the withdrawal fee stays in P&L (conservative)
        if t == "accountClassTransfer":
            return usdc if d.get("toPerp") else -usdc
        if t in ("internalTransfer", "subAccountTransfer"):
            dst, src = str(d.get("destination") or "").lower(), str(d.get("user") or "").lower()
            return (usdc if dst == me else 0.0) - (usdc if src == me else 0.0)
        if t == "vaultDeposit":
            return -usdc
        if t == "vaultWithdraw":
            return float(d.get("netWithdrawnUsd") or usdc)
        return 0.0                           # liquidation, spot-only moves: not perp-account flows

    def cancel(self, coin: str, venue_oid: str) -> bool:
        """True when the venue cancelled it; False when it was already filled or gone. Spot orders
        are cancelled too (asset 10000 + spot index); a coin with no known asset id raises
        VenueUnknownAsset before anything is sent."""
        asset = self._cancel_asset(coin)
        res = self._exchange({"type": "cancel", "cancels": [{"a": asset, "o": int(venue_oid)}]}, 1)
        st = res["response"]["data"]["statuses"][0]
        return st == "success"

    def fills_since(self, ts_ms: int) -> list[Fill]:
        out, seen, start = [], set(), int(ts_ms)
        for _ in range(50):
            rows = self._info({"type": "userFillsByTime", "user": self.account_address,
                               "startTime": start, "endTime": None, "aggregateByTime": False})
            self.limiter.charge(len(rows) // 20)
            new = 0
            for r in rows:
                key = (r.get("tid"), r.get("hash"), r.get("oid"), r.get("time"))
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                out.append(Fill(child_id=self._child_id(r.get("cloid"), r.get("oid")), coin=r["coin"],
                                fill_id=str(r["tid"]) if r.get("tid") is not None else "",
                                side=Side.BUY if r["side"] == "B" else Side.SELL, size=float(r["sz"]),
                                # HL docs: "fee" is the total fee, inclusive of builderFee
                                px=float(r["px"]),
                                fee_usd=float(r.get("fee") or 0.0) - float(r.get("builderFee") or 0.0),
                                builder_fee_usd=float(r.get("builderFee") or 0.0),
                                ts_ms=int(r["time"]), crossed=bool(r.get("crossed", True))))
            if len(rows) < FILLS_PAGE_MAX or new == 0:
                break
            start = max(int(r["time"]) for r in rows)    # same-ms rows are de-duplicated above
        out.sort(key=lambda f: f.ts_ms)
        return out
