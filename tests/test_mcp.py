import asyncio
import dataclasses
import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp import Client, Implementation

from omx.approvals import AgentBudget, ApprovalQueue
from omx.audit import AuditLog
from omx.config import loads, render
from omx.eligibility import Attestation
from omx.engine_api import IntentReport
from omx.mcp_server import AgentGateway, DaemonGateway, build_server, client_source
from omx.onboarding import save_eligibility
from omx.types import AccountState, BookLevel, MarketSnapshot

ACCOUNT = "0x" + "ab" * 20
NOW_S = 1_800_000_000.0


class FakeEngine:
    """EngineAPI stand-in plus snapshot()/account() for quotes and summaries."""

    def __init__(self, mid=100.0, now_s=NOW_S):
        self.mid, self.now_s = mid, now_s
        self.submitted, self.cancelled, self.kills, self.unkills = [], [], [], []
        self.killed = False

    def snapshot(self, coin):
        return MarketSnapshot(coin, int(self.now_s * 1000), (BookLevel(self.mid - 0.05, 1e4),),
                              (BookLevel(self.mid + 0.05, 1e4),), mark_px=self.mid, day_ntl_usd=1e9)

    def account(self):
        return AccountState(int(self.now_s * 1000), 10_000.0, day_start_equity_usd=10_000.0)

    def preview(self, intent):
        return {"children": [{"size": intent.size / intent.slices}] * intent.slices, "blocked": []}

    def submit(self, intent):
        self.submitted.append(intent)
        if self.killed:
            return IntentReport(intent.intent_id, "rejected", block_reasons=["KILL_SWITCH_ACTIVE"])
        return IntentReport(intent.intent_id, "accepted")

    def status(self, intent_id):
        if not any(i.intent_id == intent_id for i in self.submitted):
            return IntentReport(intent_id, "rejected", message="UNKNOWN_INTENT")
        return IntentReport(intent_id, "working")

    def cancel(self, intent_id):
        self.cancelled.append(intent_id)
        return IntentReport(intent_id, "cancelled")

    def kill(self, reason, scope="omx"):
        self.killed = True
        self.kills.append(reason)
        self.scopes = getattr(self, "scopes", []) + [scope]
        return {"killed": True, "reason": reason, "scope": scope}

    def builder_approval(self):
        """The daemon's maxBuilderFee check (configs with a builder): approved at the venue cap."""
        return {"required": True, "ok": True, "approved": 100}

    def unkill(self, confirm):
        self.unkills.append(confirm)
        if confirm != "I UNDERSTAND":
            return {"ok": False, "killed": self.killed}
        self.killed = False
        return {"ok": True, "killed": False}

    def step(self):
        pass


def confirming_console():
    """A daemon console where a human types every confirm line the daemon asks for (R2/B2)."""
    import re
    from omx.daemon import QueueConsole

    def answer(text):
        m = re.search(r"Type exactly `(confirm [0-9a-f]+)`", text)
        return m.group(1) if m else None
    return QueueConsole(answer)


@contextmanager
def daemon_running(cfg, factory, clock=time.time, step_s=0.02, console=None, **kw):
    """`omx daemon` in a background thread (the engine is built and used in that thread only).
    console=None: no interactive console, so human-privileged requests are refused."""
    from omx.daemon import Daemon
    if cfg.lock_dir is None:                  # per-account lock (R3-5) next to the test's state_dir
        cfg = dataclasses.replace(cfg, lock_dir=Path(cfg.state_dir).parent / "omx-locks")
    d = Daemon(cfg, factory, step_s=step_s, clock=clock, console=console, **kw)
    t = d.start_thread()
    try:
        yield d
    finally:
        d.stop()
        t.join(10)


def make_cfg(tmp_path, agent=True, **sub):
    text = render(ACCOUNT, "testnet", key_path="secrets/agent.key", agent_enabled=agent)
    for k, v in sub.items():
        text = text.replace(k, v)
    return loads(text, base_dir=tmp_path)


def make_eligible(state_dir):
    save_eligibility(state_dir, Attestation("KR", citizenships=("KR",), accepts_terms=True, current_country="KR"),
                     True, [])


class Clock:
    def __init__(self, t=NOW_S):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(tmp_path)
    make_eligible(cfg.state_dir)
    clock = Clock()
    audit = AuditLog(cfg.audit_path)
    eng = FakeEngine()
    q = ApprovalQueue(cfg.state_dir, audit, cfg.agent.approval_ttl_s, clock=clock)
    b = AgentBudget(cfg.state_dir, cfg.agent.daily_budget_usd, clock=clock)
    gw = AgentGateway(eng, cfg, q, b, audit, clock=clock)
    return dict(cfg=cfg, eng=eng, q=q, budget=b, gw=gw, audit=audit, clock=clock)


def call(gw, tool, args=None, client="claude-desktop"):
    async def go():
        async with Client(build_server(gw), client_info=Implementation(name=client, version="1")) as c:
            r = await c.call_tool(tool, args or {})
            return r
    r = asyncio.run(go())
    assert not r.is_error, r.content
    return r.structured_content


def list_tools(gw):
    async def go():
        async with Client(build_server(gw)) as c:
            return [t.name for t in (await c.list_tools()).tools]
    return asyncio.run(go())


def test_tool_surface_has_no_write_access_to_limits_or_unkill(env):
    names = set(list_tools(env["gw"]))
    assert names == {"get_quote", "preview_order", "submit_order", "approval_status", "order_status",
                     "cancel_order", "kill_switch", "account_summary", "policy_limits"}
    assert not any(w in n for n in names for w in ("unkill", "approve", "set", "policy_update", "fee_set", "key"))


def test_small_submit_executes_and_is_tagged(env):
    res = call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})
    assert res["status"] == "submitted" and res["ok"]
    sent = env["eng"].submitted[0]
    assert sent.source == "agent:claude-desktop"
    assert sent.intent_id.startswith("ag-")
    assert env["budget"].used() == pytest.approx(100.0 * 1.005)


def test_above_threshold_goes_to_approval_not_engine(env):
    res = call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 20.0})
    assert res["status"] == "pending_approval"
    assert res["routing_reasons"] == ["ABOVE_APPROVAL_THRESHOLD"]
    assert res["approval_id"].startswith("ap-")
    assert env["eng"].submitted == []
    st = call(env["gw"], "approval_status", {"approval_id": res["approval_id"]})
    assert st["state"] == "pending"
    assert env["q"].list()[0]["intent"]["source"] == "agent:claude-desktop"


def test_budget_exhaustion_routes_to_approval(env):
    for _ in range(5):   # 5 x ~904 USD = ~4.5k of the 5k budget, each under the 1k threshold
        assert call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 9.0})["status"] == "submitted"
    res = call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 9.0})
    assert res["status"] == "pending_approval"
    assert res["routing_reasons"] == ["AGENT_DAILY_BUDGET_EXCEEDED"]
    assert len(env["eng"].submitted) == 5


def test_input_validation_returns_block_reasons(env):
    gw = env["gw"]
    assert call(gw, "submit_order", {"coin": "SOL", "side": "buy", "size": 1})["block_reasons"] == \
        ["COIN_NOT_ALLOWED:SOL"]   # SOL is in policy but not in agent.allowed_coins
    assert call(gw, "submit_order", {"coin": "ETH", "side": "hold", "size": 1})["block_reasons"][0].startswith("BAD_SIDE")
    assert call(gw, "submit_order", {"coin": "ETH", "side": "buy", "size": -1})["block_reasons"][0].startswith("BAD_SIZE")
    assert call(gw, "submit_order", {"coin": "ETH", "side": "buy", "size": 1, "max_slippage_bps": 5000}
                )["block_reasons"][0].startswith("BAD_MAX_SLIPPAGE")
    assert call(gw, "submit_order", {"coin": "ETH", "side": "buy", "size": 1, "note": "0x" + "1f" * 32}
                )["block_reasons"][0].startswith("BAD_NOTE")
    assert env["eng"].submitted == []


def test_agent_can_kill_but_not_unkill(env):
    res = call(env["gw"], "kill_switch", {"reason": "price looks wrong"})
    assert res["ok"] and env["eng"].killed
    assert env["eng"].kills == ["agent:claude-desktop: price looks wrong"]
    assert env["eng"].unkills == []
    res = call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})
    assert res["ok"] is False and res["block_reasons"] == ["KILL_SWITCH_ACTIVE"]


def test_policy_limits_is_read_only_view(env):
    lim = call(env["gw"], "policy_limits")
    assert lim["read_only"] and lim["policy"]["max_leverage"] == 3.0
    lim["policy"]["max_leverage"] = 50
    assert env["cfg"].policy.max_leverage == 3.0
    assert call(env["gw"], "policy_limits")["policy"]["max_leverage"] == 3.0


def test_cancel_only_own_intents(env):
    own = "AGENTS_MAY_CANCEL_ONLY_OWN_INTENTS"
    assert call(env["gw"], "cancel_order", {"intent_id": "hu-123"})["block_reasons"] == [own]
    assert call(env["gw"], "cancel_order", {"intent_id": "ag-abc"})["block_reasons"] == [own]   # nobody's
    iid = call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})["report"]["intent_id"]
    assert call(env["gw"], "cancel_order", {"intent_id": iid}, client="other-agent")["block_reasons"] == [own]
    assert env["eng"].cancelled == []
    assert call(env["gw"], "cancel_order", {"intent_id": iid})["report"]["state"] == "cancelled"
    assert env["eng"].cancelled == [iid]


def test_kill_reason_with_key_refused(env):
    key = "0x" + "1f" * 32
    res = call(env["gw"], "kill_switch", {"reason": f"stop {key}"})
    assert not res["ok"] and res["block_reasons"][0].startswith("BAD_REASON") and not env["eng"].killed
    assert not env["cfg"].audit_path.exists() or key not in env["cfg"].audit_path.read_text()


def test_account_summary_uses_engine_account_view(env):
    class ViewEngine(FakeEngine):
        def account_view(self):
            return {"equity_usd": 123.0, "day_pnl_usd": -4.0, "positions": [], "open_orders": []}
    gw = AgentGateway(ViewEngine(), env["cfg"], env["q"], env["budget"], env["audit"], clock=env["clock"])
    s = call(gw, "account_summary")
    assert s["ok"] and s["equity_usd"] == 123.0 and s["day_pnl_usd"] == -4.0
    assert s["agent_budget_remaining_usd"] == 5000.0


def test_unknown_order_fields_refused(env):
    for extra in ({"intent_id": "hu-x"}, {"source": "human"}, {"max_slippage_cap_bps": 1e6}):
        res = env["gw"].submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 1.0, **extra})
        assert res["block_reasons"][0].startswith("BAD_ARGS"), extra
    assert env["eng"].submitted == []


def test_daemon_gateway_end_to_end(env):
    """The shipped wiring: MCP server -> DaemonGateway -> file IPC -> daemon -> AgentGateway -> engine."""
    from omx.killswitch import KillSwitch, is_killed
    gw = DaemonGateway(env["cfg"].state_dir)
    assert call(gw, "get_quote", {"coin": "ETH"})["block_reasons"] == ["DAEMON_NOT_RUNNING"]
    eng = FakeEngine()
    with daemon_running(env["cfg"], lambda c: eng):
        res = call(gw, "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0}, client="cursor")
        assert res["status"] == "submitted" and eng.submitted[0].source == "agent:cursor"
        iid = res["report"]["intent_id"]
        assert call(gw, "cancel_order", {"intent_id": iid}, client="other")["block_reasons"] == \
            ["AGENTS_MAY_CANCEL_ONLY_OWN_INTENTS"]
        assert call(gw, "cancel_order", {"intent_id": iid}, client="cursor")["ok"]
        assert call(gw, "policy_limits")["policy"]["max_leverage"] == 3.0
        assert call(gw, "account_summary")["equity_usd"] == 10_000.0
        assert call(gw, "kill_switch", {"reason": "x"}, client="cursor")["ok"] and eng.killed
    # daemon down: kill still writes the shared file, nothing else works
    KillSwitch(env["cfg"].state_dir).clear("I UNDERSTAND")
    res = call(gw, "kill_switch", {"reason": "again"})
    assert res["ok"] and res["daemon_running"] is False and is_killed(env["cfg"].state_dir)
    assert call(gw, "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})["block_reasons"] == \
        ["DAEMON_NOT_RUNNING"]


def test_quote_status_summary_preview(env):
    q = call(env["gw"], "get_quote", {"coin": "BTC"})
    assert q["ok"] and q["mid"] == 100.0 and q["age_ms"] == 0
    pv = call(env["gw"], "preview_order", {"coin": "ETH", "side": "sell", "size": 20.0, "algo": "twap",
                                           "duration_s": 60, "slices": 4})
    assert pv["would_need_human_approval"] and len(pv["engine_preview"]["children"]) == 4
    assert pv["intent"]["source"] == "agent:claude-desktop"
    assert call(env["gw"], "order_status", {"intent_id": "ag-none"})["block_reasons"] == ["UNKNOWN_INTENT_ID"]
    s = call(env["gw"], "account_summary")
    assert s["equity_usd"] == 10_000.0 and s["agent_budget_remaining_usd"] == 5000.0


def test_agent_disabled_and_ineligible(tmp_path):
    cfg = make_cfg(tmp_path, agent=False)
    eng = FakeEngine()
    gw = AgentGateway(eng, cfg, ApprovalQueue(cfg.state_dir), AgentBudget(cfg.state_dir, 5000.0))
    assert gw.submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 1})["block_reasons"] == ["AGENT_DISABLED"]
    cfg2 = make_cfg(tmp_path / "b")
    gw2 = AgentGateway(eng, cfg2, ApprovalQueue(cfg2.state_dir), AgentBudget(cfg2.state_dir, 5000.0))
    assert gw2.submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 1})["block_reasons"] == \
        ["ELIGIBILITY_NOT_CONFIRMED"]
    assert eng.submitted == []


def test_client_source_sanitised():
    assert client_source("Claude Desktop") == "agent:Claude-Desktop"
    assert client_source("") == "agent:unknown"
    assert client_source(None) == "agent:unknown"
    assert client_source("x" * 99) == "agent:" + "x" * 40


def test_agent_actions_audited(env):
    call(env["gw"], "submit_order", {"coin": "ETH", "side": "buy", "size": 20.0})
    call(env["gw"], "kill_switch", {"reason": "stop"})
    kinds = [json.loads(l)["kind"] for l in env["cfg"].audit_path.read_text().splitlines()]
    assert kinds == ["approval_queued", "agent_kill"]
    assert AuditLog.verify(env["cfg"].audit_path)[0]


def test_against_real_engine_with_sim_venue(tmp_path):
    """Wiring check with the real Engine: tagging reaches the engine's audit, kill trips the shared file."""
    from omx.engine import Engine
    from omx.killswitch import is_killed
    from omx.venue.sim import SimConfig, SimVenue
    cfg = make_cfg(tmp_path)
    make_eligible(cfg.state_dir)
    audit = AuditLog(cfg.audit_path)
    eng = Engine(SimVenue(SimConfig(seed=1)), cfg.policy, audit, state_dir=cfg.state_dir)
    gw = AgentGateway(eng, cfg, ApprovalQueue(cfg.state_dir, audit), AgentBudget(cfg.state_dir, 5000.0), audit)
    assert call(gw, "get_quote", {"coin": "ETH"})["ok"]
    res = call(gw, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.01}, client="cursor")
    assert res["status"] == "submitted" and "state" in res["report"]
    recs = [json.loads(l) for l in cfg.audit_path.read_text().splitlines()]
    got = [r["data"]["intent"]["source"] for r in recs if r["kind"] == "intent_received"]
    assert got == ["agent:cursor"]
    assert call(gw, "order_status", {"intent_id": "ag-missing"})["block_reasons"] == ["UNKNOWN_INTENT_ID"]
    call(gw, "kill_switch", {"reason": "test"})
    assert is_killed(cfg.state_dir) and eng.killed
    assert AuditLog.verify(cfg.audit_path)[0]


def test_inspect_mode_lists_every_tool_and_refuses_every_call(tmp_path, monkeypatch, env):
    from omx.mcp_server import InspectGateway
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    gw = InspectGateway()
    assert list_tools(gw) == list_tools(env["gw"])                        # the same tool surface
    for tool, args in [("get_quote", {"coin": "ETH"}), ("account_summary", {}), ("policy_limits", {}),
                       ("kill_switch", {"reason": "inspect test"}), ("order_status", {"intent_id": "x"})]:
        r = call(gw, tool, args)
        assert r["ok"] is False and "NOT_CONFIGURED" in r["block_reasons"], (tool, r)
    assert list(cwd.iterdir()) == []                                       # nothing written
