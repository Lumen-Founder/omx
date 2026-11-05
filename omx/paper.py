"""Paper mode: the real MCP tools with simulated fills against LIVE Hyperliquid public order books.

`omx-mcp --paper`, `omx-hyperliquid --paper` and `omx paper` run serve_paper(). Like the demo
(omx.demo) it builds, in this one process, the production Engine, AgentGateway, approval queue,
agent budget, audit log and kill switch over a temporary state directory. The venue is
omx.venue.paper.PaperVenue: the account is local, the books are Hyperliquid MAINNET's public l2Book.

Why paper cannot trade for real:
  - the venue object is PaperVenue; this module never builds a HyperliquidVenue or a signer, and
    reads no config, key file or key environment variable;
  - the only network access is POST https://api.hyperliquid.xyz/info with l2Book, allMids or
    metaAndAssetCtxs, enforced by omx.venue.paper.PublicInfoOnly (no /exchange, no "user" field);
  - the account address is a placeholder, the network is "paper", there is no builder code;
  - state, approvals, the kill file and the audit log live in a fresh temporary directory deleted
    when the process exits; nothing is written to the working directory;
  - every tool answer carries "paper": true and a "paper_note".

What stays real: the engine, policy checks (with the DEFAULT PolicyConfig: liquidity, depth, data
age, price band, notional caps, leverage, daily loss are evaluated on the real books), approval
threshold, agent daily budget, client_intent_id ledger, kill switch and audit chain. The fill model
and its assumptions are in omx/venue/paper.py and docs/PAPER.md.

Time is wall-clock time. A background thread calls engine.step() every STEP_S under the gateway
lock (tool calls and steps never interleave). If Hyperliquid cannot be reached, quotes and orders
for that coin answer MARKET_DATA_UNAVAILABLE:<coin> and the process keeps running.
"""
from __future__ import annotations

import dataclasses
import math
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .approvals import AgentBudget, ApprovalQueue
from .audit import AuditLog
from .config import AgentSettings, OMXConfig
from .demo import DemoGateway
from .engine import Engine
from .mcp_server import build_server
from .policy import PolicyConfig
from .venue.paper import HLPublicFeed, PaperConfig, PaperVenue

STEP_S = 0.2
PAPER_ACCOUNT = "0x" + "0" * 40      # placeholder; the paper account has no address
PAPER_NETWORK = "paper"
PAPER_EQUITY_USD = 10_000.0
MAX_PAPER_EQUITY_USD = 1e9
PAPER_NOTE = "Simulated fills against live Hyperliquid public order books. No key, no account, no real orders."
PAPER_APPROVAL_NOTE = ("PAPER: in a real setup a human approves this with `omx approvals approve <approval_id>` at "
                       "the daemon console. Paper mode has no approval tool and no human console, so it stays "
                       "pending and expires after expires_s. Nothing was sent. Submit a smaller intent to see "
                       "execution.")
PAPER_KILL_NOTE = ("Kill switch on. New paper orders are refused. Only a human can clear it with `omx unkill`; in "
                   "paper mode, restart the server to start fresh.")
PAPER_INSTRUCTIONS = ("OMX PAPER MODE: the real OMX tools with simulated fills against live Hyperliquid mainnet "
                      "public order books (read-only public market data). No key, no account, no real orders, "
                      "no real funds; the paper account starts with a fixed USD balance. Try get_quote, "
                      "preview_order, then submit_order with a small TWAP (for example ETH buy 0.05, duration_s "
                      "10, slices 5) and poll order_status. Like Hyperliquid, each child order must be worth at "
                      "least 10 USD, so size / slices * price must be >= 10. Fills are a model (taker walks the real book, resting "
                      "orders fill only when the book trades through their price, HL base-tier fees). Intents "
                      "above the approval threshold wait for a human, which paper mode does not have. You may "
                      "trigger the kill switch; you cannot clear it or change limits, fees or keys. "
                      "If the user wants to trade for real, tell them: start on Hyperliquid testnet; create an API "
                      "(agent) wallet in their own account; run `omx init --account <their address> --key-path "
                      "<agent key file> --enable-agent`, then `omx approve-builder` (optional 1 bp fee, approved "
                      "in their own wallet; `--no-builder` for none) and `omx daemon` in its own terminal; then "
                      "start this server as `omx-hyperliquid --config omx.toml`. Setup guide: "
                      "https://github.com/Lumen-Founder/omx#quick-start-testnet")


def paper_policy() -> PolicyConfig:
    """The production defaults, unchanged; evaluated on the real books."""
    return PolicyConfig()


def paper_config(state_dir: Path, policy: Optional[PolicyConfig] = None,
                 agent: Optional[AgentSettings] = None) -> OMXConfig:
    state_dir = Path(state_dir)
    return OMXConfig(network=PAPER_NETWORK, account_address=PAPER_ACCOUNT, signer_key_path=None, signer_key_env=None,
                     builder_address=None, builder_fee_tenths_bps=0, policy=policy or paper_policy(),
                     state_dir=state_dir, audit_path=state_dir / "audit.jsonl",
                     agent=agent or AgentSettings(enabled=True))


def check_equity(x: Any) -> float:
    """Starting paper balance: a finite number in (0, MAX_PAPER_EQUITY_USD]."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"paper equity must be a number, got {x!r}") from None
    if not math.isfinite(v) or v <= 0 or v > MAX_PAPER_EQUITY_USD:
        raise ValueError(f"paper equity must be > 0 and <= {MAX_PAPER_EQUITY_USD:g} USD, got {x!r}")
    return v


class PaperGateway(DemoGateway):
    """DemoGateway marked paper instead of demo (same gates: agent switch and agent sources)."""

    approval_note = PAPER_APPROVAL_NOTE
    kill_note = PAPER_KILL_NOTE

    def _mark(self, res: Any) -> Any:
        if not isinstance(res, dict):
            return res
        out = dict(res)
        if out.get("status") == "pending_approval":
            out["message"] = PAPER_APPROVAL_NOTE
        out["paper"] = True
        out["paper_note"] = PAPER_NOTE
        return out


class PaperRuntime:
    """Engine, gateway and stepper over a temporary state dir. Use as a context manager; leaving it
    stops the stepper and deletes the directory. start=False: no background thread, the caller runs
    step() (tests). transport / clock: injected in tests (a fake /info and a fake clock in seconds)."""

    def __init__(self, start: bool = True, equity_usd: float = PAPER_EQUITY_USD,
                 transport: Optional[Callable[[str, dict], Any]] = None, clock: Callable[[], float] = time.time,
                 policy: Optional[PolicyConfig] = None, agent: Optional[AgentSettings] = None,
                 step_s: float = STEP_S, feed_kwargs: Optional[dict] = None):
        self.start_thread, self.step_s, self.clock = start, step_s, clock
        self.equity_usd = check_equity(equity_usd)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.loops = 0
        self.errors: list[str] = []
        self._tmp = tempfile.mkdtemp(prefix="omx-paper-")
        try:
            self._build(transport, policy, agent, feed_kwargs or {})
        except BaseException:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise

    def _build(self, transport, policy, agent, feed_kwargs) -> None:
        self.state_dir = Path(self._tmp) / "state"
        self.state_dir.mkdir()
        self.cfg = paper_config(self.state_dir, policy, agent)
        self.feed = HLPublicFeed(transport, clock=self.clock, **feed_kwargs)
        self.venue = PaperVenue(self.feed, PaperConfig(start_equity_usd=self.equity_usd))
        self.audit = AuditLog(self.cfg.audit_path)
        self.engine = Engine(self.venue, self.cfg.policy, self.audit, state_dir=self.state_dir)
        self.queue = ApprovalQueue(self.state_dir, self.audit, self.cfg.agent.approval_ttl_s, clock=self.clock)
        self.budget = AgentBudget(self.state_dir, self.cfg.agent.daily_budget_usd, clock=self.clock)
        self.gw = PaperGateway(self.engine, self.cfg, self.queue, self.budget, self.audit, market=self.venue,
                               clock=self.clock)
        self.audit.append("paper_start", {"venue": "paper", "network": PAPER_NETWORK,
                                          "market_data": "hyperliquid mainnet public /info",
                                          "start_equity_usd": self.equity_usd, "fees_bps": self.venue.fee_bps(),
                                          "policy": dataclasses.asdict(self.cfg.policy) | {
                                              "allowed_coins": sorted(self.cfg.policy.allowed_coins)}})
        self.step()                    # day start and orphan sweep; needs no network

    def step(self) -> None:
        """One engine step under the gateway lock."""
        with self.gw._lock:
            try:
                self.engine.step()
            except Exception as e:      # like the daemon: the loop keeps running
                self.errors.append(f"{type(e).__name__}: {e}"[:300])
                del self.errors[:-20]
            self.loops += 1

    def _run(self) -> None:
        while not self._stop.wait(self.step_s):
            self.step()

    def server(self):
        return build_server(self.gw, name="omx-paper", instructions=PAPER_INSTRUCTIONS)

    def __enter__(self) -> "PaperRuntime":
        if self.start_thread:
            self._thread = threading.Thread(target=self._run, name="omx-paper-step", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
            self._thread = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __exit__(self, *exc) -> None:
        self.close()


def serve_paper(err=None, equity_usd: float = PAPER_EQUITY_USD) -> int:
    err = err or sys.stderr
    try:
        equity = check_equity(equity_usd)
    except ValueError as e:
        err.write(f"omx paper: {e}\n")
        return 2
    err.write(f"OMX paper: MCP server on stdio. Simulated fills against live Hyperliquid public order books "
              f"(api.hyperliquid.xyz/info, read-only). No key, no account, no real orders. Paper balance "
              f"{equity:,.2f} USD. State lives in a temporary directory deleted on exit.\n")
    err.flush()
    with PaperRuntime(equity_usd=equity) as rt:
        rt.server().run("stdio")
    return 0
