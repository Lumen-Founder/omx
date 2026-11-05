"""Execution engine: intents -> policy-checked child orders on a Venue (implements EngineAPI).

Per step the engine re-reads the kill file, polls fills, reads the account (plus external
flows and marks), resolves child states and asks the planner (omx.algos) for at most one new
child per working intent. Every child passes omx.policy.evaluate with the live snapshot, the
live account (plus our in-flight children, valued at marks for every coin) and the kill switch;
a blocked child is recorded and never sent.

Kill: the switch is checked right before every place, the venue adapter calls the engine's
abort() again immediately before the HTTP send, and an ack that arrives after a kill is
cancelled at once. A kill cancels every OMX order on the account (by cloid), not only the
children this process knows; scope "all" cancels every open order. At start the engine
cancels orphan OMX orders left by a previous run.

Ambiguous venue outcomes (timeout, connection error, 5xx) make the child `unknown`: it stays
in exposure at full size and is resolved through order_status(child_id). R3-4: every signed
action carries expiresAfter (venue adapter), and an unknown child is resent only after that
time (plus EXPIRY_MARGIN_MS) has passed AND order_status still says unknown, so the original
can no longer land next to the resend; otherwise it is reconciled from the status. A resend
reuses the same child_id, so the same cloid.

Orphans (R2/B4, R3-5): OMX-cloid orders of THIS instance (the cloid's instance tag, derived from
state_dir) that this engine does not own are cancelled right after the first good read and again
every ORPHAN_SWEEP_MS, so an order of a previous run that lands late does not rest unowned. Orders
of another OMX instance on the same account are left alone (the daemon also holds a per-account
lock, see omx.daemon); a kill (scope "omx") still cancels every OMX order, scope "all" every order.

Non-perp orders (R3-6): spot ("@107", "PURR/USDC") and other-dex ("dex:COIN") orders and positions
are not part of the perp risk view (exposure, marks, NO_MARK); kill scope "all" cancels spot
orders too, and an order whose asset id the adapter cannot resolve is logged once and skipped.

Rate budget (R2/B3): on a rate-limited venue (one with a `limiter`, i.e. Hyperliquid) the engine
does not read everything every step; PollSchedule spaces each read kind by its weight. Idle it
uses about 500 of HL's 1200 weight/min; while children work the intervals are stretched so the
plan stays inside 70 % of the budget minus a reserve for agent reads and order actions.
account_view() and preview() use the last read instead of re-reading the account. Every info
call of the step loop is in the plan (R3-9): fills, orders, account, ledger (early reads at their
worst-case rate), marks (the adapter's snapshot uses the cached perp meta), orderStatus per open
child, and books (whose interval stretches only once books need more than half of the budget).

Deposits (R3-2): equity and the ledger are read at different rates. Between ledger reads day
P&L = min(P&L with the last known flows, P&L treating the whole equity rise since the LOWEST
equity seen since that read as a deposit), so a deposit can never lift day P&L above what the
last ledger read supports (a real gain shows up at the next ledger read). An equity rise above
the equity at the last ledger read, or more than max(1 USD, 0.5 %) above that low, triggers an
early ledger read, at most once per flow_early_ms (stretched like the other reads).

Cancel-only (R2/B1): an engine built with cancel_only=True (from_config(purpose="kill"), built
without the eligibility gate) refuses submit/preview with CANCEL_ONLY and cancels every OMX order
it sees. enter_cancel_only() switches a running engine to that mode (eligibility lost).

Builder code: with a builder configured (address + fee_tenths_bps > 0) the engine itself puts
(address, fee) into every ChildOrder it creates (ChildOrder.builder) and tells the venue the same
builder; the venue attaches {"b", "f"} to every order action and refuses a child carrying any other
builder. Callers (CLI, MCP, approvals) only build Intents, which have no builder field, so they can
neither set nor remove it. builder_approval() reads the venue's maxBuilderFee; the daemon calls it
and set_builder_block() refuses NEW intents with BUILDER_NOT_APPROVED while the approval is below
the configured fee (working intents, cancels and kill are unaffected).

Account mode (omx.account_mode): a venue with account_mode() (Hyperliquid userAbstraction, cached
10 min by the adapter) must report a standard or unified mode; any other answer, or an unreadable
one, refuses new intents with ACCOUNT_MODE_UNSUPPORTED, and the policy blocks every child of working
intents while the account read carries that mode. Kill and cancels are unaffected. The account read
names its equity basis (perp accountValue, or the unified spot-balance basis); when the basis changes
during a day (the user switched the account mode, or a restart finds a day start taken in another
basis), an equity RISE across the change is booked as a non-P&L flow and a fall stays in day P&L
(conservative both ways). The basis and that adjustment are kept in the day file.

The engine is NOT thread-safe: exactly one thread may call it. It never holds keys: signing
belongs to the venue adapter.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from . import receipt as receipt_mod
from . import tca
from .account_mode import BASIS_PERP, UNIFIED_MODES, mode_block_reason
from .algos import planner as P
from .builder import builder_of
from .audit import AuditLog
from .engine_api import IntentReport
from .killswitch import CONFIRM_PHRASE, KillSwitch
from .policy import PolicyConfig, evaluate
from .types import AccountState, Algo, ChildOrder, Intent, MarketSnapshot, OpenOrder, to_record
from .venue.base import (VenueAborted, VenueAmbiguous, VenueError, VenueUnknownAsset, cloid_for, cloid_instance,
                         instance_tag, is_omx_cloid, is_perp_coin)

DAY_MS = 86_400_000
BACKOFF_BASE_MS = 500
BACKOFF_MAX_MS = 10_000
MAX_CONSECUTIVE_BLOCKS = 20
MAX_ERRORS = 20
SETTLE_MS = 1_000                 # absence from open orders before a child counts as done
UNKNOWN_RESEND_MS = 5_000         # venue still has no record of an unknown child -> resend it (at the earliest)
EXPIRY_MARGIN_MS = 2_000          # wait this long past the action's expiresAfter (clock skew to the venue)
FILL_LOOKBACK_MS = 60_000         # fill cursor trails the newest fill by this (clock skew, late rows)
FLOW_GRACE_MS = 10_000            # flows this close before the day-start read may or may not be in it
CANCEL_RETRY_MS = 5_000           # re-cancel an account order still listed after this
TERMINAL = {"done", "cancelled", "rejected", "killed"}
SINGLE_CHILD = {Algo.MARKET, Algo.LIMIT}
UNRESOLVED = {"unknown", "resend"}
LOST = {"not_sent", "not_accepted"}   # children we gave up on; if one shows up later it is cancelled
KILL_SCOPES = ("omx", "all")
DEFAULT_FEES = {"taker": 4.5, "maker": 1.5}
ORPHAN_SWEEP_MS = 60_000          # periodic sweep of OMX orders this engine does not own
TWAP_TIF = "Twap"                 # the engine's record of a native TWAP (never sent as an order)
TWAP_POLL_MS = 10_000             # suborder fills of a running native TWAP are read this often
TWAP_GRACE_MS = 60_000            # past its running time a native TWAP is read once more, then ended
TWAP_FILE = "native_twaps.jsonl"  # started native TWAPs (state_dir): cancelled at start / kill if not owned
FLOW_RISE_USD, FLOW_RISE_FRAC = 1.0, 0.005   # equity this far above the low since the ledger read: re-read it
CANCEL_ONLY = "CANCEL_ONLY"
UNKNOWN_INTENT_MSG = ("UNKNOWN_INTENT: this engine has no record of it (e.g. it was submitted before the daemon "
                      "restarted); its outcome is in the audit log (`intent_done`, `fill` records) and on the venue")

# HL documented info weights of the reads the step loop makes.
# spot = spotClearinghouseState, read with every account read of a unified account only;
# mode = userAbstraction, planned at the adapter's cache interval (10 min). While the mode cannot be
# read the adapter retries every 60 s (20/min); that fits the reserve kept outside budget_per_min,
# and an unreadable mode blocks every child anyway.
POLL_WEIGHTS = {"fills": 20, "orders": 20, "account": 2, "flows": 20, "marks": 20, "book": 2, "status": 2,
                "spot": 2, "mode": 20}
MODE_READ_PLAN_MS = 600_000
POLL_KINDS = ("fills", "orders", "account", "flows", "marks")


@dataclass(frozen=True)
class PollSchedule:
    """Minimum ms between venue reads, as (idle, working). `working` = some child is open or the
    engine is cancelling. book is the l2Book cache per coin of a working intent; its interval
    stretches only when the books alone would need more than book_share of the budget (the policy
    blocks books older than max_data_age_ms, so about 8+ working coins then block on STALE_DATA)."""
    fills: tuple = (5_000, 1_000)            # userFillsByTime
    orders: tuple = (10_000, 2_000)          # frontendOpenOrders (+ orderStatus of open children)
    account: tuple = (2_000, 2_000)          # clearinghouseState
    flows: tuple = (30_000, 30_000)          # userNonFundingLedgerUpdates
    marks: tuple = (30_000, 10_000)          # metaAndAssetCtxs (the only perp meta refresh)
    flow_early_ms: int = 10_000              # min gap of an early ledger read after an equity rise
    book_ms: int = 1_000
    book_share: float = 0.5
    budget_per_min: float = 0.7 * 1200 - 120  # 70 % of HL's 1200/min, minus a reserve for agents and orders

    def _book(self, coins: int) -> float:
        return coins * POLL_WEIGHTS["book"] * 60_000 / self.book_ms

    def book_scale(self, coins: int) -> float:
        return max(1.0, self._book(coins) / (self.budget_per_min * self.book_share))

    def planned(self, working: bool, coins: int = 0, scale: float = 1.0, children: int = 0,
                unified: bool = False) -> float:
        """Worst-case weight/min of every info call the step loop makes at this scale
        (unified: the account read also reads spotClearinghouseState)."""
        i = int(working)
        iv = lambda k: getattr(self, k)[i] * scale              # noqa: E731
        w = sum(POLL_WEIGHTS[k] * 60_000 / iv(k) for k in ("fills", "orders", "account", "marks"))
        w += POLL_WEIGHTS["flows"] * 60_000 / min(iv("flows"), self.flow_early_ms * scale)
        w += children * POLL_WEIGHTS["status"] * 60_000 / iv("orders")
        if unified:
            w += POLL_WEIGHTS["spot"] * 60_000 / iv("account")
        return w + self._fixed() + self._book(coins) / self.book_scale(coins)

    @staticmethod
    def _fixed() -> float:
        """userAbstraction reads: cached by the adapter, so not stretched by the scale."""
        return POLL_WEIGHTS["mode"] * 60_000 / MODE_READ_PLAN_MS

    def scale(self, working: bool, coins: int = 0, children: int = 0, unified: bool = False) -> float:
        """Factor >= 1 that stretches every non-book interval so the plan fits budget_per_min."""
        fixed = self._book(coins) / self.book_scale(coins) + self._fixed()
        room = max(self.budget_per_min - fixed, 1.0)
        return max(1.0, (self.planned(working, coins, 1.0, children, unified) - fixed) / room)

    def interval(self, kind: str, working: bool, scale: float = 1.0) -> float:
        return getattr(self, kind)[int(working)] * scale


def _backoff(k: int) -> int:
    return min(BACKOFF_BASE_MS * 2 ** max(0, k - 1), BACKOFF_MAX_MS)


def _clean(v):
    """JSON-safe copy (non-finite floats become strings)."""
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _accepts_abort(fn) -> bool:
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "abort" in ps or any(p.kind is p.VAR_KEYWORD for p in ps.values())


def native_twap_gate(cfg: PolicyConfig, venue) -> Optional[str]:
    if not cfg.allow_native_twap:
        return "NATIVE_TWAP_DISABLED: policy.allow_native_twap is false"
    if not hasattr(venue, "place_twap"):
        return "NATIVE_TWAP_UNSUPPORTED: this venue has no protocol TWAP"
    return None


def native_twap_policy(cfg: PolicyConfig) -> PolicyConfig:
    """The whole TWAP is one check at full size; max_child_notional_usd applies to the estimated suborder
    (native_twap_checks), not to the parent."""
    return replace(cfg, max_child_notional_usd=math.inf)


def native_twap_checks(intent: Intent, notional_usd: float, cfg: PolicyConfig) -> tuple[list, dict]:
    """Extra reasons for a native TWAP and the numbers behind them. The protocol may catch up to
    NATIVE_TWAP_CATCHUP times the even suborder size, so that is the size held to max_child_notional_usd."""
    n = max(1, intent.duration_s // P.NATIVE_TWAP_SUBORDER_S)
    sub = notional_usd / n * P.NATIVE_TWAP_CATCHUP
    more = []
    if notional_usd < P.NATIVE_TWAP_MIN_USD:
        more.append(f"NATIVE_TWAP_MIN_NOTIONAL:{P.NATIVE_TWAP_MIN_USD:g}")
    if sub > cfg.max_child_notional_usd and not intent.reduce_only:
        more.append(f"NATIVE_TWAP_SUBORDER_CAP:{sub:.0f}>{cfg.max_child_notional_usd:g}")
    return more, {"suborders": n, "suborder_bound_usd": round(sub, 2),
                  "suborder_max_slippage_bps": P.NATIVE_TWAP_SLIPPAGE_BPS, "builder_fee": None}


@dataclass
class _Child:
    order: ChildOrder
    oid: Optional[str]
    sent_ms: int
    filled: float = 0.0               # from fills
    ack_filled: float = 0.0           # from the place response / order_status
    terminal: bool = False
    cancel_pending: bool = False
    status: str = "sent"
    resend_after: int = 0             # an unknown child is not resent before this (its action's expiry)
    end_ms: int = 0                   # native TWAP: start + running time
    read_ms: int = 0                  # native TWAP: last suborder-fill read
    seen: set = field(default_factory=set)   # native TWAP: suborder fill keys already booked

    def done_sz(self) -> float:
        return max(self.filled, self.ack_filled)

    def remaining(self) -> float:
        return max(0.0, self.order.size - self.done_sz())


@dataclass
class _Work:
    intent: Intent
    state: str
    arrival_mid: Optional[float]
    start_ms: int
    algo: P.AlgoState = field(default_factory=P.AlgoState)
    children: dict = field(default_factory=dict)        # child_id -> _Child
    fills: list = field(default_factory=list)
    issued: int = 0
    sent: int = 0
    blocked: int = 0
    errors: int = 0
    rejects: list = field(default_factory=list)          # venue-side rejections (e.g. ALO_WOULD_CROSS)
    consec_blocks: int = 0
    reasons: list = field(default_factory=list)
    next_try_ms: int = 0
    done_ms: Optional[int] = None
    finalized: bool = False
    message: str = ""

    def open_children(self) -> list:
        return [c for c in self.children.values() if not c.terminal]

    def filled(self) -> float:
        return sum(c.done_sz() for c in self.children.values())


class Engine:
    def __init__(self, venue, policy_cfg: PolicyConfig, audit: AuditLog, builder_fee_tenths_bps: int = 0,
                 state_dir: Optional[str | os.PathLike] = None, clock: Optional[Callable[[], int]] = None,
                 cancel_only: bool = False, poll="auto", builder_address: Optional[str] = None):
        """poll: "auto" = PollSchedule() on a rate-limited venue (has `limiter`), else read every step;
        a PollSchedule; or None/False = read everything every step.
        builder_address + builder_fee_tenths_bps > 0: every child order carries this builder."""
        self.venue = venue
        if poll == "auto":
            poll = PollSchedule() if getattr(venue, "limiter", None) is not None else None
        self.poll: Optional[PollSchedule] = poll or None
        self.cancel_only = bool(cancel_only)
        self._cancel_only_reason = "built for kill" if cancel_only else ""
        self._polled: dict[str, int] = {}         # read kind -> last read ms
        self._scale = 1.0
        self._book_scale = 1.0
        self._books: dict[str, tuple] = {}        # coin -> (read ms, snapshot), used when poll is on
        self._flow_val: Optional[float] = None
        self._flow_equity = 0.0                   # equity at the last ledger read
        self._flow_low = 0.0                      # lowest equity seen since the last ledger read
        self._skipped: set = set()                # venue oids whose cancel cannot be built (unknown asset)
        self._cancel_skips = 0
        self._orphan_ms: Optional[int] = None
        self._cancel_errs = 0
        self.cfg = policy_cfg
        self.audit = audit
        self.builder_fee_tenths_bps = int(builder_fee_tenths_bps)
        if self.builder_fee_tenths_bps > 0 and not builder_address:
            raise ValueError("a builder fee > 0 needs a builder address (fail closed: no fee without its builder)")
        self.builder: Optional[tuple] = builder_of(builder_address, self.builder_fee_tenths_bps)
        self._builder_block: Optional[str] = None
        self._receipt_anchors: dict = {}           # (intent_id, receipt_hash) -> audit line hash
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.instance = instance_tag(self.state_dir)     # in every cloid this engine sends (R3-5)
        self.clock = clock or venue.now_ms
        self.ks = KillSwitch(state_dir)
        self._killed = False              # a kill file present at start is handled by the first check
        self._kill_scope = "omx"
        self._started = False
        self._work: dict[str, _Work] = {}
        self._child_intent: dict[str, str] = {}
        self._cursor: Optional[int] = None
        self._seen: dict = {}             # fill key -> ts_ms
        self._day: Optional[int] = None
        self._day_start: Optional[float] = None
        self._day_ts: Optional[int] = None
        self._basis: Optional[str] = None         # equity basis of the day start (None: legacy day file)
        self._basis_adj = 0.0                     # equity rises across basis changes today (not P&L)
        self._backoff_until = 0
        self._venue_errs = 0
        self._snaps: dict[str, MarketSnapshot] = {}
        self._marks: dict[str, float] = {}
        self._acct: Optional[AccountState] = None
        self._raw: Optional[AccountState] = None
        self._cancel_sent: dict[str, int] = {}   # venue oid -> last cancel request ms
        if hasattr(venue, "set_builder"):
            venue.set_builder(self.builder)
        elif hasattr(venue, "set_builder_fee"):
            venue.set_builder_fee(self.builder_fee_tenths_bps)
        vb = getattr(venue, "builder", False)
        if vb is not False:                       # the venue knows its builder: it must be ours
            norm = None if not vb else ((vb["b"], vb["f"]) if isinstance(vb, dict) else (vb[0], vb[1]))
            norm = None if norm is None else (str(norm[0]).lower(), int(norm[1]))
            if norm != self.builder:
                raise ValueError(f"BUILDER_MISMATCH: venue builder {norm} != engine builder {self.builder}")
        self._load_day()

    # ------------------------------------------------------------ persistence
    def _day_file(self) -> Optional[Path]:
        return self.state_dir / "engine_day.json" if self.state_dir is not None else None

    def _load_day(self) -> None:
        p = self._day_file()
        if p is None or not p.exists():
            return
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            self._day, self._day_start = int(d["day"]), float(d["equity_usd"])
            self._day_ts = int(d.get("ts_ms", self._day * DAY_MS))
            self._basis = d.get("basis")
            self._basis_adj = float(d.get("basis_adj") or 0.0)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_day(self) -> None:
        p = self._day_file()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": self._day, "equity_usd": self._day_start, "ts_ms": self._day_ts,
                                   "basis": self._basis, "basis_adj": self._basis_adj}), encoding="utf-8")
        os.replace(tmp, p)

    # ------------------------------------------------------------ venue reads
    def _perp(self, coin: str) -> bool:
        fn = getattr(self.venue, "perp_coins", None)
        return is_perp_coin(coin, fn() if fn is not None else None)

    def _snap(self, coin: str) -> MarketSnapshot:
        if coin not in self._snaps:
            hit = self._books.get(coin) if self.poll is not None else None
            now = self.clock()
            if hit is not None and now - hit[0] < self.poll.book_ms * self._book_scale:
                self._snaps[coin] = hit[1]
            else:
                self._snaps[coin] = self.venue.snapshot(coin)
                self._books[coin] = (now, self._snaps[coin])
        return self._snaps[coin]

    def _refs(self) -> dict:
        refs = dict(self._marks)
        refs.update({c: s.mid for c, s in self._snaps.items() if s.mid})
        return refs

    def _read_marks(self) -> dict:
        fn = getattr(self.venue, "mark_prices", None)
        return {c: float(px) for c, px in fn().items() if px} if fn is not None else {}

    def _poll_fills(self) -> None:
        if self._cursor is None:
            self._cursor = self.clock() - FILL_LOOKBACK_MS
        fills = self.venue.fills_since(self._cursor)
        occ, hi = Counter(), self._cursor
        for f in fills:
            hi = max(hi, f.ts_ms)
            if f.fill_id:
                key = ("id", f.fill_id)
            else:                     # no venue id: identical rows are told apart by their order
                base = (f.child_id, f.ts_ms, f.size, f.px, f.side.value, f.crossed)
                occ[base] += 1
                key = base + (occ[base],)
            if key in self._seen:
                continue
            self._seen[key] = f.ts_ms
            iid = self._child_intent.get(f.child_id)
            if iid is None:
                continue
            w = self._work[iid]
            w.fills.append(f)
            w.children[f.child_id].filled += f.size
            self.audit.append("fill", {"intent_id": iid, "fill": to_record(f)})
        self._cursor = max(self._cursor, hi - FILL_LOOKBACK_MS)
        self._seen = {k: t for k, t in self._seen.items() if t >= self._cursor}

    def _flows(self) -> float:
        """External flows since the day-start read. Flows within FLOW_GRACE_MS before that read
        may or may not be in it: deposits are counted, withdrawals not (the lower P&L reading)."""
        cap = self._day_ts
        ev = getattr(self.venue, "external_flow_events", None)
        if ev is not None:
            return float(sum(u for t, u in ev(cap - FLOW_GRACE_MS) if t > cap or u > 0))
        fn = getattr(self.venue, "external_flows_usd", None)
        return float(fn(cap + 1)) if fn is not None else 0.0

    def _account(self, now: int, raw: Optional[AccountState] = None, fresh: bool = True,
                 flows_due: bool = True) -> AccountState:
        """Account state for the policy. `fresh` = raw.open_orders was just read (child states are
        resolved only against a fresh listing); `flows_due` = re-read the ledger."""
        raw = raw if raw is not None else self.venue.account()
        day = now // DAY_MS
        basis = raw.equity_basis or ""
        if day != self._day or self._day_start is None:
            self._day, self._day_start, self._day_ts = day, raw.equity_usd, raw.ts_ms
            self._basis, self._basis_adj = basis, 0.0
            self._save_day()
            self.audit.append("day_start", {"day": day, "equity_usd": raw.equity_usd, "ts_ms": raw.ts_ms,
                                            "equity_basis": basis, "account_mode": raw.account_mode,
                                            "equity_flags": list(raw.equity_flags), "now_ms": now})
            flows_due = True
        if self._day_ts is None:
            self._day_ts = day * DAY_MS
        if flows_due or self._flow_val is None:
            self._flow_val, self._flow_equity, self._flow_low = self._flows(), raw.equity_usd, raw.equity_usd
        if not (basis == self._basis or (self._basis is None and basis in ("", BASIS_PERP))):
            # equity basis changed (account mode switch, or a day start from another basis / a legacy
            # day file, which v1 always took from the perp account value): a rise across the change is
            # not P&L (booked like a deposit), a fall stays in day P&L. Both are the conservative side.
            before = self._raw.equity_usd if self._raw is not None else self._day_start
            rise = max(0.0, raw.equity_usd - before)
            self._basis_adj += rise
            self.audit.append("equity_basis_change", {"from": self._basis, "to": basis,
                                                      "account_mode": raw.account_mode, "equity_before": before,
                                                      "equity_after": raw.equity_usd, "booked_as_flow_usd": rise,
                                                      "now_ms": now})
            self._basis = basis
            self._save_day()
            self._flow_equity = self._flow_low = raw.equity_usd       # re-anchor the R3-2 deposit guard
        # R3-2: until the next ledger read, the whole rise since the lowest equity seen since the
        # last one may be a deposit: day P&L = min(with known flows, with that rise as a deposit).
        self._flow_low = min(self._flow_low, raw.equity_usd)
        flows = self._flow_val + self._basis_adj + max(0.0, raw.equity_usd - self._flow_low)
        self._raw = raw
        if fresh:
            self._refresh(raw, now)
            lost = {cid for w in self._work.values() for cid, c in w.children.items() if c.status in LOST}
            strays = [o for o in raw.open_orders if o.child_id in lost]
            if strays:                # a child we gave up on landed after all
                self._cancel_venue_orders(strays, "all", "STRAY")
        listed = {o.child_id for o in raw.open_orders}
        extra = tuple(OpenOrder(c.order.child_id, c.order.coin, c.order.side, c.remaining(), c.order.px,
                                c.order.reduce_only, c.oid or "", c.order.cloid)
                      for w in self._work.values() for c in w.open_children() if c.order.child_id not in listed)
        # the policy's risk view: default-dex perps only (spot / other-dex entries have no perp mark)
        return replace(raw, day_start_equity_usd=self._day_start, day_net_flows_usd=flows,
                       positions=tuple(p for p in raw.positions if self._perp(p.coin)),
                       open_orders=tuple(o for o in raw.open_orders if self._perp(o.coin)) + extra)

    def _status(self, child_id: str) -> Optional[dict]:
        fn = getattr(self.venue, "order_status", None)
        return fn(child_id) if fn is not None else None

    def _end_child(self, w: _Work, c: _Child, state: str, st: Optional[dict] = None) -> None:
        c.status, c.terminal, c.cancel_pending = state, True, False
        if state == "rejected":
            why = (st or {}).get("reason") or "rejected"
            w.rejects.append(why)
            self.audit.append("child_error", {"intent_id": w.intent.intent_id, "child_id": c.order.child_id,
                                              "oid": c.oid, "error": why})

    def _refresh(self, raw: AccountState, now: int) -> None:
        listed = {o.child_id: o for o in raw.open_orders}
        for w in self._work.values():
            for c in w.open_children():
                if c.order.tif == TWAP_TIF:   # never listed: read through twap_fills (_poll_twaps)
                    continue
                o = listed.get(c.order.child_id)
                if c.status in UNRESOLVED:
                    self._resolve(w, c, o, now)
                    continue
                if o is not None:
                    continue
                st = self._status(c.order.child_id)
                if st is None:
                    if now - c.sent_ms < SETTLE_MS:
                        continue
                    self._end_child(w, c, "gone")
                    continue
                state = st.get("state", "unknown")
                if state == "open" or (state == "unknown" and now - c.sent_ms < SETTLE_MS):
                    continue
                c.ack_filled = max(c.ack_filled, float(st.get("filled_sz") or 0.0))
                self._end_child(w, c, "gone" if state == "unknown" else state, st)

    def _resolve(self, w: _Work, c: _Child, listed: Optional[OpenOrder], now: int) -> None:
        """Settle an unknown child from the account listing or order_status; never guess."""
        cid = c.order.child_id
        if listed is not None:
            st = {"state": "open", "oid": listed.oid or None, "filled_sz": 0.0}
        else:
            try:
                st = self._status(cid)
            except Exception as e:  # stays unknown (and in exposure); retried next step
                self.audit.append("venue_error", {"where": "order_status", "child_id": cid, "error": repr(e),
                                                  "now_ms": now})
                return
            if st is None:
                return                # no way to ask: it stays unknown until it shows up
        state = st.get("state", "unknown")
        if state == "unknown":
            if now < max(c.resend_after, c.sent_ms + UNKNOWN_RESEND_MS):
                return                # the original may still land: wait past its expiresAfter
            if w.state in TERMINAL or self._killed:
                self._end_child(w, c, "not_accepted")
            elif c.status == "resend":
                return
            else:
                c.status = "resend"
            self.audit.append("child_resolved", {"intent_id": w.intent.intent_id, "child_id": cid,
                                                 "venue_state": "unknown", "status": c.status, "now_ms": now})
            return
        c.oid = st.get("oid") or c.oid
        c.ack_filled = max(c.ack_filled, float(st.get("filled_sz") or 0.0))
        self.audit.append("child_resolved", {"intent_id": w.intent.intent_id, "child_id": cid, "oid": c.oid,
                                             "venue_state": state, "filled_sz": c.ack_filled, "now_ms": now})
        if state != "open":
            self._end_child(w, c, state, st)
            return
        c.status = "working"
        if self._killed or w.state in TERMINAL:   # it landed after we stopped wanting it
            self._cancel_child(w, c, "KILL" if self._killed else "INTENT_" + w.state.upper())

    # ------------------------------------------------------------ kill switch
    def _is_killed(self) -> bool:
        if self.ks.is_killed() and (not self._killed or self._kill_scope != "all"):
            info = self.ks.info() or {}
            scope = info.get("scope") if info.get("scope") in KILL_SCOPES else "omx"
            if not self._killed or scope == "all":           # new kill, or a stored scope that widened
                self._kill_procedure(info.get("reason", "external kill"), info.get("source", "external"), scope)
        return self._killed or self.ks.is_killed()

    def _abort(self) -> bool:
        return self._killed or self.ks.is_killed()

    def _cancel_child(self, w: _Work, c: _Child, reason: str) -> None:
        if c.terminal or c.cancel_pending or not c.oid:
            return                    # an unknown child is cancelled once it is resolved
        if c.order.tif == TWAP_TIF:
            self._cancel_twap(w, c, reason)
            return
        try:
            ok = bool(self.venue.cancel(c.order.coin, c.oid))
        except Exception as e:  # venue errors must not stop the engine
            self.audit.append("cancel", {"intent_id": w.intent.intent_id, "child_id": c.order.child_id,
                                         "oid": c.oid, "ok": False, "reason": reason, "error": repr(e)})
            return
        c.cancel_pending = ok
        self._cancel_sent[c.oid] = self.clock()
        self.audit.append("cancel", {"intent_id": w.intent.intent_id, "child_id": c.order.child_id,
                                     "oid": c.oid, "ok": ok, "reason": reason, "now_ms": self.clock()})

    def _cancel_twap(self, w: _Work, c: _Child, reason: str) -> None:
        """Stop a native TWAP (twapCancel). Its last suborder fills are read SETTLE_MS later, then it ends."""
        now = self.clock()
        try:
            ok = bool(self.venue.cancel_twap(c.order.coin, c.oid.split(":", 1)[1]))
        except Exception as e:  # retried at the next step
            self.audit.append("cancel", {"intent_id": w.intent.intent_id, "child_id": c.order.child_id,
                                         "oid": c.oid, "ok": False, "reason": reason, "native_twap": True,
                                         "error": repr(e), "now_ms": now})
            return
        c.cancel_pending, self._cancel_sent[c.oid] = True, now
        if ok:
            c.status = "cancelling"
        self.audit.append("cancel", {"intent_id": w.intent.intent_id, "child_id": c.order.child_id,
                                     "oid": c.oid, "ok": ok, "reason": reason, "native_twap": True, "now_ms": now})

    def _poll_twaps(self, now: int) -> None:
        """Book suborder fills of running native TWAPs and end them once filled, cancelled or past their time."""
        for w in self._work.values():
            for c in w.open_children():
                if c.order.tif != TWAP_TIF:
                    continue
                if c.status == "unknown":       # the start's outcome is unknown: no id to read or cancel
                    if now >= c.end_ms + TWAP_GRACE_MS:
                        self._end_child(w, c, "gone")
                        self.audit.append("child_resolved", {"intent_id": w.intent.intent_id,
                                                             "child_id": c.order.child_id, "venue_state": "unknown",
                                                             "status": "gone", "native_twap": True, "now_ms": now})
                    continue
                ending = (c.cancel_pending and now - self._cancel_sent.get(c.oid, now) >= SETTLE_MS) \
                    or now >= c.end_ms + TWAP_GRACE_MS
                if not ending and now - c.read_ms < TWAP_POLL_MS:
                    continue
                c.read_ms = now
                try:
                    fills = self.venue.twap_fills(c.oid.split(":", 1)[1])
                except Exception as e:  # read again next step
                    self.audit.append("venue_error", {"where": "twap_fills", "child_id": c.order.child_id,
                                                      "error": repr(e), "now_ms": now})
                    continue
                for f in fills:
                    key = f.fill_id or (f.ts_ms, f.size, f.px, f.side.value)
                    if key in c.seen:
                        continue
                    c.seen.add(key)
                    f = replace(f, child_id=c.order.child_id)
                    w.fills.append(f)
                    c.filled += f.size
                    self.audit.append("fill", {"intent_id": w.intent.intent_id, "fill": to_record(f),
                                               "native_twap": True})
                if c.remaining() <= 1e-12:
                    self._end_child(w, c, "filled")
                elif ending:
                    self._end_child(w, c, "cancelled" if c.status == "cancelling" else "finished")

    def _twap_file(self) -> Optional[Path]:
        return self.state_dir / TWAP_FILE if self.state_dir is not None else None

    def _store_twap(self, w: _Work, c: _Child) -> None:
        path = self._twap_file()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"twap_id": c.oid.split(":", 1)[1], "coin": c.order.coin, "child_id": c.order.child_id,
               "intent_id": w.intent.intent_id, "end_ms": c.end_ms}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _cancel_stored_twaps(self, reason: str) -> int:
        """Cancel native TWAPs started from this state_dir by an earlier run (not owned here) that may still
        run. A protocol TWAP has no cloid and is not an open order, so the account sweeps cannot see it."""
        path = self._twap_file()
        if path is None or not path.exists() or not hasattr(self.venue, "cancel_twap"):
            return 0
        now, keep, n = self.clock(), [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                end = int(r["end_ms"])
            except Exception:  # a torn last line: nothing to act on
                continue
            if now >= end + TWAP_GRACE_MS:
                continue
            if r.get("child_id") in self._child_intent:
                keep.append(r)
                continue
            try:
                ok, err = bool(self.venue.cancel_twap(r["coin"], str(r["twap_id"]))), None
            except Exception as e:  # kept: tried again at the next start / kill
                ok, err = False, repr(e)
                keep.append(r)
            n += err is None
            self.audit.append("cancel", {"scope": "native_twap", "twap_id": r["twap_id"], "coin": r["coin"],
                                         "child_id": r.get("child_id"), "ok": ok, "reason": reason,
                                         **({"error": err} if err else {}), "now_ms": now})
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in keep), encoding="utf-8")
        os.replace(tmp, path)
        return n

    def _cancel_everything(self, reason: str) -> int:
        n = 0
        for w in self._work.values():
            for c in w.open_children():
                if not c.cancel_pending and c.oid:
                    self._cancel_child(w, c, reason)
                    n += 1
        return n

    def _cancel_venue_orders(self, orders, scope: str, reason: str) -> int:
        """Cancel account orders by venue oid: OMX cloids only, or every order for scope "all"."""
        n, now = 0, self.clock()
        self._cancel_errs = self._cancel_skips = 0
        for o in orders:
            if not o.oid or (scope != "all" and not is_omx_cloid(o.cloid)):
                continue
            if o.oid in self._skipped:            # no asset id for it: logged once, never retried
                self._cancel_skips += 1
                continue
            t = self._cancel_sent.get(o.oid)
            if t is not None and now - t < CANCEL_RETRY_MS:
                continue
            self._cancel_sent[o.oid] = now
            skipped = False
            try:
                ok = bool(self.venue.cancel(o.coin, o.oid))
                err = None
            except VenueUnknownAsset as e:          # e.g. another dex: nothing sent, no meta refetch loop
                ok, err, skipped = False, repr(e), True
                self._skipped.add(o.oid)
                self._cancel_skips += 1
            except Exception as e:  # retried at the next sweep
                ok, err = False, repr(e)
                del self._cancel_sent[o.oid]
                self._cancel_errs += 1
            rec = {"scope": scope, "child_id": o.child_id, "oid": o.oid, "cloid": o.cloid, "coin": o.coin,
                   "ok": ok, "reason": reason, "now_ms": now}
            if err:
                rec["error"] = err
            if skipped:
                rec["skipped"] = "UNKNOWN_ASSET"
            self.audit.append("cancel_skipped" if skipped else "cancel", rec)
            n += err is None
        return n

    def _kill_procedure(self, reason: str, source: str, scope: str = "omx") -> dict:
        now = self.clock()
        if self._killed and self._kill_scope == "all":
            scope = "all"                                       # a scope never narrows while killed
        self._killed, self._kill_scope = True, scope
        for w in self._work.values():
            if w.state not in TERMINAL:
                w.state, w.done_ms, w.message = "killed", now, f"killed: {reason}"
        n = self._cancel_everything("KILL")
        rec = {"killed": True, "reason": reason, "source": source, "scope": scope, "ts_ms": now}
        try:
            n += self._cancel_stored_twaps("KILL")
        except Exception as e:  # the next start tries the stored TWAPs again
            rec["twap_sweep_error"] = repr(e)
        try:
            n += self._cancel_venue_orders(self.venue.account().open_orders, scope, "KILL")
            if self._cancel_errs:
                rec["cancel_errors"] = self._cancel_errs
            if self._cancel_skips:
                rec["cancels_skipped"] = self._cancel_skips   # still resting: cancel them in the venue UI
        except Exception as e:  # the killed step loop sweeps the account again
            rec["venue_sweep_error"] = repr(e)
        rec["cancels_requested"] = n
        self.audit.append("kill", rec)
        return rec

    def kill(self, reason: str, scope: str = "omx") -> dict:
        """Stop trading. scope "omx": cancel every OMX order on the account; "all": every open order.
        The KILL file stores the scope (a stored "all" is kept), so later engines honour it."""
        if scope not in KILL_SCOPES:
            raise ValueError(f"kill scope must be one of {KILL_SCOPES}")
        stored = self.ks.kill(reason, self.clock(), source="engine", scope=scope)
        return self._kill_procedure(reason, "engine", stored.get("scope", scope))

    def enter_cancel_only(self, reason: str) -> dict:
        """Stop starting intents and children for good (this engine's lifetime): working intents are
        cancelled with their children, and every OMX order on the account is cancelled now and at
        every later fresh listing. Used when eligibility is lost while the daemon runs."""
        now = self.clock()
        self.cancel_only, self._cancel_only_reason = True, str(reason)[:300]
        n = 0
        for w in self._work.values():
            if w.state not in TERMINAL:
                w.state, w.done_ms, w.message = "cancelled", now, f"cancelled: {CANCEL_ONLY}: {reason}"
                self._cancel_all_of(w, CANCEL_ONLY)
        rec = {"reason": self._cancel_only_reason, "ts_ms": now}
        try:
            n += self._cancel_venue_orders(self.venue.account().open_orders, "omx", CANCEL_ONLY)
        except Exception as e:  # the step loop sweeps again
            rec["venue_sweep_error"] = repr(e)
        rec["cancels_requested"] = n
        self.audit.append("cancel_only", rec)
        self._finalize()
        return rec

    def unkill(self, confirm: str) -> dict:
        ok = confirm == CONFIRM_PHRASE and self.ks.clear(confirm)
        if ok:
            self._killed, self._kill_scope = False, "omx"
        self.audit.append("unkill", {"ok": ok, "now_ms": self.clock()})
        return {"ok": ok, "killed": self._killed or self.ks.is_killed(),
                "message": "cleared; killed intents stay killed" if ok else f'confirm must be "{CONFIRM_PHRASE}"'}

    def _start(self, raw: AccountState) -> None:
        """First successful read: cancel OMX orders this process does not own (previous run)."""
        self._started = True
        if self._killed:
            self._orphan_ms = self.clock()
            return                    # the kill procedure already swept the account
        self._cancel_stored_twaps("ORPHAN")
        self._sweep_orphans(raw, "start")

    def _sweep_orphans(self, raw: AccountState, phase: str) -> None:
        """Cancel this instance's OMX-cloid orders on the account that this engine does not own. Runs
        at start and every ORPHAN_SWEEP_MS: an order of a dead run can land after this engine started.
        Orders with another instance tag belong to another OMX instance and are left alone."""
        self._orphan_ms = self.clock()
        orphans = [o for o in raw.open_orders if cloid_instance(o.cloid) == self.instance
                   and o.child_id not in self._child_intent]
        if orphans:
            n = self._cancel_venue_orders(orphans, "omx", "ORPHAN")
            self.audit.append("orphans_cancelled", {"count": len(orphans), "cancels_requested": n, "phase": phase,
                                                    "orders": [to_record(o) for o in orphans],
                                                    "now_ms": self.clock()})

    # ------------------------------------------------------------ child path
    def _ctx(self, w: _Work, snap: MarketSnapshot, now: int) -> P.Ctx:
        coin = w.intent.coin
        vol = 0.0
        trades_fn = getattr(self.venue, "trades_since", None)
        if trades_fn is not None:
            vol = sum(t.sz for t in trades_fn(coin, w.start_ms + 1) if not getattr(t, "own", False))
        working = tuple(P.Working(c.order.child_id, c.order.px, c.remaining(), c.order.tif, c.cancel_pending)
                        for c in w.open_children())
        return P.Ctx(now, w.start_ms, w.arrival_mid or snap.mid or 0.0, snap.mid, snap.best_bid, snap.best_ask,
                     self.venue.tick_size(coin), self.venue.size_decimals(coin), w.filled(), working, vol)

    def _reject(self, w: _Work, now: int, msg: str) -> None:
        w.state, w.done_ms, w.message = "rejected", now, msg
        self._cancel_all_of(w, "REJECTED")

    def _cancel_all_of(self, w: _Work, reason: str) -> None:
        for c in w.open_children():
            self._cancel_child(w, c, reason)

    def _decide(self, w: _Work, child: ChildOrder, snap: MarketSnapshot, acct: AccountState, now: int,
                cfg: Optional[PolicyConfig] = None, more: tuple = (), **extra) -> list:
        d = evaluate(child, w.intent, snap, acct, cfg or self.cfg, now, self._is_killed(), self._refs())
        reasons = list(d.reasons) + list(more)
        if d.allow and self._is_killed():                       # last guard right before sending
            reasons = ["KILL_SWITCH_ACTIVE"]
        self.audit.append("child_decision", {"intent_id": w.intent.intent_id, "child": to_record(child),
                                             "allow": not reasons, "reasons": reasons, "checks": _clean(d.checks),
                                             "now_ms": now, **extra})
        if reasons:
            w.blocked += 1
            w.consec_blocks += 1
            for r in reasons:
                if r not in w.reasons and len(w.reasons) < 20:
                    w.reasons.append(r)
        return reasons

    def _send(self, w: _Work, a: P.Place, snap: MarketSnapshot, now: int) -> bool:
        it = w.intent
        w.issued += 1
        cid = f"{it.intent_id}.{w.issued}"
        child = ChildOrder(cid, it.intent_id, it.coin, it.side, a.size, a.px, a.tif, it.reduce_only,
                           cloid_for(cid, self.instance), builder=self.builder)
        reasons = self._decide(w, child, snap, self._acct, now)
        if reasons:
            w.next_try_ms = now + _backoff(w.consec_blocks)
            if it.algo in SINGLE_CHILD or w.consec_blocks >= MAX_CONSECUTIVE_BLOCKS or self._killed:
                self._reject(w, now, "blocked: " + ",".join(reasons))
            return False
        self.audit.append("child_sent", {"intent_id": it.intent_id, "child_id": child.child_id, "now_ms": now})
        return self._place(w, child, now, None)

    def _resend(self, w: _Work, c: _Child, snap: MarketSnapshot, now: int) -> None:
        """The venue never got this child: send the same child (same cloid) again, re-checked."""
        cid = c.order.child_id
        acct = replace(self._acct, open_orders=tuple(o for o in self._acct.open_orders if o.child_id != cid))
        if self._decide(w, c.order, snap, acct, now, resend=True):
            self._end_child(w, c, "not_sent")
            return
        self.audit.append("child_sent", {"intent_id": w.intent.intent_id, "child_id": cid, "resend": True,
                                         "now_ms": now})
        self._place(w, c.order, now, c)

    def _place(self, w: _Work, child: ChildOrder, now: int, c: Optional[_Child]) -> bool:
        it = w.intent
        if child.builder != self.builder:          # cannot happen through the engine; fail closed
            self.audit.append("child_error", {"intent_id": it.intent_id, "child_id": child.child_id,
                                              "error": "BUILDER_MISMATCH", "now_ms": now})
            if c is None:
                self._reject(w, now, "BUILDER_MISMATCH")
            else:
                self._end_child(w, c, "not_sent")
            return False
        try:
            if _accepts_abort(self.venue.place):
                ack = self.venue.place(child, abort=self._abort)
            else:
                ack = self.venue.place(child)
        except VenueAborted as e:
            self.audit.append("child_aborted", {"intent_id": it.intent_id, "child_id": child.child_id,
                                                "error": repr(e), "now_ms": now})
            if c is not None:                             # a resend: the original is still unresolved
                c.status, c.sent_ms = "unknown", now
                c.resend_after = max(c.resend_after, now + UNKNOWN_RESEND_MS)
            self._is_killed()
            return False
        except Exception as e:
            if c is None and isinstance(e, VenueError) and not isinstance(e, VenueAmbiguous):
                w.errors += 1                             # definite refusal: nothing is live
                w.next_try_ms = now + _backoff(w.errors)
                self.audit.append("child_error", {"intent_id": it.intent_id, "child_id": child.child_id,
                                                  "error": repr(e)})
                if w.errors >= MAX_ERRORS:
                    self._reject(w, now, f"too many venue errors: {e!r}")
                return False
            return self._unknown(w, child, now, c, e)
        oid = str(ack)
        filled = float(getattr(ack, "filled_sz", 0.0) or 0.0)
        self.audit.append("child_ack", {"intent_id": it.intent_id, "child_id": child.child_id, "oid": oid,
                                        "filled_sz": filled})
        if c is None:
            c = self._register(w, child, oid, now, "sent")
        else:
            c.oid, c.status, c.sent_ms = oid, "sent", now
        c.ack_filled = max(c.ack_filled, filled)
        w.consec_blocks = 0
        if w.state not in TERMINAL:
            w.state = "working"
        if self._is_killed():                                   # the ack came back after a kill
            if w.state not in TERMINAL:
                w.state, w.done_ms, w.message = "killed", now, "killed"
            self._cancel_child(w, c, "KILL_AFTER_ACK")
        return True

    def _register(self, w: _Work, child: ChildOrder, oid: Optional[str], now: int, status: str) -> _Child:
        c = _Child(child, oid, now, status=status)
        w.children[child.child_id] = c
        self._child_intent[child.child_id] = w.intent.intent_id
        w.sent += 1
        self._acct = replace(self._acct, open_orders=self._acct.open_orders +
                             (OpenOrder(child.child_id, child.coin, child.side, child.size, child.px,
                                        child.reduce_only, oid or "", child.cloid),))
        return c

    def _unknown(self, w: _Work, child: ChildOrder, now: int, c: Optional[_Child], e: Exception) -> bool:
        """Outcome unknown: keep the child in exposure and resolve it before any resend."""
        w.errors += 1
        w.next_try_ms = now + _backoff(w.errors)
        exp = getattr(e, "expires_after_ms", None)
        self.audit.append("child_unknown", {"intent_id": w.intent.intent_id, "child_id": child.child_id,
                                            "error": repr(e), "expires_after_ms": exp, "now_ms": now})
        if c is None:
            c = self._register(w, child, None, now, "unknown")
            w.consec_blocks = 0
            if w.state not in TERMINAL:
                w.state = "working"
        else:
            c.status, c.sent_ms = "unknown", now
        c.resend_after = max(c.resend_after, now + UNKNOWN_RESEND_MS,
                             exp + EXPIRY_MARGIN_MS if exp is not None else 0)
        if w.errors >= MAX_ERRORS:
            self._reject(w, now, f"too many venue errors: {e!r}")
        self._is_killed()
        return True

    def _advance(self, w: _Work, now: int) -> None:
        if w.state in TERMINAL or now < w.next_try_ms:
            return
        snap = self._snap(w.intent.coin)
        if w.intent.algo is Algo.NATIVE_TWAP:
            self._advance_native(w, snap, now)
            return
        resend = next((c for c in w.open_children() if c.status == "resend"), None)
        if resend is not None:
            self._resend(w, resend, snap, now)
            return
        pl = P.plan(w.intent, w.algo, self._ctx(w, snap, now))
        for a in pl.actions:
            if isinstance(a, P.Cancel) and a.child_id in w.children:
                self._cancel_child(w, w.children[a.child_id], a.reason)
        places = [a for a in pl.actions if isinstance(a, P.Place)]
        if not places:
            w.algo = pl.state
            if pl.done:
                w.state, w.done_ms = "done", now
                f, s = w.filled(), w.intent.size
                w.message = "filled" if f >= s - 1e-12 else f"partial: {f:g} of {s:g}"
            return
        if self._send(w, places[0], snap, now):
            w.algo = pl.state

    def _advance_native(self, w: _Work, snap: MarketSnapshot, now: int) -> None:
        c = next(iter(w.children.values()), None)
        if c is None:
            self._start_native(w, snap, now)
        elif c.terminal:
            w.state, w.done_ms = "done", now
            f, s = w.filled(), w.intent.size
            w.message = "filled" if f >= s - 1e-12 else f"partial: {f:g} of {s:g}"
            if c.status == "gone":
                w.message += "; NATIVE_TWAP_OUTCOME_UNKNOWN: the start's answer was lost; check the venue"

    def _start_native(self, w: _Work, snap: MarketSnapshot, now: int) -> None:
        """One protocol TWAP for the whole intent. OMX checks it once at full size: exposure, leverage,
        loss, allowlist, liquidity, freshness, plus an estimated suborder cap; the suborders themselves are
        sent by the venue and may each slip up to NATIVE_TWAP_SLIPPAGE_BPS."""
        it = w.intent
        if snap.mid is None:
            self._reject(w, now, "blocked: NO_TWO_SIDED_BOOK")
            return
        size = P.round_sz(it.size, self.venue.size_decimals(it.coin))
        if size <= 0:
            self._reject(w, now, "blocked: SIZE_ROUNDS_TO_ZERO")
            return
        px = P.round_px(snap.mid, self.venue.tick_size(it.coin), "near")
        w.issued += 1
        child = ChildOrder(f"{it.intent_id}.tw", it.intent_id, it.coin, it.side, size, px, TWAP_TIF,
                           it.reduce_only, "", None)
        more, info = native_twap_checks(it, size * px, self.cfg)
        reasons = self._decide(w, child, snap, self._acct, now, cfg=native_twap_policy(self.cfg), more=more,
                               native_twap=info)
        if reasons:
            self._reject(w, now, "blocked: " + ",".join(reasons))
            return
        self.audit.append("child_sent", {"intent_id": it.intent_id, "child_id": child.child_id,
                                         "native_twap": True, "minutes": it.duration_s // 60, "now_ms": now})
        fn = self.venue.place_twap
        try:
            if _accepts_abort(fn):
                tid = fn(child, it.duration_s // 60, False, abort=self._abort)
            else:
                tid = fn(child, it.duration_s // 60, False)
        except VenueAborted as e:
            self.audit.append("child_aborted", {"intent_id": it.intent_id, "child_id": child.child_id,
                                                "error": repr(e), "now_ms": now})
            if not self._is_killed():
                self._reject(w, now, "aborted before send")
            return
        except Exception as e:
            if isinstance(e, VenueError) and not isinstance(e, VenueAmbiguous):     # refused: nothing runs
                w.errors += 1
                self.audit.append("child_error", {"intent_id": it.intent_id, "child_id": child.child_id,
                                                  "error": repr(e), "now_ms": now})
                self._reject(w, now, f"venue refused the native TWAP: {e}"[:300])
                return
            # it may be running and we have no id: keep its full size in exposure for its running time
            c = self._register(w, child, None, now, "unknown")
            c.end_ms = now + it.duration_s * 1000
            w.errors += 1
            w.state = "working"
            w.message = "NATIVE_TWAP_OUTCOME_UNKNOWN: it may be running; check the venue (no id to cancel)"
            self.audit.append("child_unknown", {"intent_id": it.intent_id, "child_id": child.child_id,
                                                "error": repr(e), "native_twap": True, "now_ms": now})
            return
        c = self._register(w, child, f"twap:{tid}", now, "running")
        c.end_ms, c.read_ms = now + it.duration_s * 1000, now
        self._store_twap(w, c)
        w.state = "working"
        self.audit.append("child_ack", {"intent_id": it.intent_id, "child_id": child.child_id, "oid": c.oid,
                                        "native_twap": True, "end_ms": c.end_ms})
        if self._is_killed():                                   # the answer came back after a kill
            w.state, w.done_ms, w.message = "killed", now, "killed"
            self._cancel_child(w, c, "KILL_AFTER_ACK")

    def _finalize(self) -> None:
        for w in self._work.values():
            if w.state in TERMINAL and not w.finalized and not w.open_children():
                w.finalized = True
                self.audit.append("intent_done", {"report": self._report(w).__dict__, "now_ms": self.clock()})

    # ------------------------------------------------------------ EngineAPI
    def _busy(self) -> bool:
        """Working poll rate: a child is open, or the engine is cancelling OMX orders it still sees."""
        if any(w.open_children() or w.state not in TERMINAL for w in self._work.values()):
            return True
        if (self._killed or self.cancel_only) and self._raw is not None:
            return any(o.oid and (self._kill_scope == "all" or is_omx_cloid(o.cloid)) for o in self._raw.open_orders)
        return False

    def _read(self, now: int) -> bool:
        """The venue reads due now (all of them without a PollSchedule). Returns True when the
        open-order listing is fresh."""
        working = self._busy()
        coins = len({w.intent.coin for w in self._work.values() if w.state not in TERMINAL})
        kids = sum(len(w.open_children()) for w in self._work.values())
        if self.poll is not None:
            unified = self._raw is not None and self._raw.account_mode in UNIFIED_MODES
            self._scale = self.poll.scale(working, coins, kids, unified)
            self._book_scale = self.poll.book_scale(coins)

        def due(kind: str) -> bool:
            if self.poll is None:
                return True
            t = self._polled.get(kind)
            if t is not None and now - t < self.poll.interval(kind, working, self._scale):
                return False
            self._polled[kind] = now
            return True

        if due("fills"):
            self._poll_fills()
        split = self.poll is not None and hasattr(self.venue, "account_core") and hasattr(self.venue, "open_orders")
        first = self._raw is None
        if not split:
            raw, fresh = self.venue.account(), True
        else:
            core = self.venue.account_core() if due("account") or first else self._raw
            fresh = due("orders") or first
            raw = replace(core, open_orders=self.venue.open_orders() if fresh else self._raw.open_orders)
        flows_due = due("flows")
        eq = raw.equity_usd
        rose = eq > self._flow_equity + 1e-9 or eq - self._flow_low > max(FLOW_RISE_USD, FLOW_RISE_FRAC * abs(eq))
        if not flows_due and self.poll is not None and self._flow_val is not None and rose \
                and now - self._polled.get("flows", 0) >= self.poll.flow_early_ms * self._scale:
            flows_due, self._polled["flows"] = True, now      # equity rose: read the ledger early
        self._acct = self._account(now, raw, fresh, flows_due)
        if due("marks") or not self._marks:
            self._marks = self._read_marks()
        return fresh

    def step(self) -> None:
        now = self.clock()
        self._is_killed()                                       # re-reads the kill file every call
        if now < self._backoff_until:
            return
        self._snaps = {}
        try:
            fresh = self._read(now)
            if not self._started:
                self._start(self._raw)
            elif fresh and not self._killed and now - (self._orphan_ms or 0) >= ORPHAN_SWEEP_MS:
                self._sweep_orphans(self._raw, "periodic")
        except Exception as e:
            self._venue_errs += 1
            self._backoff_until = now + _backoff(self._venue_errs)
            self.audit.append("venue_error", {"where": "poll", "error": repr(e), "now_ms": now})
            return
        self._venue_errs = 0
        self._poll_twaps(now)
        if self._killed:
            self._cancel_everything("KILL")                     # retries cancels that failed earlier
            if fresh:
                self._cancel_venue_orders(self._raw.open_orders, self._kill_scope, "KILL")
        elif self.cancel_only:
            self._cancel_everything(CANCEL_ONLY)
            if fresh:
                self._cancel_venue_orders(self._raw.open_orders, "omx", CANCEL_ONLY)
        else:
            for w in list(self._work.values()):
                try:
                    self._advance(w, now)
                except Exception as e:
                    w.next_try_ms = now + _backoff(w.errors + 1)
                    self.audit.append("venue_error", {"where": "advance", "intent_id": w.intent.intent_id,
                                                      "error": repr(e), "now_ms": now})
        self._finalize()

    def submit(self, intent: Intent) -> IntentReport:
        now = self.clock()
        self.audit.append("intent_received", {"intent": to_record(intent), "now_ms": now})
        if intent.intent_id in self._work:
            return IntentReport(intent.intent_id, "rejected", message="DUPLICATE_INTENT_ID")
        reason = CANCEL_ONLY if self.cancel_only else P.validate(intent)
        if reason is None and self._builder_block:
            reason = self._builder_block
        if reason is None:
            reason = self.account_mode_block()
        if reason is None and self._is_killed():
            reason = "KILL_SWITCH_ACTIVE"
        if reason is None and intent.algo is Algo.POV and not hasattr(self.venue, "trades_since"):
            reason = "POV_NEEDS_TRADE_FEED"
        if reason is None and intent.algo is Algo.NATIVE_TWAP:
            reason = native_twap_gate(self.cfg, self.venue)
        mid = None
        try:
            mid = self.venue.snapshot(intent.coin).mid
        except Exception as e:
            reason = reason or f"VENUE_ERROR:{e!r}"
        w = _Work(intent, "accepted", mid, now)
        self._work[intent.intent_id] = w
        if reason is not None:
            w.reasons.append(reason)
            self._reject(w, now, reason)
            self._finalize()
            return self._report(w)
        self.step()
        return self._report(w)

    # ------------------------------------------------------------ builder approval
    def builder_approval(self) -> dict:
        """{"required", "ok", "builder", "configured", "approved", "error"?}: the venue's maxBuilderFee for
        this account and the configured builder. No builder configured: required False, ok True.
        A venue that cannot answer, or a read error, gives ok False with "error" (fail closed)."""
        if self.builder is None:
            return {"required": False, "ok": True, "builder": None, "configured": 0, "approved": None}
        addr, fee = self.builder
        out = {"required": True, "ok": False, "builder": addr, "configured": fee, "approved": None}
        fn = getattr(self.venue, "max_builder_fee", None)
        if fn is None:
            out["error"] = "the venue cannot read maxBuilderFee"
            return out
        try:
            out["approved"] = int(fn(addr))
        except Exception as e:  # network / format errors: unknown, never approved
            out["error"] = f"{type(e).__name__}: {e}"[:300]
            return out
        out["ok"] = out["approved"] >= fee
        return out

    def set_builder_block(self, reason: Optional[str]) -> None:
        """reason (BUILDER_NOT_APPROVED: ...) refuses new intents in submit(); None lifts it."""
        reason = str(reason)[:500] if reason else None
        if reason != self._builder_block:
            self.audit.append("builder_block", {"blocked": reason is not None, "reason": reason,
                                                "now_ms": self.clock()})
        self._builder_block = reason

    @property
    def builder_block(self) -> Optional[str]:
        return self._builder_block

    def account_mode_block(self) -> Optional[str]:
        """ACCOUNT_MODE_UNSUPPORTED:<mode> when the venue's account mode is not standard / unified
        (the adapter caches the read; an unreadable mode is "unknown", also refused). None for a venue
        without account modes."""
        fn = getattr(self.venue, "account_mode", None)
        if fn is None:
            return None
        try:
            mode = fn()
        except Exception as e:  # the HL adapter never raises here; any other venue fails closed
            mode = f"unknown ({type(e).__name__})"
        return mode_block_reason(mode)

    @staticmethod
    def _unknown_report(intent_id: str) -> IntentReport:
        """R3-8: an id this engine never saw (e.g. submitted before a daemon restart) is "unknown",
        never "rejected": it may have filled. The audit log and the venue have its outcome."""
        return IntentReport(intent_id, "unknown", message=UNKNOWN_INTENT_MSG)

    def status(self, intent_id: str) -> IntentReport:
        w = self._work.get(intent_id)
        return self._report(w) if w else self._unknown_report(intent_id)

    def cancel(self, intent_id: str) -> IntentReport:
        w = self._work.get(intent_id)
        if w is None:
            return self._unknown_report(intent_id)
        if w.state not in TERMINAL:
            w.state, w.done_ms, w.message = "cancelled", self.clock(), "cancelled by user"
            self.audit.append("cancel", {"intent_id": intent_id, "scope": "intent", "now_ms": self.clock()})
            self._cancel_all_of(w, "USER_CANCEL")
            self._finalize()
        return self._report(w)

    def _view_account(self, now: int) -> AccountState:
        """The last read under a PollSchedule (no extra venue weight), else a fresh read."""
        if self.poll is not None and self._acct is not None:
            return self._acct
        acct = self._account(now)
        self._marks = self._read_marks()
        return acct

    def preview(self, intent: Intent) -> dict:
        if self.cancel_only:
            raise ValueError(f"{CANCEL_ONLY}: this engine only cancels ({self._cancel_only_reason})")
        now = self.clock()
        snap = self.venue.snapshot(intent.coin)
        self._snaps = {intent.coin: snap}
        acct = self._view_account(now)
        fees = getattr(self.venue, "fee_bps", lambda: DEFAULT_FEES)()
        reason = P.validate(intent)
        rows, extra = [], ()
        exch = 0.0
        ntl = 0.0
        killed = self._is_killed()
        native = intent.algo is Algo.NATIVE_TWAP
        builder_ntl = 0.0
        if reason is None:
            ctx = P.Ctx(now, now, snap.mid or 0.0, snap.mid, snap.best_bid, snap.best_ask,
                        self.venue.tick_size(intent.coin), self.venue.size_decimals(intent.coin))
            for i, s in enumerate(P.schedule(intent, ctx), 1):
                if s["px"] is None:
                    continue
                child = ChildOrder(f"{intent.intent_id}.preview{i}", intent.intent_id, intent.coin, intent.side,
                                   s["size"], s["px"], s["tif"], intent.reduce_only)
                d = evaluate(child, intent, snap, replace(acct, open_orders=acct.open_orders + extra),
                             native_twap_policy(self.cfg) if native else self.cfg, now, killed, self._refs())
                extra += (OpenOrder(child.child_id, child.coin, child.side, child.size, child.px, child.reduce_only),)
                n = s["size"] * s["px"]
                ntl += n
                exch += n * (fees["taker"] if s["tif"] in ("Ioc", TWAP_TIF) else fees["maker"]) / 1e4
                row = {**s, "allow": d.allow, "reasons": list(d.reasons), "checks": _clean(d.checks)}
                if native:
                    more, info = native_twap_checks(intent, n, self.cfg)
                    gate = native_twap_gate(self.cfg, self.venue)
                    row["reasons"] += more + ([gate] if gate else [])
                    row["allow"] = not row["reasons"]
                    row["native_twap"] = info
                else:
                    builder_ntl += n
                rows.append(row)
        out = {"intent": to_record(intent), "valid": reason is None, "reason": reason, "arrival_mid": snap.mid,
               "data_age_ms": now - snap.ts_ms, "children": rows, "notional_usd": ntl,
               "est_fee_usd": {"exchange": exch, "builder": builder_ntl * self.builder_fee_tenths_bps / 1e5},
               "fee_assumption": "Ioc and native TWAP suborders at taker rate, Alo/Gtc at maker rate; "
                                 "a native TWAP carries no builder fee", "killed": killed, "sent": 0,
               "builder": list(self.builder) if self.builder else None, "builder_block": self._builder_block}
        self.audit.append("preview", _clean(out))
        return out

    # ------------------------------------------------------------ views
    def snapshot(self, coin: str) -> MarketSnapshot:
        """Current venue book for one coin (no side effects)."""
        return self.venue.snapshot(coin)

    def account_view(self) -> dict:
        """Equity, positions (with marks), open orders (in-flight children included) and day P&L
        computed from the engine's day start and external flows."""
        a = self._view_account(self.clock())
        return {"ts_ms": a.ts_ms, "equity_usd": a.equity_usd, "day_start_equity_usd": a.day_start_equity_usd,
                "day_start_ts_ms": self._day_ts, "day_net_flows_usd": a.day_net_flows_usd,
                "day_pnl_usd": a.day_pnl_usd(), "killed": self.killed, "cancel_only": self.cancel_only,
                "builder_block": self._builder_block, "account_mode": a.account_mode,
                "account_mode_block": mode_block_reason(a.account_mode) if a.account_mode is not None else None,
                "equity_basis": a.equity_basis, "equity_flags": list(a.equity_flags),
                "positions": [{**to_record(p), "mark_px": self._marks.get(p.coin)} for p in a.positions],
                "open_orders": [to_record(o) for o in a.open_orders],
                "non_perp_open_orders": [to_record(o) for o in (self._raw.open_orders if self._raw else ())
                                         if not self._perp(o.coin)]}

    # ------------------------------------------------------------ reports
    def _report(self, w: _Work) -> IntentReport:
        fills = w.fills
        msg = w.message
        if w.errors:
            msg = (msg + "; " if msg else "") + f"venue errors: {w.errors}"
        if w.rejects:
            msg = (msg + "; " if msg else "") + f"venue rejects: {len(w.rejects)} ({w.rejects[-1]})"
        return IntentReport(
            intent_id=w.intent.intent_id, state=w.state, filled_size=w.filled(), avg_px=tca.avg_px(fills),
            arrival_mid=w.arrival_mid, shortfall_bps=tca.shortfall_bps(w.intent.side, fills, w.arrival_mid),
            exch_fee_usd=sum(f.fee_usd for f in fills), builder_fee_usd=sum(f.builder_fee_usd for f in fills),
            children_sent=w.sent, children_blocked=w.blocked, block_reasons=list(w.reasons), message=msg)

    def tca(self, intent_id: str, horizons_ms=tca.DEFAULT_HORIZONS_MS) -> dict:
        w = self._work[intent_id]
        end = w.done_ms if w.done_ms is not None else self.clock()
        between = getattr(self.venue, "trades_between", None)
        trades = between(w.intent.coin, w.start_ms, end) if between else None
        return tca.build_report(w.intent, w.fills, w.arrival_mid, w.start_ms, end, trades,
                                getattr(self.venue, "mid_at", None), horizons_ms, w.sent, w.blocked)

    def receipt(self, intent_id: str, network: Optional[str] = None) -> dict:
        """Execution receipt of a known intent (omx.receipt). A final receipt is anchored once per
        receipt hash in the audit log (`receipt` record); polling it again writes nothing new."""
        w = self._work[intent_id]
        children = [{"child_id": cid, "cloid": c.order.cloid, "oid": c.oid, "tif": c.order.tif,
                     "px": c.order.px, "size": c.order.size, "reduce_only": c.order.reduce_only,
                     "sent_ms": c.sent_ms, "status": c.status, "filled": c.done_sz(),
                     "builder": list(c.order.builder) if c.order.builder else None,
                     "route": "hyperliquid_native_twap" if c.order.tif == TWAP_TIF else "omx"}
                    for cid, c in w.children.items()]
        fills = [{"fill_id": f.fill_id, "child_id": f.child_id, "px": f.px, "size": f.size, "fee_usd": f.fee_usd,
                  "builder_fee_usd": f.builder_fee_usd, "crossed": f.crossed, "ts_ms": f.ts_ms}
                 for f in sorted(w.fills, key=lambda f: (f.ts_ms, f.fill_id))]
        try:
            tca_report = self.tca(intent_id)
        except Exception as e:  # a venue without trade history still gets a receipt
            tca_report = {"error": f"TCA_UNAVAILABLE:{type(e).__name__}"}
        rec = receipt_mod.build(to_record(w.intent), w.state, to_record(self._report(w)), tca_report,
                                children, fills, self.builder, network)
        if rec["final"]:
            key = (intent_id, rec["receipt_hash"])
            if key not in self._receipt_anchors:
                self._receipt_anchors[key] = self.audit.append("receipt", {"intent_id": intent_id,
                                                             "receipt_hash": rec["receipt_hash"]}, durable=True)
            rec["audit_anchor"] = self._receipt_anchors[key]
        return rec

    def intents(self) -> list[str]:
        return list(self._work)

    @property
    def killed(self) -> bool:
        return self._killed or self.ks.is_killed()


PURPOSES = ("trade", "kill")


def from_config(cfg, signer=None, transport=None, purpose: str = "trade"):
    """Build the live engine from an OMXConfig: Hyperliquid adapter + policy + audit + kill switch.

    purpose "trade": refuses to build without a valid, unexpired eligibility attestation in
    cfg.state_dir. purpose "kill": skips that gate (cancelling reduces risk and must work for an
    expired or blocked user) and returns a cancel-only engine: submit/preview are refused with
    CANCEL_ONLY; kill() and the step loop only cancel.
    The agent key is loaded here from the configured path or environment variable, inside the
    user's own process. Pass `signer`/`transport` to inject fakes in tests.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    if purpose == "trade":
        from . import eligibility
        require_valid = getattr(eligibility, "require_valid", None)
        if require_valid is None:     # fail closed if the eligibility gate is missing
            raise RuntimeError("ELIGIBILITY_GATE_MISSING: omx.eligibility.require_valid is not available")
        require_valid(cfg.state_dir, time.time())
    from .venue.hyperliquid import HyperliquidVenue, LocalKeySigner, MAINNET_API_URL, TESTNET_API_URL
    if signer is None:
        if cfg.signer_key_path:
            signer = LocalKeySigner.from_key_file(cfg.signer_key_path)
        elif cfg.signer_key_env:
            signer = LocalKeySigner.from_env(cfg.signer_key_env)
        else:
            raise ValueError("config names no agent key path or environment variable")
    url = MAINNET_API_URL if cfg.network == "mainnet" else TESTNET_API_URL
    builder = (cfg.builder_address, cfg.builder_fee_tenths_bps) if cfg.builder_address and cfg.builder_fee_tenths_bps else None
    venue = HyperliquidVenue(url, cfg.account_address, signer, builder=builder, transport=transport,
                             order_expiry_ms=cfg.order_expiry_ms)
    return Engine(venue, cfg.policy, AuditLog(cfg.audit_path),
                  builder_fee_tenths_bps=cfg.builder_fee_tenths_bps if builder else 0, state_dir=cfg.state_dir,
                  cancel_only=purpose == "kill", builder_address=cfg.builder_address if builder else None)
