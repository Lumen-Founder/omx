"""MCP server (stdio) that lets AI agents use OMX inside the user's limits.

Built on the official `mcp` SDK 2.x: mcp.server.mcpserver.MCPServer (the v1
FastMCP renamed). Agents get quotes, previews, submits, status, cancels and the
kill switch. They cannot unkill, approve, or change policy, builder fee or keys:
no tool for that exists. Every intent is tagged source "agent:<client name>";
the name comes from the MCP client's initialize info and is a label, not an
authentication.

Process model (fix contract R1, item 8): the MCP server holds no Engine, no key
and no audit writer. Each tool call is forwarded to `omx daemon` over the file
IPC (omx.ipc); the daemon runs AgentGateway in its single engine thread, so the
agent budget check+reserve and every engine call are serialised there. If the
daemon is not running, tools return DAEMON_NOT_RUNNING; kill_switch still writes
the shared KILL file.

Submit routing: an intent whose notional bound is above agent.approval_above_usd,
or that would push the day's agent notional past agent.daily_budget_usd, is
queued for a human (`omx approvals approve <id>`) and the tool returns the
approval id instead of executing. An agent may cancel only intents created under
its own client name.

Idempotency (R3-3): submit_order takes an optional client_intent_id (8..40 chars of
[A-Za-z0-9_-]); the intent id becomes "ag-<client_intent_id>" and is claimed in the
daemon's intent-id ledger (state_dir/intent_ids.jsonl, shared with human submits, kept
across restarts) before anything is sent or queued. A second submit with the same id
returns the existing intent's id and state (status "duplicate") and sends nothing.

Run: python -m omx.cli mcp --config omx.toml   (or python -m omx.mcp_server --config omx.toml)
Try without a key, account or daemon: omx-mcp --demo (simulated venue in this process, see omx.demo)
Paper trading on live public books, still without a key or account: omx-mcp --paper (see omx.paper)
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from mcp.server.mcpserver import Context, MCPServer

from . import ipc
from .approvals import AgentBudget, ApprovalError, ApprovalQueue, make_intent, notional_bound_usd
from .audit import AuditLog
from .config import OMXConfig, contains_key_like, eligibility_gate
from .engine_api import EngineAPI, kill_engine
from .killswitch import kill_from_outside
from .onboarding import builder_fee_usd, max_fee_rate_str
from .types import Side, to_record

CLIENT = re.compile(r"[^A-Za-z0-9._-]+")
CLIENT_INTENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
ORDER_KEYS = frozenset({"coin", "side", "size", "algo", "limit_px", "duration_s", "slices", "pov_rate",
                        "post_only", "reduce_only", "max_slippage_bps", "note"})


def market_of(engine: Any) -> Any:
    """Object with snapshot(coin): the engine itself (R1 Engine.snapshot) or its venue."""
    for cand in (engine, getattr(engine, "venue", None)):
        if cand is not None and hasattr(cand, "snapshot"):
            return cand
    return None


def kill_reason_problem(reason: str) -> Optional[str]:
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 200 or not reason.isprintable():
        return "BAD_REASON: printable text, 1..200 chars"
    if contains_key_like(reason):
        return "BAD_REASON: looks like a private key; never send keys to OMX"
    return None


def _plain(obj: Any) -> Any:
    return json.loads(ipc.dumps(obj))


def _serial(fn):
    """One gateway call at a time: budget check+reserve and engine calls never interleave."""
    @functools.wraps(fn)
    def run(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return run


def client_source(name: Optional[str]) -> str:
    clean = CLIENT.sub("-", (name or "").strip())[:40].strip("-")
    return "agent:" + (clean or "unknown")


def _report(rep) -> dict:
    return dataclasses.asdict(rep) if dataclasses.is_dataclass(rep) else dict(rep)


def _unknown(rep: dict) -> bool:
    return rep.get("state") in ("rejected", "unknown") and str(rep.get("message", "")).startswith("UNKNOWN_INTENT")


def _err(code: str, **extra) -> dict:
    return {"ok": False, "block_reasons": [code], **extra}


class AgentGateway:
    """Agent-facing operations, run inside the daemon next to the engine (or in-process in tests).
    Every tool method holds one lock, so concurrent callers are serialised."""

    def __init__(self, engine: EngineAPI, cfg: OMXConfig, queue: ApprovalQueue, budget: AgentBudget,
                 audit: Optional[AuditLog] = None, market: Any = None, clock: Callable[[], float] = time.time,
                 intent_ids: Any = None):
        """intent_ids: the daemon's persistent intent-id ledger (ipc.IdLedger); a set in-process."""
        self.engine, self.cfg, self.queue, self.budget, self.audit = engine, cfg, queue, budget, audit
        self.intent_ids = intent_ids if intent_ids is not None else set()
        self.market = market if market is not None else market_of(engine)
        self.clock = clock
        self.owners: dict[str, str] = {}          # intent_id -> creating agent source
        self._lock = threading.RLock()

    def register_owner(self, intent_id: str, source: str) -> None:
        if source.startswith("agent:"):
            self.owners[intent_id] = source

    def _log(self, kind: str, data: dict) -> None:
        if self.audit is not None:
            self.audit.append(kind, data)

    def _snap(self, coin: str):
        if coin not in self.cfg.agent_coins:
            raise ValueError(f"COIN_NOT_ALLOWED_FOR_AGENTS:{coin}")
        if self.market is None:
            raise ValueError("NO_MARKET_DATA")
        s = self.market.snapshot(coin)
        if s.mid is None:
            raise ValueError("NO_TWO_SIDED_BOOK")
        return s

    def _intent(self, source: str, args: dict, intent_id: Optional[str] = None):
        if not isinstance(args, dict) or set(args) - ORDER_KEYS:
            raise ValueError("BAD_ARGS: order fields are " + ", ".join(sorted(ORDER_KEYS)))
        return make_intent(source=source, allowed_coins=self.cfg.agent_coins, intent_id=intent_id,
                           max_slippage_cap_bps=self.cfg.policy.price_band_bps, **args)

    def _duplicate(self, iid: str) -> dict:
        """The existing intent behind a reused client_intent_id; nothing new is sent or queued."""
        out = {"ok": True, "status": "duplicate", "intent_id": iid, "block_reasons": [],
               "message": "DUPLICATE_CLIENT_INTENT_ID: this id was already submitted; no new order was sent"}
        try:
            rep = _report(self.engine.status(iid))
        except Exception as e:  # report what is known
            rep = {"state": "unknown", "message": f"ENGINE_ERROR:{type(e).__name__}"}
        if _unknown(rep):
            ap = next((r for r in self.queue.list(include_final=True) if r["intent"]["intent_id"] == iid), None)
            if ap is not None:
                return {**out, "approval_id": ap["approval_id"], "approval_state": ap["state"]}
            rep = {"intent_id": iid, "state": "unknown", "message": "submitted before a daemon restart or not "
                   "executed; its outcome is in the audit log and on the venue"}
        return {**out, "report": rep}

    def _gate(self) -> Optional[dict]:
        if not self.cfg.agent.enabled:
            return _err("AGENT_DISABLED")
        if not self.cfg.policy.allow_agent_sources:
            return _err("AGENT_SOURCE_DISABLED")
        prob = eligibility_gate(self.cfg)
        return _err(prob) if prob else None

    def _routing(self, notional: float) -> list[str]:
        r = []
        if notional > self.cfg.agent.approval_above_usd:
            r.append("ABOVE_APPROVAL_THRESHOLD")
        if self.budget.would_exceed(notional):
            r.append("AGENT_DAILY_BUDGET_EXCEEDED")
        return r

    # ------------------------------------------------------------ tools

    @_serial
    def get_quote(self, coin: str) -> dict:
        try:
            s = self._snap(coin)
        except ValueError as e:
            return _err(str(e))
        now_ms = int(self.clock() * 1000)
        band = self.cfg.policy.depth_band_bps
        return {"ok": True, "coin": coin, "best_bid": s.best_bid, "best_ask": s.best_ask, "mid": s.mid,
                "mark_px": s.mark_px, "ts_ms": s.ts_ms, "age_ms": now_ms - s.ts_ms,
                "day_ntl_usd": s.day_ntl_usd, "depth_band_bps": band,
                "ask_depth_usd": s.depth_usd(Side.BUY, band), "bid_depth_usd": s.depth_usd(Side.SELL, band),
                "block_reasons": []}

    @_serial
    def preview_order(self, source: str, args: dict) -> dict:
        try:
            intent = self._intent(source, args)
            s = self._snap(intent.coin)
        except ValueError as e:
            return _err(str(e))
        notional = notional_bound_usd(intent, s.mid)
        try:
            pv = self.engine.preview(intent)
        except Exception as e:  # engine refusal is data for the agent, not a crash
            return _err(f"ENGINE_ERROR:{type(e).__name__}", message=str(e)[:300])
        gate = self._gate()
        return {"ok": True, "intent": to_record(intent), "engine_preview": pv,
                "notional_bound_usd": round(notional, 2),
                "builder_fee_rate": max_fee_rate_str(self.cfg.builder_fee_tenths_bps),
                "builder_fee_bound_usd": round(builder_fee_usd(notional, self.cfg.builder_fee_tenths_bps), 4),
                "would_need_human_approval": bool(self._routing(notional)),
                "routing_reasons": self._routing(notional),
                "block_reasons": gate["block_reasons"] if gate else []}

    @_serial
    def submit_order(self, source: str, args: dict) -> dict:
        args = dict(args) if isinstance(args, dict) else args
        cid = args.pop("client_intent_id", None) if isinstance(args, dict) else None
        if cid is not None and not (isinstance(cid, str) and CLIENT_INTENT_ID.match(cid)):
            return _err("BAD_CLIENT_INTENT_ID: 8..40 chars of A-Z a-z 0-9 _ -")
        iid = "ag-" + cid if cid is not None else None
        if iid is not None and iid in self.intent_ids:
            return self._duplicate(iid)
        gate = self._gate()
        if gate:
            return gate
        try:
            intent = self._intent(source, args, iid)
            s = self._snap(intent.coin)
        except ValueError as e:
            return _err(str(e))
        if iid is not None:
            self.intent_ids.add(iid)              # claimed before anything is sent or queued
        notional = notional_bound_usd(intent, s.mid)
        reasons = self._routing(notional)
        if not reasons and not self.budget.reserve(notional):     # atomic check+reserve
            reasons = ["AGENT_DAILY_BUDGET_EXCEEDED"]
        if reasons:
            rec = self.queue.enqueue(intent, reasons, notional)
            return {"ok": True, "status": "pending_approval", "approval_id": rec["approval_id"],
                    "expires_s": rec["expires_s"], "intent_id": intent.intent_id,
                    "notional_bound_usd": rec["notional_est_usd"], "routing_reasons": reasons,
                    "block_reasons": [],
                    "message": "A human must approve this with `omx approvals approve <approval_id>`."}
        self._log("agent_submit", {"source": source, "intent": to_record(intent),
                                   "notional_bound_usd": round(notional, 2)})
        try:
            rep = _report(self.engine.submit(intent))
        except Exception as e:
            self.budget.refund(notional)
            return _err(f"ENGINE_ERROR:{type(e).__name__}", message=str(e)[:300])
        if rep.get("state") == "rejected":
            self.budget.refund(notional)
        else:
            self.register_owner(intent.intent_id, source)
        return {"ok": rep.get("state") != "rejected", "status": "submitted", "intent_id": intent.intent_id,
                "report": rep, "block_reasons": list(rep.get("block_reasons") or [])}

    @_serial
    def approval_status(self, approval_id: str) -> dict:
        if not re.match(r"^ap-[0-9a-f]{12}$", approval_id or ""):
            return _err("BAD_APPROVAL_ID")
        try:
            r = self.queue.get(approval_id)
        except ApprovalError:
            return _err("UNKNOWN_APPROVAL_ID")
        return {"ok": True, "approval_id": approval_id, "state": r["state"], "expires_s": r["expires_s"],
                "intent_id": r["intent"]["intent_id"], "result": r["result"], "block_reasons": []}

    @_serial
    def order_status(self, intent_id: str) -> dict:
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", intent_id or ""):
            return _err("BAD_INTENT_ID")
        try:
            rep = _report(self.engine.status(intent_id))
        except KeyError:
            return _err("UNKNOWN_INTENT_ID")
        except Exception as e:
            return _err(f"ENGINE_ERROR:{type(e).__name__}", message=str(e)[:300])
        if _unknown(rep):
            return _err("UNKNOWN_INTENT_ID")
        return {"ok": True, "report": rep, "block_reasons": []}

    @_serial
    def cancel_order(self, source: str, intent_id: str) -> dict:
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", intent_id or ""):
            return _err("BAD_INTENT_ID")
        if self.owners.get(intent_id) != source:
            return _err("AGENTS_MAY_CANCEL_ONLY_OWN_INTENTS")
        self._log("agent_cancel", {"source": source, "intent_id": intent_id})
        try:
            rep = _report(self.engine.cancel(intent_id))
        except KeyError:
            return _err("UNKNOWN_INTENT_ID")
        except Exception as e:
            return _err(f"ENGINE_ERROR:{type(e).__name__}", message=str(e)[:300])
        if _unknown(rep):
            return _err("UNKNOWN_INTENT_ID")
        return {"ok": True, "report": rep, "block_reasons": list(rep.get("block_reasons") or [])}

    @_serial
    def kill_switch(self, source: str, reason: str) -> dict:
        bad = kill_reason_problem(reason)
        if bad:
            return _err(bad)
        reason = reason.strip()
        self._log("agent_kill", {"source": source, "reason": reason})
        res = kill_engine(self.engine, f"{source}: {reason}", "omx")
        return {"ok": True, "result": res, "block_reasons": [],
                "message": "Kill switch on. Only a human can clear it, with `omx unkill`."}

    @_serial
    def account_summary(self) -> dict:
        budget = {"agent_budget_used_usd": round(self.budget.used(), 2),
                  "agent_budget_remaining_usd": round(self.budget.remaining(), 2), "block_reasons": []}
        view_fn = getattr(self.engine, "account_view", None)
        try:
            if view_fn is not None:                   # R1: engine day start, external flows excluded
                return {**_plain(view_fn()), "ok": True, **budget}
            acct = getattr(self.engine, "account", None) or getattr(self.market, "account", None)
            if acct is None:
                return _err("NO_ACCOUNT_DATA")
            a = acct()
        except Exception as e:
            return _err(f"ENGINE_ERROR:{type(e).__name__}", message=str(e)[:300])
        return {"ok": True, "ts_ms": a.ts_ms, "equity_usd": a.equity_usd, "day_pnl_usd": a.day_pnl_usd(),
                "positions": [to_record(p) for p in a.positions],
                "open_orders": [to_record(o) for o in a.open_orders], **budget}

    @_serial
    def policy_limits(self) -> dict:
        p, a = self.cfg.policy, self.cfg.agent
        pol = {f.name: getattr(p, f.name) for f in dataclasses.fields(p)}
        pol["allowed_coins"] = sorted(p.allowed_coins)
        return {"ok": True, "read_only": True, "network": self.cfg.network, "policy": pol,
                "agent": {"enabled": a.enabled, "approval_above_usd": a.approval_above_usd,
                          "daily_budget_usd": a.daily_budget_usd, "allowed_coins": sorted(self.cfg.agent_coins),
                          "approval_ttl_s": a.approval_ttl_s},
                "builder_fee_tenths_bps": self.cfg.builder_fee_tenths_bps,
                "builder_fee_rate": max_fee_rate_str(self.cfg.builder_fee_tenths_bps),
                "block_reasons": []}


# ---------------------------------------------------------------- MCP wiring

SERVER_INSTRUCTIONS = ("OMX executes Hyperliquid orders inside limits the human set. Preview before submitting. "
                       "Large or over-budget intents return an approval_id and wait for a human. You may trigger "
                       "the kill switch; you cannot clear it or change limits, fees or keys.")


def build_server(gw: AgentGateway, name: str = "omx", instructions: Optional[str] = None):
    srv = MCPServer(name, instructions=instructions or SERVER_INSTRUCTIONS)

    def src(ctx: Context) -> str:
        try:
            cp = ctx.session.client_params
            return client_source(cp.client_info.name if cp else None)
        except Exception:
            return client_source(None)

    def order_args(coin, side, size, algo, limit_px, duration_s, slices, pov_rate, post_only, reduce_only,
                   max_slippage_bps, note) -> dict:
        return dict(coin=coin, side=side, size=size, algo=algo, limit_px=limit_px, duration_s=duration_s,
                    slices=slices, pov_rate=pov_rate, post_only=post_only, reduce_only=reduce_only,
                    max_slippage_bps=max_slippage_bps, note=note)

    tool = srv.tool(structured_output=True)

    @tool
    def get_quote(coin: str) -> dict[str, Any]:
        """Top of book, mid, data age and depth within the policy band for an agent-allowed coin."""
        return gw.get_quote(coin)

    @tool
    def preview_order(ctx: Context, coin: str, side: str, size: float, algo: str = "twap",
                      limit_px: Optional[float] = None, duration_s: int = 0, slices: int = 1,
                      pov_rate: float = 0.0, post_only: bool = False, reduce_only: bool = False,
                      max_slippage_bps: float = 50.0, note: str = "") -> dict[str, Any]:
        """Dry run: planned child orders, policy decisions, fee bound, and whether a human must approve.
        side: buy|sell. algo: market|limit|twap|pov|chase. size in base units. Sends nothing."""
        return gw.preview_order(src(ctx), order_args(coin, side, size, algo, limit_px, duration_s, slices,
                                                     pov_rate, post_only, reduce_only, max_slippage_bps, note))

    @tool
    def submit_order(ctx: Context, coin: str, side: str, size: float, algo: str = "twap",
                     limit_px: Optional[float] = None, duration_s: int = 0, slices: int = 1,
                     pov_rate: float = 0.0, post_only: bool = False, reduce_only: bool = False,
                     max_slippage_bps: float = 50.0, note: str = "",
                     client_intent_id: Optional[str] = None) -> dict[str, Any]:
        """Submit an intent. Above the approval threshold or the daily agent budget it is queued for a
        human and an approval_id is returned instead; check it with approval_status.
        ALWAYS pass a fresh client_intent_id (8..40 chars, A-Z a-z 0-9 _ -) and REUSE it when you retry
        after a timeout or error: a repeated id returns the existing intent (status "duplicate",
        intent_id "ag-<client_intent_id>") and never sends a second order."""
        args = order_args(coin, side, size, algo, limit_px, duration_s, slices, pov_rate, post_only, reduce_only,
                          max_slippage_bps, note)
        if client_intent_id is not None:
            args["client_intent_id"] = client_intent_id
        return gw.submit_order(src(ctx), args)

    @tool
    def approval_status(approval_id: str) -> dict[str, Any]:
        """State of a queued approval: pending, approved, rejected, expired, executed or failed."""
        return gw.approval_status(approval_id)

    @tool
    def order_status(intent_id: str) -> dict[str, Any]:
        """Fill progress, average price, cost vs arrival mid, fees and block reasons of an intent."""
        return gw.order_status(intent_id)

    @tool
    def cancel_order(ctx: Context, intent_id: str) -> dict[str, Any]:
        """Cancel an intent this agent (same client name) created, and its resting children."""
        return gw.cancel_order(src(ctx), intent_id)

    @tool
    def kill_switch(ctx: Context, reason: str) -> dict[str, Any]:
        """Stop all new orders and cancel resting ones. Agents can turn it on, never off."""
        return gw.kill_switch(src(ctx), reason)

    @tool
    def account_summary() -> dict[str, Any]:
        """Equity, day P&L (flows excluded), positions, open orders and remaining agent budget."""
        return gw.account_summary()

    @tool
    def policy_limits() -> dict[str, Any]:
        """Read-only view of the policy limits, agent thresholds and builder fee."""
        return gw.policy_limits()

    return srv


class DaemonGateway:
    """Same tool surface as AgentGateway, forwarded to `omx daemon`. Holds no engine, key or audit writer."""

    def __init__(self, state_dir, timeout_s: float = 15.0, default_source: str = "agent:unknown"):
        self.state_dir, self.timeout_s, self.default_source = state_dir, timeout_s, default_source

    def _call(self, op: str, source: Optional[str] = None, **args) -> dict:
        try:
            return ipc.call(self.state_dir, "agent." + op, args, self.timeout_s, client=source or self.default_source)
        except ipc.DaemonDown as e:
            return _err("DAEMON_NOT_RUNNING", message=str(e))
        except ipc.IPCTimeout as e:
            return _err("DAEMON_TIMEOUT", message=str(e), executed=e.executed)
        except ipc.IPCError as e:
            return _err(str(e).split(":")[0][:60] or "IPC_ERROR", message=str(e)[:300])

    def get_quote(self, coin: str) -> dict:
        return self._call("get_quote", coin=coin)

    def preview_order(self, source: str, args: dict) -> dict:
        return self._call("preview_order", source, args=args)

    def submit_order(self, source: str, args: dict) -> dict:
        return self._call("submit_order", source, args=args)

    def approval_status(self, approval_id: str) -> dict:
        return self._call("approval_status", approval_id=approval_id)

    def order_status(self, intent_id: str) -> dict:
        return self._call("order_status", intent_id=intent_id)

    def cancel_order(self, source: str, intent_id: str) -> dict:
        return self._call("cancel_order", source, intent_id=intent_id)

    def kill_switch(self, source: str, reason: str) -> dict:
        bad = kill_reason_problem(reason)
        if bad:
            return _err(bad)
        kill_from_outside(self.state_dir, f"{source}: {reason.strip()}")   # works even with the daemon down
        r = self._call("kill_switch", source, reason=reason)
        if "DAEMON_NOT_RUNNING" in r.get("block_reasons", []):
            return {"ok": True, "kill_file": True, "daemon_running": False, "block_reasons": [],
                    "message": "Kill file set; no new orders can be sent. The daemon is not running, so resting "
                               "orders are cancelled when a human runs `omx kill` or starts `omx daemon`."}
        return r

    def account_summary(self) -> dict:
        return self._call("account_summary")

    def policy_limits(self) -> dict:
        return self._call("policy_limits")


INSPECT_MESSAGE = ("This is OMX in inspect-only mode (no config, no daemon, no key): it lists its tools and refuses "
                   "every call. To trade, run `omx init`, `omx daemon`, then `omx-mcp --config omx.toml`.")


class InspectGateway(DaemonGateway):
    """The tool surface with no config, state dir, daemon or key: every call is refused and nothing is written
    (directories such as Glama start a server only to list its tools)."""

    def __init__(self):
        super().__init__(state_dir=None)

    def _call(self, op: str, source: Optional[str] = None, **args) -> dict:
        return _err("NOT_CONFIGURED", message=INSPECT_MESSAGE)

    def kill_switch(self, source: str, reason: str) -> dict:
        return _err("NOT_CONFIGURED", message=INSPECT_MESSAGE)


def serve(cfg: OMXConfig) -> None:
    build_server(DaemonGateway(cfg.state_dir)).run("stdio")


NO_CONFIG_DEMO_NOTE = ("omx-hyperliquid: no --config, no $OMX_CONFIG and no ./omx.toml, so this is the DEMO "
                       "(simulated venue, nothing real is traded). To trade: `omx init`, `omx daemon`, then "
                       "`omx-hyperliquid --config omx.toml`.\n")


def _no_config_given(config_arg: Optional[str]) -> bool:
    return config_arg is None and not os.environ.get("OMX_CONFIG") and not Path("omx.toml").exists()


def hyperliquid_main(argv: Optional[list[str]] = None) -> int:
    """`omx-hyperliquid`, the command MCP registries and install links run. With no config anywhere it
    serves the demo instead of exiting, so a client that installs it with no arguments gets working
    tools. A config that is named but missing is still an error (main)."""
    return main(argv, prog="omx-hyperliquid", no_config_demo=True)


def main(argv: Optional[list[str]] = None, prog: str = "omx-mcp", no_config_demo: bool = False) -> int:
    from .cli import load_config_or_exit
    ap = argparse.ArgumentParser(prog=prog, description="OMX MCP server (stdio), a client of `omx daemon`")
    ap.add_argument("--config", default=None)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true",
                      help="list the tools without a config or daemon; every call is refused (for MCP directories)")
    mode.add_argument("--demo", action="store_true",
                      help="try the tools on a simulated venue in this process: no config, key, daemon or network")
    mode.add_argument("--paper", action="store_true",
                      help="paper trading: simulated fills against live Hyperliquid public order books (read-only "
                           "public data); no config, key, account or daemon; nothing real is traded")
    ap.add_argument("--paper-equity", type=float, default=None, metavar="USD",
                    help="starting paper balance in USD (default 10000); only with --paper")
    ns = ap.parse_args(argv)
    if ns.paper_equity is not None and not ns.paper:
        ap.error("--paper-equity needs --paper")
    if ns.paper:
        from .paper import PAPER_EQUITY_USD, serve_paper
        return serve_paper(equity_usd=PAPER_EQUITY_USD if ns.paper_equity is None else ns.paper_equity)
    if ns.demo:
        from .demo import serve_demo
        return serve_demo()
    if ns.inspect:
        build_server(InspectGateway()).run("stdio")
        return 0
    if no_config_demo and _no_config_given(ns.config):
        from .demo import serve_demo
        sys.stderr.write(NO_CONFIG_DEMO_NOTE)
        return serve_demo()
    cfg = load_config_or_exit(ns.config, sys.stderr)
    if cfg is None:
        return 2
    prob = eligibility_gate(cfg)
    if prob or not cfg.agent.enabled:
        sys.stderr.write(f"refusing to start: {prob or 'AGENT_DISABLED (set [agent] enabled = true)'}\n")
        return 2
    if not ipc.daemon_alive(cfg.state_dir):
        sys.stderr.write("warning: `omx daemon` is not running; tools answer DAEMON_NOT_RUNNING until it is\n")
    serve(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
