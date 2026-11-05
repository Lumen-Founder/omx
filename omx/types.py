"""Core value types shared by the policy, engine, venues and interfaces.

Everything is plain dataclasses so state can be serialised into the audit log
and replayed. Prices and sizes are floats in venue units; notional is USD.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class Algo(str, Enum):
    MARKET = "market"      # one aggressive IOC with a price cap
    LIMIT = "limit"        # one resting order (GTC) or post-only (ALO)
    TWAP = "twap"          # equal slices over a duration
    POV = "pov"            # slices sized to a share of observed volume
    CHASE = "chase"        # post-only at touch, re-priced until filled or capped


@dataclass(frozen=True)
class Intent:
    """What the user or agent wants. The engine turns it into child orders."""
    intent_id: str
    coin: str
    side: Side
    size: float                          # base units, > 0
    algo: Algo = Algo.TWAP
    limit_px: Optional[float] = None     # hard price cap (buy) / floor (sell)
    duration_s: int = 0                  # for TWAP / POV
    slices: int = 1
    pov_rate: float = 0.0                # for POV, 0..1
    post_only: bool = False
    reduce_only: bool = False
    max_slippage_bps: float = 50.0       # vs arrival mid, checked per child
    source: str = "human"                # "human" | "agent:<name>"
    note: str = ""


@dataclass(frozen=True)
class ChildOrder:
    child_id: str
    intent_id: str
    coin: str
    side: Side
    size: float
    px: float
    tif: str                             # "Ioc" | "Gtc" | "Alo"
    reduce_only: bool = False
    cloid: str = ""                      # set by the engine (instance-tagged); "" = venue default
    builder: Optional[tuple] = None      # (lowercase address, fee tenths bps), set by the engine only


@dataclass(frozen=True)
class Fill:
    child_id: str
    coin: str
    side: Side
    size: float
    px: float
    fee_usd: float                       # exchange fee, negative = rebate
    builder_fee_usd: float = 0.0
    ts_ms: int = 0
    crossed: bool = True
    fill_id: str = ""                    # venue trade id (HL tid); dedupe key


@dataclass(frozen=True)
class BookLevel:
    px: float
    sz: float


@dataclass(frozen=True)
class MarketSnapshot:
    coin: str
    ts_ms: int                           # time the data was produced by the venue
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    mark_px: Optional[float] = None
    day_ntl_usd: Optional[float] = None  # trailing 24 h traded notional

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].px if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].px + self.asks[0].px) / 2.0

    def depth_usd(self, side: Side, band_bps: float) -> float:
        """Resting notional on the side we would take, within band_bps of mid."""
        m = self.mid
        if m is None:
            return 0.0
        levels = self.asks if side is Side.BUY else self.bids
        lim = m * (1 + band_bps / 1e4) if side is Side.BUY else m * (1 - band_bps / 1e4)
        tot = 0.0
        for lv in levels:
            if (side is Side.BUY and lv.px > lim) or (side is Side.SELL and lv.px < lim):
                break
            tot += lv.px * lv.sz
        return tot


@dataclass(frozen=True)
class Position:
    coin: str
    size: float                          # signed base units
    entry_px: float
    leverage: float = 1.0


@dataclass(frozen=True)
class OpenOrder:
    child_id: str
    coin: str
    side: Side
    size: float                          # remaining
    px: float
    reduce_only: bool = False
    oid: str = ""                        # venue order id
    cloid: str = ""                      # client order id ("" when the order has none)


@dataclass(frozen=True)
class AccountState:
    ts_ms: int
    equity_usd: float
    positions: tuple[Position, ...] = ()
    open_orders: tuple[OpenOrder, ...] = ()
    day_start_equity_usd: Optional[float] = None
    day_net_flows_usd: float = 0.0       # deposits - withdrawals today, kept out of P&L
    account_mode: Optional[str] = None   # venue account abstraction mode (None: the venue has none)
    equity_basis: str = ""               # where equity_usd comes from (omx.account_mode BASIS_*)
    equity_flags: tuple = ()             # e.g. ("EQUITY_BASIS_UNVERIFIED",)

    def position(self, coin: str) -> float:
        return sum(p.size for p in self.positions if p.coin == coin)

    def day_pnl_usd(self) -> float:
        if self.day_start_equity_usd is None:
            return 0.0
        return self.equity_usd - self.day_start_equity_usd - self.day_net_flows_usd


@dataclass(frozen=True)
class Decision:
    allow: bool
    reasons: tuple[str, ...] = ()
    checks: dict = field(default_factory=dict)


def to_record(obj) -> dict:
    """Dataclass -> JSON-safe dict (enums become their values)."""
    def conv(v):
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v
    return conv(asdict(obj))
