"""Regressions for review findings F1, F5a, F5b (interface side), F8b, F8c, F10, rewritten for the
R1 process model: `omx daemon` owns the engine; CLI and MCP are clients over the file IPC."""
import asyncio
import io
import json
import threading
import time

from mcp import Client, Implementation

from omx.approvals import AgentBudget, ApprovalQueue, make_intent
from omx.audit import AuditLog
from omx.cli import main as cli_main
from omx.config import loads, render
from omx.engine import Engine
from omx.killswitch import KillSwitch
from omx.mcp_server import AgentGateway, DaemonGateway, build_server
from omx.policy import PolicyConfig
from omx.venue.sim import SimConfig, SimVenue

from test_daemon import SimTimeEngine, quiet, sim_factory, wait_for
from test_mcp import ACCOUNT, FakeEngine, confirming_console, daemon_running, make_eligible


def cfg_in(tmp_path, agent=True):
    text = render(ACCOUNT, "testnet", key_path="secrets/agent.key", agent_enabled=agent)
    p = tmp_path / "omx.toml"
    p.write_text(text, encoding="utf-8")
    cfg = loads(text, base_dir=tmp_path)
    make_eligible(cfg.state_dir)
    return p, cfg


def cli(argv, factory=None, answers=()):
    out, err = io.StringIO(), io.StringIO()
    a = list(answers)
    code = cli_main(argv, engine_factory=factory or (lambda c: None), input_fn=lambda p: a.pop(0),
                    out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def submitted_id(out):
    return json.loads(out[: out.index("\nBuilder fee")])["intent"]["intent_id"]


# F1: CLI submit -> cancel finds the intent; kill cancels the resting OMX order
def test_F1_cli_cancel_and_kill_reach_the_engine_that_placed_the_order(tmp_path):
    p, cfg = cfg_in(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):
        args = ["--config", str(p), "submit", "ETH", "buy", "1.0", "--algo", "limit", "--limit-px", "2990", "--yes"]
        code, out, err = cli(args)
        assert code == 0, err
        iid = submitted_id(out)
        code, out2, _ = cli(["--config", str(p), "cancel", iid])
        assert code == 0 and "UNKNOWN_INTENT" not in out2 and json.loads(out2)["state"] == "cancelled"
        code, out, err = cli(args)
        iid2 = submitted_id(out)
        time.sleep(0.3)
        code, _, _ = cli(["--config", str(p), "kill", "--reason", "stop"])
        assert code == 0 and KillSwitch(cfg.state_dir).is_killed()
        assert json.loads(cli(["--config", str(p), "status", iid2])[1])["state"] == "killed"
    sim.advance(2_000)
    assert sim.account().open_orders == (), "kill left an OMX order resting on the venue"


# F1b: the daemon loop keeps stepping, so every TWAP slice is sent
def test_F1b_cli_twap_sends_every_slice(tmp_path):
    p, cfg = cfg_in(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):
        code, out, err = cli(["--config", str(p), "submit", "ETH", "buy", "1.0", "--algo", "twap",
                              "--duration-s", "10", "--slices", "5", "--yes"])
        assert code == 0, err
        iid = submitted_id(out)
        wait_for(lambda: json.loads(cli(["--config", str(p), "status", iid])[1])["state"] == "done")
    assert sim.place_calls == 5


# F5a: parallel MCP submits cannot exceed the daily agent budget (in-process gateway and daemon path)
class SlowEngine(FakeEngine):
    def submit(self, intent):
        time.sleep(0.1)
        return super().submit(intent)


def _parallel_submits(gw, n=12):
    async def go():
        async with Client(build_server(gw), client_info=Implementation(name="x", version="1")) as c:
            return await asyncio.gather(*[c.call_tool("submit_order", {"coin": "ETH", "side": "buy", "size": 9.0})
                                          for _ in range(n)])
    return asyncio.run(go())


def test_F5a_parallel_submits_in_process_gateway(tmp_path):
    p, cfg = cfg_in(tmp_path)
    eng = SlowEngine()
    _parallel_submits(AgentGateway(eng, cfg, ApprovalQueue(cfg.state_dir), AgentBudget(cfg.state_dir, 5_000.0)))
    assert 9.0 * 100 * 1.005 * len(eng.submitted) <= 5_000.0 and len(eng.submitted) == 5


def test_F5a_parallel_submits_through_daemon(tmp_path):
    p, cfg = cfg_in(tmp_path)
    eng = SlowEngine()
    with daemon_running(cfg, lambda c: eng):
        rs = _parallel_submits(DaemonGateway(cfg.state_dir, timeout_s=30))
    assert all(not r.is_error for r in rs)
    assert 9.0 * 100 * 1.005 * len(eng.submitted) <= 5_000.0 and len(eng.submitted) == 5


# F5b (interface side): concurrent clients cannot run engine calls concurrently, so the coin cap holds
class SlowPlace(SimVenue):
    def place(self, child, abort=None):
        time.sleep(0.2)                                   # signing + HTTP round trip
        return super().place(child, abort=abort)


def test_F5b_concurrent_cli_submits_respect_coin_cap(tmp_path):
    p, cfg = cfg_in(tmp_path)
    sim = SlowPlace(SimConfig(seed=1, start_equity_usd=1e5), coins=quiet())
    pol = PolicyConfig(default_coin_notional_usd=5_000, max_account_notional_usd=1e5, max_child_notional_usd=5_000,
                       allowed_coins=cfg.policy.allowed_coins, min_depth_usd=0.0, min_day_ntl_usd=0.0)
    factory = lambda c: SimTimeEngine(sim, pol, AuditLog(c.audit_path), state_dir=c.state_dir)   # noqa: E731
    with daemon_running(cfg, factory):
        args = ["--config", str(p), "submit", "ETH", "buy", "1.0", "--algo", "market", "--yes"]
        ts = [threading.Thread(target=cli, args=(args,)) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        time.sleep(0.3)
    sim.advance(1_000)
    pos = sim.account().position("ETH")
    assert pos * 3000 <= 5_000 + 1e-6, f"coin cap 5000 USD, position {pos} ETH"


# F8b: CLI `approvals approve` -> the daemon executes with its single audit writer -> chain intact
def test_F8b_cli_approvals_approve_keeps_audit_chain(tmp_path):
    p, cfg = cfg_in(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    q = ApprovalQueue(cfg.state_dir, AuditLog(cfg.audit_path))
    rec = q.enqueue(make_intent(coin="ETH", side="buy", size=1.0, algo="market", source="agent:x"),
                    ["ABOVE_APPROVAL_THRESHOLD"], 3000.0)
    with daemon_running(cfg, sim_factory(sim), console=confirming_console()):
        code, _, err = cli(["--config", str(p), "approvals", "approve", rec["approval_id"], "--yes"])
        assert code == 0, err
    ok, n, msg = AuditLog.verify(cfg.audit_path)
    assert ok, msg
    kinds = [json.loads(line)["kind"] for line in cfg.audit_path.read_text().splitlines()]
    assert "approval_executed" in kinds and "intent_received" in kinds


# F8c: the MCP process writes no audit records; the daemon is the one writer
def test_F8c_mcp_through_daemon_keeps_audit_chain(tmp_path):
    p, cfg = cfg_in(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    gw = DaemonGateway(cfg.state_dir)
    with daemon_running(cfg, sim_factory(sim, Engine)):
        r = gw.submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 0.1, "algo": "market"})
        assert r["ok"], r
        assert gw.kill_switch("agent:x", "test")["ok"]
    ok, n, msg = AuditLog.verify(cfg.audit_path)
    assert ok, msg
    kinds = [json.loads(line)["kind"] for line in cfg.audit_path.read_text().splitlines()]
    assert {"agent_submit", "agent_kill", "kill"} <= set(kinds)


# F10: a key-shaped kill reason is refused before it reaches the audit log or the KILL file
def test_F10_agent_kill_reason_with_key_refused(tmp_path):
    p, cfg = cfg_in(tmp_path)
    key = "0x" + "1f" * 32
    audit = AuditLog(cfg.audit_path)
    eng = FakeEngine()
    gw = AgentGateway(eng, cfg, ApprovalQueue(cfg.state_dir), AgentBudget(cfg.state_dir, 5e3), audit)
    assert not gw.kill_switch("agent:x", key)["ok"] and not eng.killed
    dg = DaemonGateway(cfg.state_dir)                     # client side refuses too, daemon or not
    assert not dg.kill_switch("agent:x", f"reason {key}")["ok"]
    assert not KillSwitch(cfg.state_dir).is_killed()
    assert not cfg.audit_path.exists() or key not in cfg.audit_path.read_text()
