"""Stable engine surface used by the CLI, the MCP server and the local UI.

The implementation lives in omx.engine.Engine; this module pins the shapes so
interface code and engine code can be built in parallel.

Threading (fix contract R1, item 6/8): an Engine is NOT thread-safe. In a running system the
only caller is the `omx daemon` thread; CLI and MCP are clients of the daemon.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from typing import Any, Optional, Protocol

from .types import Intent


@dataclass
class IntentReport:
    intent_id: str
    state: str                         # "accepted" | "working" | "done" | "cancelled" | "rejected" | "killed"
                                       # | "unknown" (an id this engine has no record of, e.g. after a restart)
    filled_size: float = 0.0
    avg_px: Optional[float] = None
    arrival_mid: Optional[float] = None
    shortfall_bps: Optional[float] = None   # signed cost vs arrival mid, + = worse for user
    exch_fee_usd: float = 0.0
    builder_fee_usd: float = 0.0
    children_sent: int = 0
    children_blocked: int = 0
    block_reasons: list = field(default_factory=list)
    message: str = ""


class EngineAPI(Protocol):
    def preview(self, intent: Intent) -> dict:
        """Dry run: planned children, per-child policy decision, fee estimate. Sends nothing."""
        ...

    def submit(self, intent: Intent) -> IntentReport: ...

    def status(self, intent_id: str) -> IntentReport: ...

    def cancel(self, intent_id: str) -> IntentReport: ...

    def kill(self, reason: str, scope: str = "omx") -> dict:
        """Block all new children. scope "omx": cancel every open order on the account whose cloid
        is an OMX cloid (also from earlier runs); scope "all": cancel every open order."""
        ...

    def snapshot(self, coin: str) -> Any:
        """Current MarketSnapshot of a coin."""
        ...

    def account_view(self) -> dict:
        """equity, positions, open orders, day_pnl_usd (engine day start, external flows excluded)."""
        ...

    def unkill(self, confirm: str) -> dict:
        """Only clears when confirm == "I UNDERSTAND"; the CLI asks a human."""
        ...

    def step(self) -> None:
        """Advance working intents once (poll fills, send due slices, re-price chases)."""
        ...


KILL_SCOPES = ("omx", "all")


def kill_engine(engine: Any, reason: str, scope: str = "omx") -> dict:
    """engine.kill(reason, scope). Engines without the scope parameter (pre-R1) only support "omx"."""
    if scope not in KILL_SCOPES:
        raise ValueError(f"BAD_SCOPE: one of {KILL_SCOPES}")
    try:
        has_scope = "scope" in inspect.signature(engine.kill).parameters
    except (TypeError, ValueError):
        has_scope = True
    if has_scope:
        return engine.kill(reason, scope=scope)
    if scope != "omx":
        raise ValueError("ENGINE_HAS_NO_SCOPE_ALL: this engine can only cancel its own orders")
    return engine.kill(reason)


PURPOSES = ("trade", "kill")


def build_engine(factory: Any, cfg: Any, purpose: str = "trade") -> Any:
    """factory(cfg, purpose=...) when the factory takes `purpose`, else factory(cfg) (test fakes).
    purpose "kill" = an engine for cancelling only, built without the eligibility gate (R2/B1)."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    try:
        ps = inspect.signature(factory).parameters
        takes = "purpose" in ps or any(p.kind is p.VAR_KEYWORD for p in ps.values())
    except (TypeError, ValueError):
        takes = False
    return factory(cfg, purpose=purpose) if takes else factory(cfg)
