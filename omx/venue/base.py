"""Venue interface. The engine talks only to this; the Hyperliquid adapter and the
local simulator both implement it."""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Optional, Protocol

from ..types import AccountState, ChildOrder, Fill, MarketSnapshot

# Every OMX order carries a client order id with this 4-byte tag ("OMX" + version 1), so
# OMX orders on the account are recognisable after a restart (kill / orphan cleanup).
# R3-5: the next 4 bytes are the instance tag (derived from the engine's state_dir), so an
# engine's orphan sweep cancels only its own instance's orders, never another OMX instance's.
OMX_CLOID_TAG = "4f4d5801"
DEFAULT_INSTANCE = "00000000"


def instance_tag(state_dir) -> str:
    """4-byte hex tag of an OMX instance: sha256 of its normalised absolute state_dir path."""
    if state_dir is None:
        return DEFAULT_INSTANCE
    norm = os.path.normcase(os.path.abspath(os.fspath(state_dir)))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]


def cloid_for(child_id: str, instance: str = DEFAULT_INSTANCE) -> str:
    """Deterministic 16-byte client order id: 0x + OMX tag + instance tag + sha256(child_id)[:16 hex]."""
    if len(instance) != 8 or any(ch not in "0123456789abcdef" for ch in instance):
        raise ValueError("instance tag must be 8 lowercase hex chars")
    return "0x" + OMX_CLOID_TAG + instance + hashlib.sha256(child_id.encode("utf-8")).hexdigest()[:16]


def is_omx_cloid(cloid: Optional[str]) -> bool:
    c = (cloid or "").lower()
    return len(c) == 34 and c.startswith("0x" + OMX_CLOID_TAG)


def cloid_instance(cloid: Optional[str]) -> Optional[str]:
    """Instance tag of an OMX cloid, None for other cloids."""
    return (cloid or "").lower()[10:18] if is_omx_cloid(cloid) else None


def is_perp_coin(coin: str, perp_coins=None) -> bool:
    """R3-6: spot pairs ("@107", "PURR/USDC") and other-dex perps ("dex:COIN") are not default-dex perps.
    With a non-empty perp_coins set (the perp meta), a coin outside it is not a perp either."""
    if not coin or coin.startswith("@") or ":" in coin or "/" in coin:
        return False
    return not perp_coins or coin in perp_coins


class VenueError(RuntimeError):
    """Definite failure: the venue did not take the action."""


class VenueAborted(VenueError):
    """place() was aborted right before the send (kill switch); nothing left the machine."""


class VenueAmbiguous(VenueError):
    """The request may or may not have reached the venue (timeout, connection error, 5xx).
    expires_after_ms: venue time after which the signed action can no longer land (R3-4), if known."""

    def __init__(self, msg: str = "", expires_after_ms: Optional[int] = None):
        super().__init__(msg)
        self.expires_after_ms = expires_after_ms


class VenueUnknownAsset(VenueError):
    """The venue has no asset id for this coin (e.g. another dex); nothing was sent."""


class PlaceAck(str):
    """Venue order id returned by place(), plus any fill the place response already reported."""

    def __new__(cls, oid: str, filled_sz: float = 0.0, avg_px: Optional[float] = None):
        s = super().__new__(cls, oid)
        s.filled_sz = float(filled_sz)
        s.avg_px = avg_px
        return s


class Venue(Protocol):
    name: str

    def now_ms(self) -> int: ...

    def snapshot(self, coin: str) -> MarketSnapshot: ...

    def account(self) -> AccountState: ...

    def place(self, child: ChildOrder, abort: Optional[Callable[[], bool]] = None) -> str:
        """Submit one child order. Returns the venue order id (maybe a PlaceAck).
        Calls abort() immediately before the send and raises VenueAborted if it is true.
        Raises VenueAmbiguous when the outcome is unknown, VenueError when it was refused."""
        ...

    def cancel(self, coin: str, venue_oid: str) -> bool: ...

    def order_status(self, child_id: str) -> dict:
        """{"state": "open|filled|cancelled|rejected|unknown", "filled_sz": float, "oid": str|None}."""
        ...

    def fills_since(self, ts_ms: int) -> list[Fill]: ...

    def mark_prices(self) -> dict[str, float]: ...

    def external_flows_usd(self, start_ms: int) -> float:
        """Deposits - withdrawals +/- transfers into/out of the trading account since start_ms."""
        ...

    def tick_size(self, coin: str) -> float: ...

    def size_decimals(self, coin: str) -> int: ...
