"""Builder-code enforcement (offline): the engine attaches the configured builder to every order,
callers cannot set or remove it, fills report the builder fee apart from the venue fee, and the
daemon refuses new intents while maxBuilderFee is below the configured fee (BUILDER_NOT_APPROVED)."""
import dataclasses
import io
import json
import time
from pathlib import Path

import pytest
from eth_account import Account

from omx import ipc
from omx.approvals import make_intent
from omx.audit import AuditLog
from omx.builder import BUILDER_NOT_APPROVED, builder_of, not_approved_message, parse_max_builder_fee
from omx.cli import main
from omx.config import ConfigError, load, loads, render
from omx.engine import Engine, from_config
from omx.mcp_server import DaemonGateway
from omx.policy import PolicyConfig
from omx.types import ChildOrder, Intent, Side
from omx.venue.base import VenueError
from omx.venue.hyperliquid import TESTNET_API_URL, HyperliquidVenue, LocalKeySigner, TokenBucket
from omx.venue.sim import CoinSpec, SimConfig, SimVenue

from test_mcp import ACCOUNT, daemon_running, make_eligible

BUILDER = "0x" + "be" * 20
FEE = 10                                               # 1 bps
CAPS = dict(min_depth_usd=0.0, depth_band_bps=25.0, allowed_coins=frozenset({"ETH", "SOL"}),
            max_account_notional_usd=50_000.0, default_coin_notional_usd=50_000.0, max_child_notional_usd=50_000.0)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any socket connect during these tests is a bug."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def quiet():
    spec = dict(vol_bps_sqrt_s=0.0, trade_rate_hz=0.0)
    return (CoinSpec("ETH", 3000.0, 0.1, 4, **spec), CoinSpec("SOL", 150.0, 0.01, 2, level_sz=100.0, **spec))


def sim_engine(tmp_path, builder=(BUILDER, FEE), **simkw):
    sim = SimVenue(SimConfig(seed=3, **simkw), coins=quiet())
    eng = Engine(sim, PolicyConfig(**CAPS), AuditLog(tmp_path / "audit.jsonl"),
                 builder_fee_tenths_bps=builder[1] if builder else 0,
                 builder_address=builder[0] if builder else None, state_dir=tmp_path / "state")
    return sim, eng


def intent(iid="i-1", **kw):
    kw.setdefault("algo", "twap")
    return make_intent(coin="ETH", side="buy", size=kw.pop("size", 1.0), source="human", intent_id=iid, **kw)


def run(sim, eng, ms, step=100):
    for _ in range(ms // step):
        sim.advance(step)
        eng.step()


# ------------------------------------------------------------ engine + sim

def test_every_child_carries_the_builder_and_fills_split_the_fee(tmp_path):
    sim, eng = sim_engine(tmp_path)
    assert eng.builder == (BUILDER, FEE) and sim.builder == (BUILDER, FEE)
    eng.submit(intent(duration_s=4, slices=4, max_slippage_bps=100))
    run(sim, eng, 8_000)
    rep = eng.status("i-1")
    assert rep.state == "done" and rep.children_sent == 4
    for cid in (f"i-1.{k}" for k in range(1, 5)):
        assert sim.order_status(cid)["builder"] == (BUILDER, FEE)
    fills = sim.fills_since(0)
    assert fills
    for f in fills:
        ntl = f.size * f.px
        assert f.builder_fee_usd == pytest.approx(ntl * FEE / 1e5)
        assert f.fee_usd == pytest.approx(ntl * (4.5 if f.crossed else 1.5) / 1e4)   # venue fee apart
    assert rep.builder_fee_usd == pytest.approx(sum(f.size * f.px for f in fills) * FEE / 1e5)
    recs = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    sent = [r["data"]["child"] for r in recs if r["kind"] == "child_decision"]
    assert sent and all(c["builder"] == [BUILDER, FEE] for c in sent)


def test_no_builder_configured_sends_none(tmp_path):
    sim, eng = sim_engine(tmp_path, builder=None)
    eng.submit(intent(algo="market", size=0.5))
    run(sim, eng, 1_000)
    assert sim.order_status("i-1.1")["builder"] is None
    assert all(f.builder_fee_usd == 0 for f in sim.fills_since(0))
    assert eng.builder_approval() == {"required": False, "ok": True, "builder": None, "configured": 0,
                                      "approved": None}


def test_fee_without_address_and_venue_mismatch_fail_closed(tmp_path):
    sim = SimVenue(SimConfig(), coins=quiet())
    with pytest.raises(ValueError, match="needs a builder address"):
        Engine(sim, PolicyConfig(**CAPS), AuditLog(tmp_path / "a.jsonl"), builder_fee_tenths_bps=10)

    class Pinned(SimVenue):                  # a venue that keeps its own builder whatever it is told
        def set_builder(self, builder):
            self.builder = ("0x" + "11" * 20, 5)
    with pytest.raises(ValueError, match="BUILDER_MISMATCH"):
        Engine(Pinned(SimConfig(), coins=quiet()), PolicyConfig(**CAPS), AuditLog(tmp_path / "b.jsonl"),
               builder_fee_tenths_bps=FEE, builder_address=BUILDER)


def test_sim_refuses_a_child_with_another_builder(tmp_path):
    sim, _ = sim_engine(tmp_path)
    for other in (("0x" + "11" * 20, FEE), (BUILDER, 5)):
        c = ChildOrder("x.1", "x", "ETH", Side.BUY, 0.1, 2990.0, "Gtc", builder=other)
        with pytest.raises(VenueError, match="BUILDER_MISMATCH"):
            sim.place(c)
    # a child without the field still gets the venue's builder (a caller cannot remove it)
    sim.place(ChildOrder("y.1", "y", "ETH", Side.BUY, 0.1, 2990.0, "Gtc"))
    assert sim.order_status("y.1")["builder"] == (BUILDER, FEE)


def test_callers_cannot_set_or_remove_the_builder(tmp_path):
    assert "builder" not in {f.name for f in dataclasses.fields(Intent)}
    with pytest.raises(TypeError):
        make_intent(coin="ETH", side="buy", size=1.0, builder={"b": BUILDER, "f": 0})
    # MCP gateway and daemon human requests refuse unknown order fields
    from omx.mcp_server import AgentGateway
    cfg = loads(render(ACCOUNT, "testnet", key_path="k", builder_address=BUILDER, fee_tenths_bps=FEE,
                       agent_enabled=True), base_dir=tmp_path)
    gw = AgentGateway.__new__(AgentGateway)
    gw.cfg = cfg
    with pytest.raises(ValueError, match="BAD_ARGS"):
        gw._intent("agent:x", {"coin": "ETH", "side": "buy", "size": 1.0, "builder": None})
    from omx.daemon import Daemon
    d = Daemon(cfg, lambda c: None, console=None)
    with pytest.raises(ValueError, match="BAD_ARGS"):
        d._human_intent({"coin": "ETH", "side": "buy", "size": 1.0, "builder": {"b": BUILDER, "f": 0}})


def test_engine_block_refuses_new_intents_only(tmp_path):
    sim, eng = sim_engine(tmp_path)
    assert eng.builder_approval()["ok"] is False and eng.builder_approval()["approved"] == 0
    sim.approve_builder_fee(BUILDER, FEE)
    assert eng.builder_approval() == {"required": True, "ok": True, "builder": BUILDER, "configured": FEE,
                                      "approved": FEE}
    eng.submit(intent("w-1", duration_s=6, slices=3, max_slippage_bps=100))
    run(sim, eng, 500)
    eng.set_builder_block(not_approved_message((BUILDER, FEE), 0))
    rep = eng.submit(intent("n-1"))
    assert rep.state == "rejected" and rep.block_reasons[0].startswith(BUILDER_NOT_APPROVED)
    assert "omx approve-builder" in rep.block_reasons[0]
    assert eng.preview(intent("p-1"))["builder_block"].startswith(BUILDER_NOT_APPROVED)
    run(sim, eng, 8_000)
    assert eng.status("w-1").state == "done" and eng.status("w-1").children_sent == 3   # working intent finished
    eng.set_builder_block(None)
    assert eng.submit(intent("n-2", algo="market", size=0.1)).state != "rejected"
    rec = eng.kill("test")
    assert rec["killed"] is True


def test_builder_approval_read_errors_are_not_approvals(tmp_path):
    sim, eng = sim_engine(tmp_path)
    sim.max_builder_fee = lambda b=None: (_ for _ in ()).throw(RuntimeError("timeout"))
    res = eng.builder_approval()
    assert res["ok"] is False and res["approved"] is None and "timeout" in res["error"]


def test_parse_max_builder_fee():
    assert parse_max_builder_fee(10) == 10 and parse_max_builder_fee(0) == 0 and parse_max_builder_fee("25") == 25
    for bad in (None, True, "0.01%", {"x": 1}, -1, 1.5):
        with pytest.raises(ValueError):
            parse_max_builder_fee(bad)
    assert builder_of(BUILDER.upper().replace("0X", "0x"), 10) == (BUILDER, 10)
    assert builder_of(BUILDER, 0) is None and builder_of(None, 10) is None
    with pytest.raises(ValueError):
        builder_of(BUILDER, 101)


# ------------------------------------------------------------ Hyperliquid adapter

META = [{"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
        [{"dayNtlVlm": "900000000.0", "markPx": "1891.4", "oraclePx": "1891", "prevDayPx": "1800"}]]


class FakeHL:
    def __init__(self, approved=FEE):
        self.calls, self.approved = [], approved

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path == "/exchange":
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": "0.01", "avgPx": "1891.5", "oid": 77}}]}}}
        now = int(time.time() * 1000)
        t = payload["type"]
        return {"metaAndAssetCtxs": META,
                "l2Book": {"coin": "ETH", "time": now, "levels": [[{"px": "1891.0", "sz": "500", "n": 3}],
                                                                  [{"px": "1891.5", "sz": "500", "n": 3}]]},
                "clearinghouseState": {"marginSummary": {"accountValue": "1000"}, "assetPositions": [], "time": now},
                "frontendOpenOrders": [], "userFillsByTime": [], "userNonFundingLedgerUpdates": [],
                "orderStatus": {"status": "unknownOid"}, "userAbstraction": "disabled",
                "maxBuilderFee": self.approved}[t]


def hl(agent, transport, builder=(BUILDER, FEE), account=ACCOUNT):
    return HyperliquidVenue(TESTNET_API_URL, account, agent, builder=builder, transport=transport,
                            limiter=TokenBucket(sleep=lambda s: None), sleep=lambda s: None)


def test_hl_builder_must_differ_from_account_and_agent():
    agent = LocalKeySigner(Account.create())
    with pytest.raises(VenueError, match="differ"):
        hl(agent, FakeHL(), builder=(ACCOUNT, FEE))
    with pytest.raises(VenueError, match="differ"):
        hl(agent, FakeHL(), builder=(agent.address, FEE))
    with pytest.raises(ConfigError, match="differ"):
        loads(render(ACCOUNT, "testnet", key_path="k", builder_address=ACCOUNT, fee_tenths_bps=FEE))


def test_hl_attaches_builder_always_and_refuses_another():
    agent = LocalKeySigner(Account.create())
    v = hl(agent, FakeHL())
    plain = ChildOrder("c-1", "i", "ETH", Side.BUY, 0.01, 1891.5, "Ioc")
    assert v.build_order_action(plain)["builder"] == {"b": BUILDER, "f": FEE}
    ours = dataclasses.replace(plain, builder=(BUILDER, FEE))
    assert v.build_order_action(ours)["builder"] == {"b": BUILDER, "f": FEE}
    for other in ((BUILDER, 0), ("0x" + "11" * 20, FEE)):
        with pytest.raises(VenueError, match="BUILDER_MISMATCH"):
            v.build_order_action(dataclasses.replace(plain, builder=other))
    bare = hl(agent, FakeHL(), builder=None)
    assert "builder" not in bare.build_order_action(plain)
    with pytest.raises(VenueError, match="BUILDER_MISMATCH"):
        bare.build_order_action(ours)


def test_hl_max_builder_fee_request_shape():
    t = FakeHL(approved=25)
    v = hl(LocalKeySigner(Account.create()), t)
    assert v.max_builder_fee() == 25
    assert t.calls[-1] == ("/info", {"type": "maxBuilderFee", "user": ACCOUNT, "builder": BUILDER})
    t.approved = {"error": "x"}
    with pytest.raises(ValueError):
        v.max_builder_fee()


def test_from_config_order_on_the_wire_carries_the_builder(tmp_path):
    make_eligible(tmp_path / "state")
    cfg = loads(render(ACCOUNT, "testnet", key_path="k", builder_address=BUILDER, fee_tenths_bps=FEE),
                base_dir=tmp_path)
    t = FakeHL(approved=FEE)
    eng = from_config(cfg, signer=LocalKeySigner(Account.create()), transport=t)
    assert eng.builder == (BUILDER, FEE) and eng.builder_approval()["ok"] is True
    rep = eng.submit(make_intent(coin="ETH", side="buy", size=0.01, algo="market", source="human",
                                 intent_id="hu-1", max_slippage_bps=50))
    bodies = [p for path, p in t.calls if path == "/exchange"]
    assert bodies, rep
    assert all(b["action"]["builder"] == {"b": BUILDER, "f": FEE} for b in bodies if b["action"]["type"] == "order")


# ------------------------------------------------------------ daemon

class SimTimeEngine(Engine):
    def step(self):
        self.venue.advance(250)
        return super().step()


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text(render(ACCOUNT, "testnet", key_path="secrets/agent.key", builder_address=BUILDER,
                        fee_tenths_bps=FEE, agent_enabled=True), encoding="utf-8")
    make_eligible(tmp_path / "state")
    return p


def factory(sim):
    return lambda c: SimTimeEngine(sim, c.policy, AuditLog(c.audit_path), state_dir=c.state_dir,
                                   builder_fee_tenths_bps=c.builder_fee_tenths_bps, builder_address=c.builder_address)


def cli(cfgfile, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--config", str(cfgfile), *argv], engine_factory=lambda c: None, input_fn=lambda p: "y",
                out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def wait_for(fn, timeout_s=20.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_daemon_not_approved_refuses_new_intents_then_lifts(cfgfile):
    cfg = load(cfgfile)
    sim = SimVenue(SimConfig(seed=1, require_builder_approval=True), coins=quiet())
    with daemon_running(cfg, factory(sim), builder_retry_s=0.1) as d:
        assert d.builder_block and d.builder_block.startswith(BUILDER_NOT_APPROVED)
        code, out, err = cli(cfgfile, "submit", "ETH", "buy", "0.5", "--algo", "market", "--yes")
        assert code == 1 and BUILDER_NOT_APPROVED in err and "omx approve-builder" in err
        res = DaemonGateway(cfg.state_dir).submit_order("agent:a", {"coin": "ETH", "side": "buy", "size": 0.1})
        assert BUILDER_NOT_APPROVED in json.dumps(res)
        assert ipc.read_heartbeat(cfg.state_dir) is not None
        assert wait_for(lambda: (ipc.read_heartbeat(cfg.state_dir) or {}).get("builder_not_approved") is True)
        code, out, err = cli(cfgfile, "kill", "--reason", "kill always works")
        assert code == 0, err
        code, out, err = cli(cfgfile, "unkill")       # needs a human console: refused, kill stays
        sim.approve_builder_fee(BUILDER, FEE)
        wait_for(lambda: d.builder_block is None)
    assert sim.place_calls == 0
    recs = [json.loads(x) for x in cfg.audit_path.read_text().splitlines()]
    start = next(r for r in recs if r["kind"] == "daemon_start")["data"]
    assert start["builder"] == [BUILDER, FEE] and start["builder_block"].startswith(BUILDER_NOT_APPROVED)
    assert any(r["kind"] == "builder_check" and r["data"]["ok"] for r in recs)


def test_daemon_revocation_keeps_working_intents_running(cfgfile):
    cfg = load(cfgfile)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    sim.approve_builder_fee(BUILDER, FEE)
    with daemon_running(cfg, factory(sim), builder_check_s=0.1, builder_retry_s=0.1) as d:
        assert d.builder_block is None
        code, out, err = cli(cfgfile, "submit", "ETH", "buy", "1.0", "--algo", "twap", "--duration-s", "20",
                             "--slices", "4", "--yes")
        assert code == 0, err
        iid = json.loads(out[: out.index("\nBuilder fee")])["intent"]["intent_id"]
        sim.approve_builder_fee(BUILDER, 0)           # revoked mid-run
        wait_for(lambda: d.builder_block is not None)
        code, _, err = cli(cfgfile, "submit", "ETH", "buy", "0.1", "--algo", "market", "--yes")
        assert code == 1 and BUILDER_NOT_APPROVED in err
        code, out, err = cli(cfgfile, "status", iid)
        rep = wait_for(lambda: (r := json.loads(cli(cfgfile, "status", iid)[1]))["state"] == "done" and r,
                       timeout_s=30)
    assert rep["children_sent"] == 4                  # the working intent was not stopped


def test_daemon_start_read_error_blocks_and_later_error_keeps_state(cfgfile):
    cfg = load(cfgfile)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    calls = {"n": 0}

    def flaky(b=None):
        calls["n"] += 1
        if calls["n"] == 1 or calls["n"] >= 3:
            raise RuntimeError("connection reset")
        return FEE
    sim.max_builder_fee = flaky
    with daemon_running(cfg, factory(sim), builder_check_s=0.1, builder_retry_s=0.1) as d:
        assert d.builder_block and "connection reset" in d.builder_block      # unknown at start: refused
        wait_for(lambda: calls["n"] >= 2 and d.builder_block is None)        # a good read lifts it
        wait_for(lambda: calls["n"] >= 5)
        assert d.builder_block is None                                        # transient errors keep it


def test_daemon_without_builder_never_checks(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text(render(ACCOUNT, "testnet", key_path="k", agent_enabled=True), encoding="utf-8")
    make_eligible(tmp_path / "state")
    cfg = load(p)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, lambda c: SimTimeEngine(sim, c.policy, AuditLog(c.audit_path),
                                                     state_dir=c.state_dir)) as d:
        time.sleep(0.2)
        assert d.builder_block is None
    assert sim.builder_checks == 0
