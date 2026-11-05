"""Fix round R3 (third review, _review/test_review_findings_r3.py), items R3-2..R3-11 of the lead's list.

Offline only: SimVenue, the fake HL transport, throwaway keys created in-test. No network.
Lead item -> review test: R3-2 deposits = review R3_1, R3-3 agent idempotency = R3_7, R3-4 expiry =
R3_6, R3-5 instances = R3_5, R3-6 spot = R3_3, R3-7 daemon kill = R3_4, R3-8 status = R3_8, R3-9
weight = R3_2, R3-10 console text = R3_9, R3-11 NUL console = R3_10.
"""
import dataclasses
import io
import json
import os
import sys
import time

import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from omx import ipc
from omx.approvals import ApprovalQueue, make_intent
from omx.audit import AuditLog
from omx.cli import _sweep_warning
from omx.cli import main as cli_main
from omx.config import account_lock_path, load, render
from omx.daemon import Daemon, interactive_console
from omx.engine import EXPIRY_MARGIN_MS, Engine, PollSchedule, from_config
from omx.killswitch import kill_from_outside
from omx.mcp_server import DaemonGateway
from omx.policy import PolicyConfig
from omx.tty import is_console
from omx.types import Algo, Intent, Side
from omx.venue import hyperliquid as hlmod
from omx.venue.base import cloid_for, cloid_instance, instance_tag, is_omx_cloid
from omx.venue.hyperliquid import TESTNET_API_URL, HyperliquidVenue, LocalKeySigner, TokenBucket
from omx.venue.sim import SimConfig, SimVenue

from test_daemon import SimTimeEngine, quiet, sim_factory, wait_for
from test_hyperliquid import BOOK, NOW, USER, FakeClock, FakeTransport, ok_order
from test_mcp import ACCOUNT, FakeEngine, confirming_console, daemon_running, make_eligible

WEIGHT = {"clearinghouseState": 2, "frontendOpenOrders": 20, "userFillsByTime": 20, "userNonFundingLedgerUpdates": 20,
          "metaAndAssetCtxs": 20, "l2Book": 2, "orderStatus": 2, "spotMeta": 20}
CANCEL_OK = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def cfgfile_at(tmp_path, **sub):
    text = render(ACCOUNT, "testnet", key_path="secrets/agent.key", agent_enabled=True)
    for k, v in sub.items():
        text = text.replace(k, v)
    p = tmp_path / "omx.toml"
    p.write_text(text, encoding="utf-8")
    return p


def cli(p, *argv, factory=None, answers=()):
    out, err = io.StringIO(), io.StringIO()
    a = list(answers)
    code = cli_main(["--config", str(p), *argv], engine_factory=factory or (lambda c: None),
                    input_fn=lambda q: a.pop(0), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def hl_engine(tmp_path, info=None, exchange=(), policy=None, **vkw):
    """Real Engine on the real HL adapter, fake transport, fake clock (limiter sleeps advance it)."""
    clk = FakeClock()
    now_ms = lambda: NOW + round((clk.t - 1000) * 1000)            # noqa: E731
    base = {"clearinghouseState": {"assetPositions": [], "marginSummary": {"accountValue": "10000"}, "time": NOW},
            "frontendOpenOrders": [], "userFillsByTime": [], "userNonFundingLedgerUpdates": [],
            "l2Book": lambda p: {**BOOK, "time": now_ms() - 100}, "orderStatus": {"status": "unknownOid"}}
    base.update(info or {})
    t = FakeTransport(info=base, exchange=exchange)
    signer = LocalKeySigner(Account.create())
    v = HyperliquidVenue(TESTNET_API_URL, USER, signer, transport=t, limiter=TokenBucket(clock=clk, sleep=clk.sleep),
                         clock_ms=now_ms, sleep=clk.sleep, **vkw)
    eng = Engine(v, policy or PolicyConfig(min_depth_usd=0.0), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    return eng, clk, t


def run_steps(eng, clk, seconds, dt=0.2, each=None):
    for _ in range(int(round(seconds / dt))):
        clk.t += dt
        eng.step()
        if each is not None:
            each()


def recs(path, kind):
    return [r for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()) if r["kind"] == kind]


# ====================================================================== R3-2 deposits never mask a loss
def ledger_engine(tmp_path):
    st = {"eq": "10000", "ledger": []}
    eng, clk, t = hl_engine(tmp_path, info={
        "clearinghouseState": lambda p: {"assetPositions": [], "time": NOW, "marginSummary": {"accountValue": st["eq"]}},
        "userNonFundingLedgerUpdates": lambda p: list(st["ledger"])})
    return eng, clk, t, st


@pytest.mark.parametrize("deposit", [600.0, 1000.0])
def test_R3_2_deposit_after_a_loss_keeps_the_daily_loss_limit_until_the_ledger_shows_it(tmp_path, deposit):
    eng, clk, t, st = ledger_engine(tmp_path)
    eng.step()                                                          # day start 10000, ledger read
    clk.t += 3.0
    st["eq"] = "9400"                                                   # lost 600 (limit 500)
    eng.step()
    assert eng._acct.day_pnl_usd() == pytest.approx(-600.0)
    st["eq"] = str(9400 + deposit)                                      # the user adds margin
    st["ledger"] = [{"time": NOW + 3500, "hash": "0x1", "delta": {"type": "deposit", "usdc": str(deposit)}}]
    clk.t += 0.2
    eng.step()
    assert t.count("userNonFundingLedgerUpdates") == 1                  # early read waits for its gap
    assert eng._acct.day_pnl_usd() == pytest.approx(-600.0)             # whole rise since the low = deposit
    rep = eng.submit(Intent("hu-dl", "BTC", Side.BUY, 0.01, algo=Algo.LIMIT, limit_px=123400.0))
    assert rep.state == "rejected" and "DAILY_LOSS_LIMIT" in rep.block_reasons
    pnls = []
    run_steps(eng, clk, 12.0, each=lambda: pnls.append(eng._acct.day_pnl_usd()))
    assert max(pnls) <= -600.0 + 1e-9
    assert t.count("userNonFundingLedgerUpdates") == 2                  # early ledger read (gap 10 s)
    assert eng._acct.day_net_flows_usd == pytest.approx(deposit) and pnls[-1] == pytest.approx(-600.0)
    rep = eng.submit(Intent("hu-dl2", "BTC", Side.BUY, 0.01, algo=Algo.LIMIT, limit_px=123400.0))
    assert rep.state == "rejected" and "DAILY_LOSS_LIMIT" in rep.block_reasons   # the loss is real


def test_R3_2_rise_above_the_low_triggers_an_early_ledger_read_even_below_the_reference(tmp_path):
    eng, clk, t, st = ledger_engine(tmp_path)
    eng.step()
    clk.t += 3.0
    st["eq"] = "9000"
    eng.step()
    st["eq"] = "9300"                                                   # below 10000, but 300 above the low
    st["ledger"] = [{"time": NOW + 3500, "hash": "0x2", "delta": {"type": "deposit", "usdc": "300"}}]
    run_steps(eng, clk, 8.0)
    assert t.count("userNonFundingLedgerUpdates") == 2 and eng._acct.day_pnl_usd() == pytest.approx(-1000.0)


def test_R3_2_a_real_gain_counts_after_the_next_ledger_read(tmp_path):
    eng, clk, t, st = ledger_engine(tmp_path)
    eng.step()
    st["eq"] = "10200"                                                  # trading gain, no ledger entry
    clk.t += 0.2
    eng.step()
    assert eng._acct.day_pnl_usd() == pytest.approx(0.0)                # treated as a possible deposit
    run_steps(eng, clk, 11.0)
    assert eng._acct.day_pnl_usd() == pytest.approx(200.0)              # the ledger read shows no flow


# ====================================================================== R3-3 agent client_intent_id
def agent_daemon_cfg(tmp_path, **sub):
    p = cfgfile_at(tmp_path, **sub)
    make_eligible(tmp_path / "state")
    return load(p)


class SlowSubmit(SimTimeEngine):
    def submit(self, intent):
        time.sleep(1.0)                                                 # venue slow for one request
        return super().submit(intent)


def test_R3_3_agent_retry_with_client_intent_id_never_doubles_and_survives_restart(tmp_path):
    cfg = agent_daemon_cfg(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    order = {"coin": "ETH", "side": "buy", "size": 0.1, "algo": "market", "client_intent_id": "retry-0001"}
    with daemon_running(cfg, sim_factory(sim, SlowSubmit)):
        r1 = DaemonGateway(cfg.state_dir, timeout_s=0.4).submit_order("agent:x", dict(order))
        assert "DAEMON_TIMEOUT" in r1["block_reasons"] and r1.get("executed") is None, r1
        r2 = DaemonGateway(cfg.state_dir, timeout_s=10).submit_order("agent:x", dict(order))
        assert r2["ok"] and r2["status"] == "duplicate" and r2["intent_id"] == "ag-retry-0001", r2
        assert r2["report"]["state"] in ("working", "done")
        time.sleep(0.5)
    assert sim.place_calls == 1
    with daemon_running(cfg, sim_factory(sim)):                         # restart: the ledger is on disk
        r3 = DaemonGateway(cfg.state_dir, timeout_s=10).submit_order("agent:x", dict(order))
        assert r3["status"] == "duplicate" and r3["report"]["state"] == "unknown", r3
        bad = DaemonGateway(cfg.state_dir, timeout_s=10).submit_order("agent:x", {**order, "client_intent_id": "x"})
        assert bad["block_reasons"][0].startswith("BAD_CLIENT_INTENT_ID")
        time.sleep(0.3)
    assert sim.place_calls == 1
    assert "ag-retry-0001" in (cfg.state_dir / "intent_ids.jsonl").read_text(encoding="utf-8")


def test_R3_3_duplicate_of_a_queued_intent_returns_its_approval(tmp_path):
    cfg = agent_daemon_cfg(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    order = {"coin": "ETH", "side": "buy", "size": 1.0, "algo": "market", "client_intent_id": "big-order-1"}
    with daemon_running(cfg, sim_factory(sim)):
        gw = DaemonGateway(cfg.state_dir, timeout_s=10)
        r1 = gw.submit_order("agent:x", dict(order))
        assert r1["status"] == "pending_approval" and r1["intent_id"] == "ag-big-order-1"
        r2 = gw.submit_order("agent:x", dict(order))
        assert r2["status"] == "duplicate" and r2["approval_id"] == r1["approval_id"]
        assert r2["approval_state"] == "pending"
    assert len(ApprovalQueue(cfg.state_dir, AuditLog(cfg.audit_path)).list()) == 1 and sim.place_calls == 0


def test_R3_3_mcp_tool_and_agents_doc_carry_client_intent_id():
    import inspect
    from pathlib import Path
    from omx import mcp_server
    src = inspect.getsource(mcp_server.build_server)
    assert "client_intent_id: Optional[str] = None" in src
    doc = (Path(mcp_server.__file__).resolve().parents[1] / "docs" / "AGENTS.md").read_text(encoding="utf-8")
    assert "client_intent_id" in doc and "reuse" in doc.lower()


# ====================================================================== R3-4 action expiry
def test_R3_4_every_action_is_signed_with_expires_after(tmp_path):
    eng, clk, t = hl_engine(tmp_path, exchange=[TimeoutError("read timed out")], order_expiry_ms=15_000)
    eng.step()
    eng.submit(Intent("hu-x", "BTC", Side.BUY, 0.01, algo=Algo.MARKET, max_slippage_bps=30.0))
    body = [b for path, b in t.calls if path == "/exchange"][0]
    assert body["expiresAfter"] == body["nonce"] + 15_000
    signer = recover_agent_or_user_from_l1_action(body["action"], body["signature"], None, body["nonce"],
                                                  body["expiresAfter"], False)
    assert signer == eng.venue.signer.address


def test_R3_4_ambiguous_order_is_resent_only_after_its_expiry_and_an_unknown_status(tmp_path):
    filled = ok_order({"filled": {"totalSz": "0.01", "avgPx": "123457.0", "oid": 9}})
    eng, clk, t = hl_engine(tmp_path, exchange=[TimeoutError("read timed out"), filled])
    eng.step()
    eng.submit(Intent("hu-x", "BTC", Side.BUY, 0.01, algo=Algo.MARKET, max_slippage_bps=30.0))
    first = [b for path, b in t.calls if path == "/exchange"][0]
    resent_at = []

    def watch():
        sent = [b for path, b in t.calls if path == "/exchange"]
        if len(sent) == 2 and not resent_at:
            resent_at.append(sent[1]["nonce"])
    run_steps(eng, clk, 30.0, each=watch)
    assert resent_at and resent_at[0] >= first["expiresAfter"] + EXPIRY_MARGIN_MS
    assert t.count("orderStatus") >= 2                                  # asked before resending
    assert eng.status("hu-x").state == "done"


def test_R3_4_ambiguous_order_found_by_status_is_reconciled_not_resent(tmp_path):
    st = {"status": "order", "order": {"order": {"oid": 9, "origSz": "0.01", "sz": "0"}, "status": "filled"}}
    eng, clk, t = hl_engine(tmp_path, info={"orderStatus": st}, exchange=[TimeoutError("read timed out")])
    eng.step()
    eng.submit(Intent("hu-y", "BTC", Side.BUY, 0.01, algo=Algo.MARKET, max_slippage_bps=30.0))
    run_steps(eng, clk, 30.0)
    assert sum(1 for path, _ in t.calls if path == "/exchange") == 1
    assert eng.status("hu-y").state == "done" and eng.status("hu-y").filled_size == pytest.approx(0.01)


@pytest.mark.parametrize("delay_ms,places", [(10_000, 2), (1_000, 1)])
def test_R3_4_sim_models_expiry_late_original_cannot_double(tmp_path, delay_ms, places):
    sim = SimVenue(SimConfig(seed=1, order_expiry_ms=3_000), coins=quiet())
    eng = Engine(sim, PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    sim.advance(100)
    eng.step()
    sim.delay_next_places(delay_ms)                    # 10 s: arrives after its expiry; 1 s: before
    eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    for _ in range(100):
        sim.advance(200)
        eng.step()
    assert sim.place_calls == places and sim.account().position("ETH") == pytest.approx(1.0)
    assert eng.status("m1").state == "done"
    assert any(e[1] == "expired_action" for e in sim.events) == (delay_ms > 3_000)


def test_R3_4_config_order_expiry_reaches_the_adapter(tmp_path):
    p = cfgfile_at(tmp_path, **{"order_expiry_ms = 15000": "order_expiry_ms = 7000"})
    make_eligible(tmp_path / "state")
    eng = from_config(load(p), signer=LocalKeySigner(Account.create()), transport=FakeTransport())
    assert eng.venue.order_expiry_ms == 7_000
    with pytest.raises(Exception):
        load(cfgfile_at(tmp_path, **{"order_expiry_ms = 15000": "order_expiry_ms = 500"}))


# ====================================================================== R3-5 two instances, one account
def test_R3_5_second_instance_does_not_sweep_the_first_instances_orders(tmp_path):
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    a = Engine(sim, PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "sa")
    b = Engine(sim, PolicyConfig(), AuditLog(tmp_path / "b.jsonl"), state_dir=tmp_path / "sb")
    sim.advance(100)
    a.step()
    b.step()
    assert a.submit(Intent("hu-a", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2980.0)).state == "working"
    for _ in range(65):
        sim.advance(1_000)
        a.step()
        b.step()
    live = [o for o in sim.account().open_orders if o.child_id == "hu-a.1"]
    assert a.status("hu-a").state == "working" and live
    assert cloid_instance(live[0].cloid) == a.instance != b.instance
    b.kill("stop everything")                                            # a kill is a kill: every OMX order
    sim.advance(1_000)
    assert sim.account().open_orders == ()


def test_R3_5_cloid_layout_keeps_the_omx_prefix_and_adds_the_instance_tag(tmp_path):
    tag = instance_tag(tmp_path / "s")
    c = cloid_for("hu-1.1", tag)
    assert c.startswith("0x4f4d5801" + tag) and len(c) == 34 and is_omx_cloid(c) and cloid_instance(c) == tag
    assert instance_tag(tmp_path / "s") == tag != instance_tag(tmp_path / "t")
    assert cloid_for("hu-1.1", tag) != cloid_for("hu-1.1", instance_tag(tmp_path / "t"))


def test_R3_5_second_daemon_on_the_same_account_refuses_to_start(tmp_path):
    cfgs = []
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        make_eligible(d / "state")
        cfgs.append(dataclasses.replace(load(cfgfile_at(d)), lock_dir=tmp_path / "locks"))
    with daemon_running(cfgs[0], lambda c: FakeEngine()):
        second = Daemon(cfgs[1], lambda c: FakeEngine())
        assert second.run() == 1 and "already trades this account" in str(second.error)
        assert not ipc.daemon_alive(cfgs[1].state_dir)
    assert account_lock_path(cfgs[0]) == account_lock_path(cfgs[1])
    assert account_lock_path(cfgs[0]).parent == tmp_path / "locks"
    with daemon_running(cfgs[1], lambda c: FakeEngine()):               # released at the first's stop
        assert ipc.call(cfgs[1].state_dir, "ping", {})["pid"]


# ====================================================================== R3-6 spot / other-dex orders
SPOT = {"coin": "@107", "side": "B", "sz": "10", "limitPx": "1.0", "oid": 9, "cloid": None, "reduceOnly": False}
NAMED = {"coin": "PURR/USDC", "side": "S", "sz": "5", "limitPx": "0.2", "oid": 10, "cloid": None, "reduceOnly": False}
OTHER_DEX = {"coin": "xyz:ABC", "side": "B", "sz": "1", "limitPx": "5", "oid": 11, "cloid": None, "reduceOnly": False}
NOT_IN_META = {"coin": "FOO", "side": "B", "sz": "1", "limitPx": "5", "oid": 12, "cloid": None, "reduceOnly": False}


def test_R3_6_spot_other_dex_and_unlisted_orders_do_not_block_perp_orders(tmp_path):
    eng, clk, t = hl_engine(tmp_path, info={"frontendOpenOrders": [SPOT, NAMED, OTHER_DEX, NOT_IN_META]},
                            exchange=[ok_order({"resting": {"oid": 77}})])
    eng.step()
    rep = eng.submit(Intent("hu-p", "BTC", Side.BUY, 0.01, algo=Algo.LIMIT, limit_px=123400.0))
    assert rep.state == "working", rep.block_reasons
    d = recs(tmp_path / "a.jsonl", "child_decision")[-1]["data"]
    assert d["checks"]["coins_without_mark"] == []
    view = eng.account_view()
    assert {o["coin"] for o in view["non_perp_open_orders"]} == {"@107", "PURR/USDC", "xyz:ABC", "FOO"}


def test_R3_6_kill_all_cancels_spot_orders_and_skips_unknown_assets_without_a_meta_loop(tmp_path):
    spot_meta = {"universe": [{"name": "PURR/USDC", "index": 0, "tokens": [1, 0]}], "tokens": []}
    eng, clk, t = hl_engine(tmp_path, info={"frontendOpenOrders": [SPOT, NAMED, OTHER_DEX], "spotMeta": spot_meta},
                            exchange=[CANCEL_OK, CANCEL_OK])
    eng.step()
    metas = t.count("metaAndAssetCtxs")
    rec = eng.kill("everything", scope="all")
    sent = [b["action"] for path, b in t.calls if path == "/exchange"]
    assert [a["cancels"][0]["a"] for a in sent] == [10_107, 10_000]     # 10000 + spot index
    assert not rec.get("cancel_errors") and rec["cancels_requested"] == 2 and rec["cancels_skipped"] == 1
    assert recs(tmp_path / "a.jsonl", "cancel_skipped")[0]["data"]["coin"] == "xyz:ABC"
    t.info["frontendOpenOrders"] = [OTHER_DEX]
    run_steps(eng, clk, 60.0)                                           # killed loop keeps sweeping
    assert sum(1 for path, _ in t.calls if path == "/exchange") == 2    # skipped once, never retried
    assert t.count("metaAndAssetCtxs") - metas <= 3                     # marks schedule only, no refetch loop
    assert len(recs(tmp_path / "a.jsonl", "cancel_skipped")) == 1
    assert "could not be cancelled" in _sweep_warning(eng.kill("again", scope="all"))


def test_R3_6_unknown_coin_does_not_refetch_meta_on_every_call(tmp_path):
    eng, clk, t = hl_engine(tmp_path)
    eng.step()
    n = t.count("metaAndAssetCtxs")
    for _ in range(5):
        with pytest.raises(hlmod.VenueUnknownAsset):
            eng.venue.cancel("NOPE", "1")
    assert t.count("metaAndAssetCtxs") == n + 1


# ====================================================================== R3-7 daemon kill reports a failed sweep
class SweepFails(FakeEngine):
    def kill(self, reason, scope="omx"):
        super().kill(reason, scope)
        return {"killed": True, "scope": scope, "venue_sweep_error": "VenueError('HTTP 502')", "cancels_requested": 0}


class SweepSkips(FakeEngine):
    def kill(self, reason, scope="omx"):
        super().kill(reason, scope)
        return {"killed": True, "scope": scope, "cancels_requested": 1, "cancels_skipped": 1}


@pytest.mark.parametrize("cls,word", [(SweepFails, "FAILED"), (SweepSkips, "could not be cancelled")])
def test_R3_7_daemon_kill_with_failed_sweep_exits_3_with_a_warning(tmp_path, cls, word):
    cfg = agent_daemon_cfg(tmp_path)
    with daemon_running(cfg, lambda c: cls()):
        code, out, err = cli(cfg.source_path, "kill", "--reason", "crash")
    assert code == 3 and "WARNING" in out and word in out, out
    with daemon_running(cfg, lambda c: FakeEngine()):
        code, out, err = cli(cfg.source_path, "kill", "--reason", "fine")
    assert code == 0 and "WARNING" not in out


# ====================================================================== R3-8 status after a restart
def test_R3_8_status_after_restart_is_unknown_with_a_hint_never_rejected(tmp_path):
    cfg = agent_daemon_cfg(tmp_path)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):
        code, out, err = cli(cfg.source_path, "submit", "ETH", "buy", "0.1", "--algo", "market", "--yes")
        assert code == 0, err + out
        iid = json.loads(out[: out.index("\nBuilder fee")])["intent"]["intent_id"]
        wait_for(lambda: json.loads(cli(cfg.source_path, "status", iid)[1])["state"] == "done")
    with daemon_running(cfg, sim_factory(sim)):
        code, out, err = cli(cfg.source_path, "status", iid)
        code2, out2, _ = cli(cfg.source_path, "status", "hu-neverseen")
    st = json.loads(out)
    assert code == 0 and st["state"] == "unknown" and "earlier daemon run" in st["message"]
    assert "audit log" in st["message"]
    st2 = json.loads(out2)
    assert st2["state"] == "unknown" and "earlier daemon run" not in st2["message"]


# ====================================================================== R3-9 weight plan
def test_R3_9_snapshot_uses_cached_meta(tmp_path):
    eng, clk, t = hl_engine(tmp_path)
    eng.step()
    n = t.count("metaAndAssetCtxs")
    for _ in range(10):
        clk.t += 6.0
        eng.venue.snapshot("BTC")
    assert t.count("metaAndAssetCtxs") == n and t.count("l2Book") >= 10


def per_min(t):
    return sum(WEIGHT[b["type"]] for path, b in t.calls if path == "/info")


def test_R3_9_one_resting_order_stays_inside_70pct(tmp_path):
    st = {"placed": False}
    resting = {"coin": "BTC", "side": "B", "sz": "0.01", "limitPx": "123400.0", "oid": 77, "cloid": None,
               "reduceOnly": False}

    def place(p):
        st["placed"] = True
        resting["cloid"] = p["action"]["orders"][0]["c"]
        return ok_order({"resting": {"oid": 77}})
    eng, clk, t = hl_engine(tmp_path, info={"frontendOpenOrders": lambda p: [resting] if st["placed"] else []},
                            exchange=[place])
    eng.step()
    assert eng.submit(Intent("hu-w", "BTC", Side.BUY, 0.01, algo=Algo.LIMIT, limit_px=123400.0)).state == "working"
    run_steps(eng, clk, 10.0)
    t.calls.clear()
    run_steps(eng, clk, 60.0)
    assert eng.status("hu-w").state == "working"
    assert per_min(t) <= 0.7 * 1200, {k: t.count(k) for k in WEIGHT}
    assert t.count("metaAndAssetCtxs") <= 3


COINS7 = ["BTC", "ETH", "SOL", "AAA", "BBB", "CCC", "DDD"]


def test_R3_9_seven_working_coins_fit_the_plan_and_books_stay_fresh(tmp_path):
    meta = [{"universe": [{"name": c, "szDecimals": 4, "maxLeverage": 10} for c in COINS7]},
            [{"dayNtlVlm": "1000000000", "markPx": "100.0"} for _ in COINS7]]
    placed = []

    def place(p):
        w = p["action"]["orders"][0]
        placed.append({"coin": COINS7[w["a"]], "side": "B", "sz": w["s"], "limitPx": w["p"], "oid": 100 + len(placed),
                       "cloid": w["c"], "reduceOnly": False})
        return ok_order({"resting": {"oid": placed[-1]["oid"]}})
    clock = {}
    eng, clk, t = hl_engine(tmp_path, info={
        "metaAndAssetCtxs": meta, "frontendOpenOrders": lambda p: list(placed),
        "l2Book": lambda p: {"coin": p["coin"], "time": clock["now"]() - 100,
                             "levels": [[{"px": "99.99", "sz": "1000", "n": 1}], [{"px": "100.01", "sz": "1000", "n": 1}]]}},
        exchange=[place] * 7, policy=PolicyConfig(allowed_coins=frozenset(COINS7), min_day_ntl_usd=0.0,
                                                  min_depth_usd=0.0))
    clock["now"] = eng.venue.clock_ms
    eng.step()
    for c in COINS7:
        rep = eng.submit(Intent(f"hu-{c.lower()}", c, Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=99.5))
        assert rep.state == "working", (c, rep.block_reasons)
    run_steps(eng, clk, 10.0)
    t.calls.clear()
    run_steps(eng, clk, 60.0)
    assert all(eng.status(f"hu-{c.lower()}").state == "working" for c in COINS7)
    assert per_min(t) <= 0.7 * 1200, {k: t.count(k) for k in WEIGHT}
    ps = PollSchedule()
    assert ps.book_scale(7) > 1.0 and ps.book_ms * ps.book_scale(7) < 3_000 - 200    # books still fresh
    assert ps.planned(True, 7, ps.scale(True, 7, 7), 7) <= ps.budget_per_min + 1e-6
    assert 7 * 2 * 60_000 / (ps.book_ms * ps.book_scale(7)) <= ps.budget_per_min * ps.book_share + 1e-6


def test_R3_9_plan_counts_every_read_kind():
    ps = PollSchedule()
    base = ps.planned(True, 1, 1.0, 0)
    assert ps.planned(True, 1, 1.0, 3) > base                           # orderStatus per open child
    assert ps.planned(False) == 582                                     # early ledger reads at their worst case; + mode reads
    assert ps.planned(False, unified=True) == 642                       # unified: spotClearinghouseState with every account read


# ====================================================================== R3-10 console confirmation text
def test_R3_10_approve_prompt_shows_the_order_and_the_cli_request_id(tmp_path):
    cfg = agent_daemon_cfg(tmp_path)
    q = ApprovalQueue(cfg.state_dir, AuditLog(cfg.audit_path))
    aid = q.enqueue(make_intent(coin="ETH", side="buy", size=1.25, algo="market", source="agent:x"),
                    ["ABOVE_APPROVAL_THRESHOLD"], 3750.0)["approval_id"]
    console = confirming_console()
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim), console=console):
        code, out, err = cli(cfg.source_path, "approvals", "approve", aid, "--yes")
    assert code == 0, err + out
    prompt = next(s for s in console.shown if "CONFIRMATION NEEDED" in s)
    for word in ("ETH", "1.25", "BUY", "market", "3,750.00 USD", "agent:x", aid):
        assert word in prompt, (word, prompt)
    rid = out.split("Request id ")[1].split()[0]
    assert ipc.REQ_ID.match(rid) and rid in prompt
    code_shown = prompt.split("confirm ")[1].split("`")[0]
    assert code_shown not in out                                        # the code is only in the daemon console


def test_R3_10_unkill_prompt_shows_the_kill_reason_and_scope(tmp_path):
    cfg = agent_daemon_cfg(tmp_path)
    kill_from_outside(cfg.state_dir, "human:cli: funding spike", "all")
    console = confirming_console()
    with daemon_running(cfg, lambda c: FakeEngine(), console=console):
        code, out, err = cli(cfg.source_path, "unkill", answers=["I UNDERSTAND"])
    assert code == 0, err + out
    prompt = next(s for s in console.shown if "CONFIRMATION NEEDED" in s)
    assert "funding spike" in prompt and "scope all" in prompt and "CLEAR the kill switch" in prompt


# ====================================================================== R3-11 interactive console detection
@pytest.mark.skipif(os.name != "nt", reason="NUL is a character device only on Windows")
def test_R3_11_nul_stdin_is_not_an_interactive_console(monkeypatch):
    with open(os.devnull, "r") as nul:
        assert nul.isatty() and not is_console(nul)                    # isatty() alone would say yes
        monkeypatch.setattr(sys, "stdin", nul)
        assert interactive_console(io.StringIO()) is None


def test_R3_11_pipes_files_and_string_streams_are_not_consoles(tmp_path):
    r, w = os.pipe()
    with os.fdopen(r, "r") as fr, os.fdopen(w, "w"):
        assert not is_console(fr)
    with open(tmp_path / "f.txt", "w+") as f:
        assert not is_console(f)
    assert not is_console(io.StringIO()) and not is_console(None)


@pytest.mark.skipif(os.name != "nt", reason="NUL is a character device only on Windows")
def test_R3_11_daemon_with_nul_stdin_refuses_human_ops_immediately(tmp_path, monkeypatch):
    cfg = agent_daemon_cfg(tmp_path)
    q = ApprovalQueue(cfg.state_dir, AuditLog(cfg.audit_path))
    aid = q.enqueue(make_intent(coin="ETH", side="buy", size=1.0, algo="market", source="agent:x"),
                    ["ABOVE_APPROVAL_THRESHOLD"], 3000.0)["approval_id"]
    with open(os.devnull, "r") as nul:
        monkeypatch.setattr(sys, "stdin", nul)
        with daemon_running(cfg, lambda c: FakeEngine(), console="auto") as d:
            assert d.console is None
            t0 = time.monotonic()
            code, out, err = cli(cfg.source_path, "approvals", "reject", aid)
    assert code == 1 and "HUMAN_CONSOLE_REQUIRED" in err and time.monotonic() - t0 < 5.0
