"""Fix round R2: regressions for the second review (_review/test_review_findings_r2.py, B1..B5)
plus the lower items. Offline: SimVenue / fake HL transport, throwaway keys only."""
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from eth_account import Account

from omx import cli as cli_mod
from omx import ipc
from omx.approvals import ApprovalQueue, make_intent
from omx.audit import AuditLog
from omx.cli import main as cli_main
from omx.config import load, render
from omx.daemon import Daemon, QueueConsole
from omx.eligibility import Attestation, EligibilityError, record_attempt
from omx.engine import Engine, PollSchedule, from_config
from omx.killswitch import KillSwitch, kill_from_outside
from omx.mcp_server import DaemonGateway
from omx.policy import PolicyConfig
from omx.types import Algo, ChildOrder, Intent, Side
from omx.venue.base import cloid_for, instance_tag
from omx.venue.hyperliquid import TESTNET_API_URL, HyperliquidVenue, LocalKeySigner, TokenBucket
from omx.venue.sim import SimConfig, SimVenue

from test_daemon import quiet, sim_factory, wait_for
from test_hyperliquid import NOW, USER, FakeClock, FakeTransport
from test_mcp import ACCOUNT, FakeEngine, confirming_console, daemon_running, make_eligible

KR = Attestation("KR", citizenships=("KR",), accepts_terms=True, current_country="KR")
US = Attestation("US", citizenships=("US",), accepts_terms=True, current_country="US")
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


def submit(p, *args):
    code, out, err = cli(p, "submit", *args, "--yes")
    assert code == 0, err + out
    return json.loads(out[: out.index("\nBuilder fee")])["intent"]["intent_id"]


def status(p, iid):
    code, out, err = cli(p, "status", iid)
    assert code == 0, err
    return json.loads(out)


def kinds(cfg):
    return [json.loads(line)["kind"] for line in Path(cfg.audit_path).read_text().splitlines()]


def drop_request(state_dir, op, args, client="human:cli", rid=None, deadline_s=10.0):
    """What any same-user program can do: write a request file by hand."""
    rid = rid or ipc.new_id()
    req = {"id": rid, "op": op, "args": args, "client": client, "created_ms": int(time.time() * 1000),
           "deadline_ms": int((time.time() + deadline_s) * 1000)}
    ipc.write_atomic(ipc.dirs(state_dir)["inbox"] / f"{rid}.req", json.dumps(req))
    return rid, req


def response(state_dir, rid):
    p = ipc.dirs(state_dir)["outbox"] / f"{rid}.resp"
    return wait_for(lambda: p.exists() and json.loads(p.read_text(encoding="utf-8")))


def queue_agent_approval(cfg):
    q = ApprovalQueue(cfg.state_dir, AuditLog(cfg.audit_path))
    return q, q.enqueue(make_intent(coin="ETH", side="buy", size=1.0, algo="market", source="agent:x"),
                        ["ABOVE_APPROVAL_THRESHOLD"], 3000.0)["approval_id"]


# =============================================================================== B1
def hl_kill_factory(open_orders, exchange):
    """from_config with an injected throwaway signer and fake transport (no key file, no network)."""
    t = FakeTransport(info={"clearinghouseState": {"assetPositions": [], "marginSummary": {"accountValue": "1000"},
                                                   "time": NOW},
                            "frontendOpenOrders": open_orders, "userFillsByTime": [],
                            "userNonFundingLedgerUpdates": []}, exchange=exchange)

    def factory(c, purpose="trade"):
        return from_config(c, signer=LocalKeySigner(Account.create()), transport=t, purpose=purpose)
    return factory, t


def test_B1_expired_attestation_kill_without_daemon_cancels(tmp_path):
    p = cfgfile_at(tmp_path)
    cfg = load(p)
    record_attempt(cfg.state_dir, KR, True, [], now_s=time.time() - 31 * 86_400)     # attested 31 days ago
    omx_order = {"coin": "ETH", "side": "B", "sz": "1.0", "limitPx": "1800", "oid": 5,
                 "cloid": cloid_for("hu-old.1"), "reduceOnly": False}
    factory, t = hl_kill_factory([omx_order], [CANCEL_OK])
    with pytest.raises(EligibilityError):
        factory(cfg)                                                  # trading engines stay gated
    code, out, err = cli(p, "kill", "--reason", "market crash", factory=factory)
    assert code == 0 and "NOT cancelled" not in out and "WARNING" not in out, out + err
    cancels = [b["action"] for path, b in t.calls if path == "/exchange"]
    assert cancels == [{"type": "cancel", "cancels": [{"a": 1, "o": 5}]}]
    assert json.loads(out)["one_shot_engine"]["cancels_requested"] == 1


def test_B1_cancel_only_engine_refuses_new_orders(tmp_path):
    p = cfgfile_at(tmp_path)
    cfg = load(p)
    record_attempt(cfg.state_dir, US, False, [], now_s=time.time())                 # blocked user
    factory, t = hl_kill_factory([], [])
    eng = factory(cfg, purpose="kill")
    assert eng.cancel_only
    rep = eng.submit(Intent("hu-x", "ETH", Side.BUY, 0.1))
    assert rep.state == "rejected" and "CANCEL_ONLY" in rep.block_reasons
    with pytest.raises(ValueError, match="CANCEL_ONLY"):
        eng.preview(Intent("hu-y", "ETH", Side.BUY, 0.1))
    assert not [c for c in t.calls if c[0] == "/exchange"]


def test_B1_cancel_only_daemon_starts_without_eligibility_and_sweeps(tmp_path):
    p = cfgfile_at(tmp_path)
    cfg = load(p)
    record_attempt(cfg.state_dir, KR, True, [], now_s=time.time() - 31 * 86_400)     # expired
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    sim.place(ChildOrder("hu-old.1", "hu-old", "ETH", Side.BUY, 1.0, 2990.0, "Gtc"))
    sim.advance(500)
    assert sim.account().open_orders
    with pytest.raises(Exception, match="ELIGIBILITY"):
        with daemon_running(cfg, sim_factory(sim)):                  # a normal daemon stays gated
            pass
    with daemon_running(cfg, sim_factory(sim), cancel_only=True):
        wait_for(lambda: sim.account().open_orders == ())
        code, _, err = cli(p, "submit", "ETH", "buy", "1.0", "--algo", "market", "--yes")
        assert code == 1 and "ELIGIBILITY_EXPIRED" in err                 # the CLI refuses first
        with pytest.raises(ipc.IPCError, match="CANCEL_ONLY"):            # a direct request: the daemon refuses
            ipc.call(cfg.state_dir, "submit", {"order": {"coin": "ETH", "side": "buy", "size": 1.0,
                                                         "algo": "market"}})
        r = DaemonGateway(cfg.state_dir).submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 0.1,
                                                                  "algo": "market"})
        assert not r["ok"] and "CANCEL_ONLY" in r["block_reasons"][0]
    assert "cancel_only" in kinds(cfg)


def test_B1_running_daemon_loses_eligibility_and_stops_trading(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim), elig_check_s=0.1) as d:
        iid = submit(p, "ETH", "buy", "1.0", "--algo", "limit", "--limit-px", "2990")
        wait_for(lambda: status(p, iid)["children_sent"] == 1)
        record_attempt(cfg.state_dir, US, False, [], now_s=time.time())             # moved to the US
        wait_for(lambda: d.cancel_only)
        wait_for(lambda: status(p, iid)["state"] == "cancelled")
        wait_for(lambda: sim.account().open_orders == ())
        code, _, err = cli(p, "submit", "ETH", "buy", "1.0", "--algo", "market", "--yes")
        assert code == 1 and ("CANCEL_ONLY" in err or "ELIGIBILITY" in err)
    lost = [json.loads(line) for line in cfg.audit_path.read_text().splitlines()
            if json.loads(line)["kind"] == "eligibility_lost"]
    assert lost and lost[0]["data"]["reason"].startswith("ELIGIBILITY")
    assert sim.account().position("ETH") == 0.0


# =============================================================================== B2
HUMAN_OPS = (("approvals.reject", {"approval_id": "ap-000000000000"}), ("unkill", {"confirm": "I UNDERSTAND"}),
             ("eligibility.record", {"attestation": {}}), ("audit.repair", {}))


def test_B2_forged_human_ops_refused_without_console(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):                       # no interactive console
        gw = DaemonGateway(cfg.state_dir)
        r = gw.submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 1.0, "algo": "market"})
        aid = r["approval_id"]
        rid, _ = drop_request(cfg.state_dir, "approvals.approve", {"approval_id": aid})
        assert response(cfg.state_dir, rid)["error"].startswith("HUMAN_CONSOLE_REQUIRED")
        assert gw.approval_status(aid)["state"] == "pending"
        for op, args in HUMAN_OPS:
            rid, _ = drop_request(cfg.state_dir, op, args)
            assert response(cfg.state_dir, rid)["error"].startswith("HUMAN_CONSOLE_REQUIRED"), op
        # agent-role ops keep working over IPC
        small = gw.submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market"})
        assert small["ok"] and small["status"] == "submitted", small
        assert gw.account_summary()["ok"]
    assert sim.place_calls == 1


def test_B2_human_op_runs_only_after_exact_console_confirmation(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    _, aid = queue_agent_approval(cfg)
    console = QueueConsole()
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng, console=console):
        gw = DaemonGateway(cfg.state_dir)
        rid, _ = drop_request(cfg.state_dir, "approvals.approve", {"approval_id": aid})
        wait_for(lambda: console.shown)
        code = console.shown[-1].split("[")[1].split("]")[0]
        assert f"confirm {code}" in console.shown[-1] and aid in console.shown[-1]
        time.sleep(0.2)
        assert gw.approval_status(aid)["state"] == "pending" and eng.submitted == []   # waiting, not run
        console.feed("confirm 000000")                                 # wrong code
        console.feed("yes")                                            # not the exact line
        time.sleep(0.2)
        assert gw.approval_status(aid)["state"] == "pending"
        console.feed(f"deny {code}")
        assert response(cfg.state_dir, rid)["error"].startswith("DENIED_AT_CONSOLE")
        assert gw.approval_status(aid)["state"] == "pending" and eng.submitted == []
        rid, _ = drop_request(cfg.state_dir, "approvals.approve", {"approval_id": aid})
        wait_for(lambda: len([s for s in console.shown if "CONFIRMATION NEEDED" in s]) == 2)
        code = [s for s in console.shown if "CONFIRMATION NEEDED" in s][-1].split("[")[1].split("]")[0]
        console.feed(f"confirm {code}\n")
        assert response(cfg.state_dir, rid)["ok"]
        assert gw.approval_status(aid)["state"] == "executed" and len(eng.submitted) == 1
        rid, _ = drop_request(cfg.state_dir, "approvals.approve", {"approval_id": aid}, deadline_s=0.3)
        assert response(cfg.state_dir, rid)["error"].startswith("EXPIRED")     # never confirmed
    k = kinds(cfg)
    assert k.count("human_op_pending") == 3 and k.count("human_op_denied") == 1 and k.count("human_op_confirmed") == 1


def test_B2_replayed_request_id_refused_across_restarts(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng):
        rid, req = drop_request(cfg.state_dir, "kill", {"reason": "once"})
        assert response(cfg.state_dir, rid)["ok"]
        (ipc.dirs(cfg.state_dir)["outbox"] / f"{rid}.resp").unlink()
        ipc.write_atomic(ipc.dirs(cfg.state_dir)["inbox"] / f"{rid}.req", json.dumps(req))     # replay
        assert response(cfg.state_dir, rid)["error"].startswith("REPLAYED")
        rid2, _ = drop_request(cfg.state_dir, "ping", {}, deadline_s=ipc.MAX_DEADLINE_S + 60)
        assert response(cfg.state_dir, rid2)["error"].startswith("BAD_REQUEST: deadline")
    (ipc.dirs(cfg.state_dir)["outbox"] / f"{rid}.resp").unlink()
    with daemon_running(cfg, lambda c: eng):                          # a new daemon remembers the id
        ipc.write_atomic(ipc.dirs(cfg.state_dir)["inbox"] / f"{rid}.req", json.dumps(req))
        assert response(cfg.state_dir, rid)["error"].startswith("REPLAYED")
    assert eng.kills == ["once"]
    assert (Path(cfg.state_dir) / "ipc" / ipc.PROCESSED).exists()


def test_B2_id_ledger_is_bounded_and_drops_expired(tmp_path):
    clock = [1000.0]
    led = ipc.IdLedger(tmp_path / "ids.jsonl", max_n=5, clock=lambda: clock[0])
    for i in range(12):
        led.add(f"r{i}", until_ms=int((clock[0] + 10) * 1000))
    assert len(led.ids) <= 10 and "r11" in led
    clock[0] += 20
    led2 = ipc.IdLedger(tmp_path / "ids.jsonl", max_n=5, clock=lambda: clock[0])
    assert not led2.ids and (tmp_path / "ids.jsonl").read_text() == ""


# =============================================================================== B3
def hl_engine_fake_clock(tmp_path, **kw):
    clk = FakeClock()
    cs = {"assetPositions": [], "marginSummary": {"accountValue": "10000"}, "time": NOW}
    t = FakeTransport(info={"clearinghouseState": cs, "frontendOpenOrders": [], "userFillsByTime": [],
                            "userNonFundingLedgerUpdates": []})
    v = HyperliquidVenue(TESTNET_API_URL, USER, LocalKeySigner(Account.create()), transport=t,
                         limiter=TokenBucket(clock=clk, sleep=clk.sleep),
                         clock_ms=lambda: NOW + round((clk.t - 1000) * 1000), sleep=clk.sleep)
    return Engine(v, PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s", **kw), clk, t


WEIGHT = {"clearinghouseState": 2, "frontendOpenOrders": 20, "userFillsByTime": 20,
          "userNonFundingLedgerUpdates": 20, "metaAndAssetCtxs": 20, "l2Book": 2}


def test_B3_review_scenario_one_loop_plus_account_summary_is_fast(tmp_path):
    eng, clk, _ = hl_engine_fake_clock(tmp_path)
    for _ in range(30):
        eng.step()
    t0 = clk.t
    eng.step()
    eng.account_view()
    assert clk.t - t0 <= ipc.LIVE_S


def test_B3_idle_weight_per_minute_with_fake_clock(tmp_path):
    eng, clk, t = hl_engine_fake_clock(tmp_path)
    eng.step()                                                         # first read: everything once
    t.calls.clear()
    for _ in range(300):                                               # one minute of 200 ms steps
        clk.t += 0.2
        eng.step()
        eng.account_view()                                             # an agent account_summary each loop
    per_min = sum(WEIGHT[b["type"]] for path, b in t.calls if path == "/info")
    counts = {k: t.count(k) for k in WEIGHT}
    assert counts == {"clearinghouseState": 30, "frontendOpenOrders": 6, "userFillsByTime": 12,
                      "userNonFundingLedgerUpdates": 2, "metaAndAssetCtxs": 2, "l2Book": 0}, counts
    # R3-9: the plan is a worst-case bound that also counts early ledger reads (equity rises), here none
    assert per_min == 500 <= PollSchedule().planned(False) == 582            # 580 + userAbstraction every 10 min (+2)
    assert per_min <= 0.7 * 1200 and clk.slept == []


def test_B3_working_plan_is_stretched_into_the_budget():
    ps = PollSchedule()
    raw = ps.planned(True, coins=1)
    assert raw > 1200                                                  # the nominal working rates do not fit
    s = ps.scale(True, coins=1)
    assert s > 1 and ps.planned(True, coins=1, scale=s) <= ps.budget_per_min + 1e-6
    assert ps.scale(False) == 1.0


def test_B3_deposit_between_ledger_reads_never_raises_day_pnl(tmp_path):
    """Equity is read every 2 s, the ledger every 30 s: a deposit in between must not mask a loss."""
    clk = FakeClock()
    st = {"eq": "10000", "ledger": []}
    t = FakeTransport(info={"clearinghouseState": lambda p: {"assetPositions": [], "time": NOW,
                                                             "marginSummary": {"accountValue": st["eq"]}},
                            "frontendOpenOrders": [], "userFillsByTime": [],
                            "userNonFundingLedgerUpdates": lambda p: list(st["ledger"])})
    v = HyperliquidVenue(TESTNET_API_URL, USER, LocalKeySigner(Account.create()), transport=t,
                         limiter=TokenBucket(clock=clk, sleep=clk.sleep),
                         clock_ms=lambda: NOW + round((clk.t - 1000) * 1000), sleep=clk.sleep)
    eng = Engine(v, PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    eng.step()                                                         # day start 10000, ledger read
    st["eq"] = "10400"                                                 # lost 600, deposited 1000
    st["ledger"] = [{"time": NOW + 1500, "hash": "0x1", "delta": {"type": "deposit", "usdc": "1000.0"}}]
    pnls = []
    for _ in range(60):                                                # 12 s of 200 ms steps
        clk.t += 0.2
        eng.step()
        pnls.append(eng._acct.day_pnl_usd())
    assert max(pnls) <= 0.0, "the deposit showed up as trading profit before the ledger was read"
    assert pnls[-1] == pytest.approx(-600.0)                           # early ledger read within 10 s
    assert t.count("userNonFundingLedgerUpdates") == 2


def test_B3_slow_loop_is_busy_not_down(tmp_path, monkeypatch):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    monkeypatch.setattr(ipc, "BUSY_S", 0.5)
    gate = threading.Event()

    class Slow(FakeEngine):
        def step(self):
            if not gate.is_set():
                gate.wait(5)                                           # the venue makes one loop slow

    with daemon_running(cfg, lambda c: Slow()) as d:
        wait_for(lambda: ipc.daemon_state(cfg.state_dir)[0] == "busy", timeout_s=5)
        assert ipc.daemon_alive(cfg.state_dir)
        hb = ipc.read_heartbeat(cfg.state_dir)
        assert time.time() - hb["ts_s"] < 2.5 and time.time() * 1000 - hb["loop_progress_ms"] > 400
        gate.set()
        wait_for(lambda: ipc.daemon_state(cfg.state_dir)[0] == "running")
        assert ipc.call(cfg.state_dir, "ping", {})["loops"] >= 1


# =============================================================================== B4
def test_B4_late_omx_order_swept_by_periodic_orphan_sweep(tmp_path):
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    eng = Engine(sim, PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    sim.advance(100)
    eng.step()                                                         # start: sweep finds nothing
    own = eng.submit(Intent("hu-mine", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT,
                            limit_px=2980.0))
    sim.place(ChildOrder("hu-previousrun.3", "hu-previousrun", "ETH", Side.BUY, 1.0, 2990.0, "Gtc",
                         cloid=cloid_for("hu-previousrun.3", instance_tag(tmp_path / "s"))))   # same instance
    for _ in range(30):
        sim.advance(1_000)
        eng.step()
    assert any(o.child_id == "hu-previousrun.3" for o in sim.account().open_orders)   # not before 60 s
    for _ in range(32):
        sim.advance(1_000)
        eng.step()
    left = [o.child_id for o in sim.account().open_orders]
    assert "hu-previousrun.3" not in left, "late OMX order from the previous run left resting"
    assert left == [f"{own.intent_id}.1"] and eng.status("hu-mine").state == "working"   # own order kept
    recs = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert [r["data"]["phase"] for r in recs if r["kind"] == "orphans_cancelled"] == ["periodic"]


# =============================================================================== B5
def test_B5_kill_all_scope_survives_failed_one_shot(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    sim.place_foreign(ChildOrder("manual.1", "manual", "ETH", Side.BUY, 1.0, 2990.0, "Gtc"))
    sim.advance(500)
    code, out, _ = cli(p, "kill", "--all", "--reason", "everything", factory=lambda c: None)
    assert code == 3 and json.loads(out[: out.rindex("}") + 1])["kill_file"]["scope"] == "all"
    assert KillSwitch(cfg.state_dir).scope() == "all"
    code, out, _ = cli(p, "kill", "--reason", "again", factory=lambda c: None)       # never narrows
    assert KillSwitch(cfg.state_dir).scope() == "all"
    with daemon_running(cfg, sim_factory(sim)):
        time.sleep(0.5)
    sim.advance(2_000)
    assert sim.account().open_orders == (), "kill --all left the manual order resting"


def test_B5_running_engine_widens_to_stored_all(tmp_path):
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    state = tmp_path / "s"
    eng = Engine(sim, PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=state)
    sim.place_foreign(ChildOrder("manual.1", "manual", "ETH", Side.BUY, 1.0, 2990.0, "Gtc"))
    sim.advance(500)
    eng.kill("agent kill")                                             # scope omx: manual order stays
    sim.advance(500)
    eng.step()
    assert sim.account().open_orders
    kill_from_outside(state, "human: everything", "all")
    eng.step()
    sim.advance(1_000)
    assert sim.account().open_orders == ()


# =============================================================================== lower items
def test_one_shot_kill_with_failed_sweep_exits_nonzero(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    sim = SimVenue(SimConfig(seed=1), coins=quiet())

    class Down:
        def __getattr__(self, k):
            return getattr(sim, k)

        def account(self):
            raise ConnectionError("venue unreachable")

    def factory(c, purpose="trade"):
        return Engine(Down(), c.policy, AuditLog(c.audit_path), state_dir=c.state_dir, cancel_only=purpose == "kill")
    code, out, _ = cli(p, "kill", "--reason", "x", factory=factory)
    assert code == 3 and "WARNING" in out and "venue sweep FAILED" in out and "venue unreachable" in out


def test_approval_stuck_after_crash_is_unknown_and_never_reexecuted(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    q, aid = queue_agent_approval(cfg)
    q.approve(aid)                                    # the old daemon died right here, before the result
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng, console=confirming_console()):
        st = DaemonGateway(cfg.state_dir).approval_status(aid)
        assert st["state"] == "unknown_after_crash" and "outcome unknown" in st["result"]["error"]
        code, _, err = cli(p, "approvals", "approve", aid, "--yes")
        assert code == 1 and "unknown_after_crash" in err
    assert eng.submitted == [] and "approval_unknown_after_crash" in kinds(cfg)


def test_duplicate_intent_id_refused_even_after_restart(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng):
        assert submit(p, "ETH", "buy", "1", "--intent-id", "hu-retry1") == "hu-retry1"
        code, _, err = cli(p, "submit", "ETH", "buy", "1", "--yes", "--intent-id", "hu-retry1")
        assert code == 1 and "DUPLICATE_INTENT_ID" in err
    with daemon_running(cfg, lambda c: FakeEngine()):
        code, _, err = cli(p, "submit", "ETH", "buy", "1", "--yes", "--intent-id", "hu-retry1")
        assert code == 1 and "DUPLICATE_INTENT_ID" in err
    assert [i.intent_id for i in eng.submitted] == ["hu-retry1"]


def test_submit_timeout_prints_intent_id_and_status_hint(tmp_path, monkeypatch):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    cfg = load(p)
    monkeypatch.setattr(cli_mod, "TIMEOUT_S", 0.5)
    stop = threading.Event()

    def half_daemon():                                # answers preview, never claims submit
        d = ipc.dirs(cfg.state_dir)
        while not stop.is_set():
            ipc.write_heartbeat(cfg.state_dir, {"pid": os.getpid(), "loop_progress_ms": int(time.time() * 1000)})
            for f in d["inbox"].glob("*.req"):
                try:
                    req = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if req["op"] == "preview":
                    os.replace(f, d["work"] / f.name)
                    ipc.respond(cfg.state_dir, req["id"], True, {"ok": True})
            time.sleep(0.02)
    th = threading.Thread(target=half_daemon, daemon=True)
    th.start()
    deadline = time.time() + 10                       # slow CI runners: wait for the first heartbeat
    while not ipc.daemon_alive(cfg.state_dir) and time.time() < deadline:
        time.sleep(0.01)
    try:
        code, _, err = cli(p, "submit", "ETH", "buy", "1", "--yes", "--intent-id", "hu-timeout1")
    finally:
        stop.set()
        th.join(2)
    assert code == 1 and "hu-timeout1" in err and "omx status hu-timeout1" in err and "--intent-id" in err
    assert "was NOT sent" in err


def test_local_human_commands_need_a_terminal(tmp_path):
    p = cfgfile_at(tmp_path)
    make_eligible(tmp_path / "state")
    kill_from_outside(tmp_path / "state", "x")
    out, err = io.StringIO(), io.StringIO()
    code = cli_main(["--config", str(p), "unkill"], engine_factory=lambda c: None, input_fn=input, out=out, err=err)
    assert code == 1 and "HUMAN_CONSOLE_REQUIRED" in err.getvalue()    # pytest stdin is not a TTY
    assert KillSwitch(tmp_path / "state").is_killed()
