"""Demo mode: the real MCP tools on a simulated venue, with no key, account, daemon or network.

`omx-mcp --demo`, `omx-hyperliquid --demo` and `omx demo` all run serve_demo(). It builds, inside
this one process, the same pieces `omx daemon` runs (Engine, AgentGateway, approval queue, agent
budget, audit log, kill switch) on top of the local simulator omx.venue.sim.SimVenue instead of the
Hyperliquid adapter, and serves the normal tool surface on stdio.

Why a demo cannot trade for real:
  - the venue object is SimVenue; this module never imports the Hyperliquid adapter, builds no
    signer and reads no config, key file or environment variable;
  - the account address is a placeholder, the network is "demo", there is no builder code;
  - state, approvals, the kill file and the audit log live in a fresh temporary directory that is
    deleted when the process exits; nothing is written to the working directory;
  - every tool answer carries "demo": true and a "demo_note".

What stays real: the engine, policy checks, approval threshold, agent daily budget, client_intent_id
ledger, kill switch and audit chain are the production code, with the default policy and agent
limits (see demo_policy). There is no approval tool and no human console,
so an intent routed to a human stays pending and expires; the answer says so.

Time: a background thread advances the simulator by the wall time elapsed (at most MAX_ADVANCE_MS per
loop) and calls engine.step() every STEP_S, like the daemon loop. It takes the gateway lock, so tool
calls and steps never interleave (the engine is not thread-safe). Market dynamics are model
assumptions (see omx/venue/sim.py), not Hyperliquid data.
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .approvals import AgentBudget, ApprovalQueue
from .audit import AuditLog
from .config import AgentSettings, OMXConfig
from .engine import Engine
from .mcp_server import AgentGateway, build_server
from .policy import PolicyConfig
from .venue.sim import SimConfig, SimVenue

STEP_S = 0.2                       # same loop period as the daemon
MAX_ADVANCE_MS = 1_000             # after a stall the simulator does not jump ahead more than this
DEMO_ACCOUNT = "0x" + "0" * 40     # placeholder; the simulator has no accounts
DEMO_NETWORK = "demo"
DEMO_EQUITY_USD = 10_000.0
DEMO_NOTE = ("DEMO: simulated venue inside this process. No network, no key, no real orders, no real funds. "
             "Prices are a random-walk model, not Hyperliquid data.")
DEMO_APPROVAL_NOTE = ("DEMO: in a real setup a human approves this with `omx approvals approve <approval_id>` at "
                      "the daemon console. The demo has no approval tool and no human console, so it stays "
                      "pending and expires after expires_s. Nothing was sent. Submit a smaller intent to see "
                      "execution.")
DEMO_KILL_NOTE = ("Kill switch on. New orders are refused. Only a human can clear it with `omx unkill`; in the "
                  "demo, restart the server to start fresh.")
DEMO_INSTRUCTIONS = ("OMX DEMO MODE: the real OMX tools on a local simulated venue. No key, account or network; "
                     "no real order can be sent. Try get_quote, preview_order, then submit_order with a small "
                     "TWAP (for example ETH buy 0.1, duration_s 10, slices 5) and poll order_status. Intents "
                     "above the approval threshold wait for a human, which the demo does not have. You may "
                     "trigger the kill switch; you cannot clear it or change limits, fees or keys. "
                     "If the user wants to trade for real, tell them: start on Hyperliquid testnet; create an API "
                     "(agent) wallet in their own account; run `omx init --account <their address> --key-path "
                     "<agent key file> --enable-agent`, then `omx approve-builder` (optional 1 bp fee, approved "
                     "in their own wallet; `--no-builder` for none) and `omx daemon` in its own terminal; then "
                     "start this server as `omx-hyperliquid --config omx.toml`. Setup guide: "
                     "https://github.com/Lumen-Founder/omx#quick-start-testnet")


def demo_policy() -> PolicyConfig:
    """The production defaults, unchanged. The simulator's book clears them: day notional 1e9 USD
    (floor 5e7) and about 490k (ETH, SOL) / 650k (BTC) USD resting within 25 bps (floor 250k), measured
    with seeds 1, 7, 42. Notional caps, leverage, daily loss, data age and price band all apply."""
    return PolicyConfig()


def demo_config(state_dir: Path, policy: Optional[PolicyConfig] = None,
                agent: Optional[AgentSettings] = None) -> OMXConfig:
    state_dir = Path(state_dir)
    return OMXConfig(network=DEMO_NETWORK, account_address=DEMO_ACCOUNT, signer_key_path=None, signer_key_env=None,
                     builder_address=None, builder_fee_tenths_bps=0, policy=policy or demo_policy(),
                     state_dir=state_dir, audit_path=state_dir / "audit.jsonl",
                     agent=agent or AgentSettings(enabled=True))


def _mark(res: Any) -> Any:
    if not isinstance(res, dict):
        return res
    out = dict(res)
    if out.get("status") == "pending_approval":
        out["message"] = DEMO_APPROVAL_NOTE
    out["demo"] = True
    out["demo_note"] = DEMO_NOTE
    return out


class DemoGateway(AgentGateway):
    """AgentGateway with every answer marked demo. The eligibility attestation is not asked: it is about
    trading on Hyperliquid, and nothing here reaches Hyperliquid. The agent switch and
    policy.allow_agent_sources are still checked."""

    def _gate(self) -> Optional[dict]:
        if not self.cfg.agent.enabled:
            return {"ok": False, "block_reasons": ["AGENT_DISABLED"]}
        if not self.cfg.policy.allow_agent_sources:
            return {"ok": False, "block_reasons": ["AGENT_SOURCE_DISABLED"]}
        return None

    def get_quote(self, coin):
        return _mark(super().get_quote(coin))

    def preview_order(self, source, args):
        return _mark(super().preview_order(source, args))

    def submit_order(self, source, args):
        return _mark(super().submit_order(source, args))

    def approval_status(self, approval_id):
        res = _mark(super().approval_status(approval_id))
        if res.get("state") == "pending":
            res["message"] = DEMO_APPROVAL_NOTE
        return res

    def order_status(self, intent_id):
        return _mark(super().order_status(intent_id))

    def cancel_order(self, source, intent_id):
        return _mark(super().cancel_order(source, intent_id))

    def kill_switch(self, source, reason):
        res = _mark(super().kill_switch(source, reason))
        if res.get("ok"):
            res["message"] = DEMO_KILL_NOTE
        return res

    def account_summary(self):
        return _mark(super().account_summary())

    def policy_limits(self):
        return _mark(super().policy_limits())


class DemoRuntime:
    """The demo's engine, gateway and stepper over a temporary state dir. Use as a context manager;
    leaving it stops the stepper and deletes the directory. start=False: no background thread, the
    caller advances time with step(ms) (tests)."""

    def __init__(self, start: bool = True, seed: int = 7, policy: Optional[PolicyConfig] = None,
                 agent: Optional[AgentSettings] = None, step_s: float = STEP_S):
        self.start_thread, self.step_s = start, step_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.loops = 0
        self.errors: list[str] = []
        self._tmp = tempfile.mkdtemp(prefix="omx-demo-")
        try:
            self._build(seed, policy, agent)
        except BaseException:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise

    def _build(self, seed: int, policy: Optional[PolicyConfig], agent: Optional[AgentSettings]) -> None:
        self.state_dir = Path(self._tmp) / "state"
        self.state_dir.mkdir()
        self.cfg = demo_config(self.state_dir, policy, agent)
        now_ms = int(time.time() * 1000)
        self.sim = SimVenue(SimConfig(seed=seed, start_ms=now_ms, start_equity_usd=DEMO_EQUITY_USD))
        self.audit = AuditLog(self.cfg.audit_path)
        self.engine = Engine(self.sim, self.cfg.policy, self.audit, state_dir=self.state_dir)
        clock = lambda: self.sim.now_ms() / 1000.0          # noqa: E731 - one time base: the simulator's
        self.queue = ApprovalQueue(self.state_dir, self.audit, self.cfg.agent.approval_ttl_s, clock=clock)
        self.budget = AgentBudget(self.state_dir, self.cfg.agent.daily_budget_usd, clock=clock)
        self.gw = DemoGateway(self.engine, self.cfg, self.queue, self.budget, self.audit, market=self.sim,
                              clock=clock)
        self.audit.append("demo_start", {"venue": "sim", "network": DEMO_NETWORK, "sim_rng": seed,
                                         "start_equity_usd": DEMO_EQUITY_USD,
                                         "policy": dataclasses.asdict(self.cfg.policy) | {
                                             "allowed_coins": sorted(self.cfg.policy.allowed_coins)}})
        for _ in range(3):                  # first reads: day start, orphan sweep, fresh books
            self.step(int(self.step_s * 1000))

    def step(self, ms: int) -> None:
        """Advance the simulator by ms and run one engine step, under the gateway lock."""
        with self.gw._lock:
            if ms > 0:
                self.sim.advance(int(ms))
            try:
                self.engine.step()
            except Exception as e:      # like the daemon: the loop keeps running
                self.errors.append(f"{type(e).__name__}: {e}"[:300])
                del self.errors[:-20]
            self.loops += 1

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self.step_s):
            now = time.monotonic()
            self.step(min(MAX_ADVANCE_MS, max(0, int((now - last) * 1000))))
            last = now

    def server(self):
        return build_server(self.gw, name="omx-demo", instructions=DEMO_INSTRUCTIONS)

    def __enter__(self) -> "DemoRuntime":
        if self.start_thread:
            self._thread = threading.Thread(target=self._run, name="omx-demo-step", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __exit__(self, *exc) -> None:
        self.close()


def serve_demo(err=None) -> int:
    err = err or sys.stderr
    err.write("OMX demo: MCP server on stdio with a simulated venue. No key, no network, no real orders. "
              "State lives in a temporary directory deleted on exit.\n")
    err.flush()
    with DemoRuntime() as rt:
        rt.server().run("stdio")
    return 0
