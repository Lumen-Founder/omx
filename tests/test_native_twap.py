"""Hyperliquid native TWAP (twapOrder) through OMX: off by default, one policy check at full size plus an
estimated suborder cap, no builder field, suborder fills read through twap_fills, cancel / kill / restart
stop it, an unknown start outcome stays in exposure, and an agent's native TWAP always waits for a human."""
import json

from test_hyperliquid import agent  # noqa: F401  (fixture)

import pytest

from omx.audit import AuditLog
from omx.engine import TWAP_FILE, TWAP_GRACE_MS, Engine
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.sim import CoinSpec, SimConfig, SimVenue

CAPS = dict(default_coin_notional_usd=50_000, max_account_notional_usd=100_000, max_child_notional_usd=20_000)
BUILDER = "0x" + "be" * 20


def quiet():
    return (CoinSpec("ETH", 3000.0, 0.1, 4, vol_bps_sqrt_s=0.0, trade_rate_hz=0.0),)


def make(tmp_path, name="a", allow=True, sim=None, builder=10, **caps):
    sim = sim or SimVenue(SimConfig(seed=1), coins=quiet())
    pol = PolicyConfig(**{**CAPS, **caps}, allow_native_twap=allow)
    eng = Engine(sim, pol, AuditLog(tmp_path / f"audit_{name}.jsonl"), builder_fee_tenths_bps=builder,
                 builder_address=BUILDER if builder else None, state_dir=tmp_path / "state")
    return sim, eng


def run(sim, eng, ms, step=500):
    for _ in range(ms // step):
        sim.advance(step)
        eng.step()


def twap(iid="n1", size=1.0, minutes=5, **kw):
    return Intent(iid, "ETH", Side.BUY, size, algo=Algo.NATIVE_TWAP, duration_s=minutes * 60, **kw)


def recs(eng, kind):
    rows = [json.loads(x) for x in eng.audit.path.read_text(encoding="utf-8").splitlines() if x]
    return [r for r in rows if r["kind"] == kind]


def test_off_by_default_nothing_is_sent(tmp_path):
    sim, eng = make(tmp_path, allow=False)
    r = eng.submit(twap())
    assert r.state == "rejected" and r.message.startswith("NATIVE_TWAP_DISABLED")
    assert sim.twap_calls == 0 and sim.place_calls == 0


def test_runs_to_completion_without_a_builder_fee(tmp_path):
    sim, eng = make(tmp_path)
    r = eng.submit(twap())
    assert r.state == "working" and sim.twap_calls == 1 and sim.place_calls == 0
    run(sim, eng, 5 * 60_000 + TWAP_GRACE_MS + 5_000)
    r = eng.status("n1")
    assert r.state == "done" and r.filled_size == pytest.approx(1.0) and r.message == "filled"
    assert r.builder_fee_usd == 0.0 and r.exch_fee_usd > 0
    assert sim.account().position("ETH") == pytest.approx(1.0)
    assert len({f.ts_ms for f in eng._work["n1"].fills}) >= 9          # suborders 30 s apart
    rec = eng.receipt("n1")
    assert rec["final"] and rec["checks"]["fills_match"] and rec["checks"]["children_builder_as_configured"]
    assert rec["children"][0]["route"] == "hyperliquid_native_twap" and rec["children"][0]["builder"] is None
    assert rec["summary"]["builder_fee_usd"] == 0.0
    assert AuditLog.verify(eng.audit.path)[0]


def test_omx_children_still_must_carry_the_builder(tmp_path):
    sim, eng = make(tmp_path)
    eng.submit(Intent("m1", "ETH", Side.BUY, 0.1, algo=Algo.MARKET))
    run(sim, eng, 2_000)
    rec = eng.receipt("m1")
    assert rec["children"][0]["route"] == "omx" and rec["checks"]["children_builder_as_configured"]
    assert rec["summary"]["builder_fee_usd"] > 0


def test_whole_size_is_checked_once(tmp_path):
    sim, eng = make(tmp_path, default_coin_notional_usd=2_000)
    r = eng.submit(twap())                                               # 1 ETH ~ 3000 USD
    assert r.state == "rejected" and "COIN_NOTIONAL_CAP" in r.message and sim.twap_calls == 0


def test_estimated_suborder_is_held_to_the_child_cap(tmp_path):
    sim, eng = make(tmp_path, max_child_notional_usd=500)               # 3000 / 10 * 3 = 900 > 500
    r = eng.submit(twap())
    assert r.state == "rejected" and "NATIVE_TWAP_SUBORDER_CAP" in r.message and sim.twap_calls == 0
    dec = recs(eng, "child_decision")[-1]["data"]
    assert dec["native_twap"]["suborder_bound_usd"] == pytest.approx(900.0, rel=0.01)
    sim2, eng2 = make(tmp_path, name="b", max_child_notional_usd=500)
    assert eng2.submit(twap("n2", minutes=10)).state == "working"         # 20 suborders: 450 <= 500


def test_minimum_and_shape(tmp_path):
    sim, eng = make(tmp_path)
    assert "NATIVE_TWAP_MIN_NOTIONAL" in eng.submit(twap("s", size=0.01)).message   # 30 USD
    assert eng.submit(twap("d1", minutes=4)).message.startswith("NATIVE_TWAP_DURATION")
    odd = Intent("d2", "ETH", Side.BUY, 1.0, algo=Algo.NATIVE_TWAP, duration_s=330)
    assert eng.submit(odd).message.startswith("NATIVE_TWAP_DURATION")
    assert eng.submit(twap("l", limit_px=3100.0)).message.startswith("NATIVE_TWAP_NO_LIMIT_PX")
    assert sim.twap_calls == 0


def test_running_twap_counts_in_exposure(tmp_path):
    sim, eng = make(tmp_path, default_coin_notional_usd=5_000)
    eng.submit(twap())
    run(sim, eng, 1_000)
    r = eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))   # 3000 more on top of 3000 working
    assert r.state == "rejected" and "COIN_NOTIONAL_CAP" in r.message


def test_cancel_stops_the_remaining_suborders(tmp_path):
    sim, eng = make(tmp_path)
    eng.submit(twap())
    run(sim, eng, 65_000)
    eng.cancel("n1")
    tid = eng._work["n1"].children["n1.tw"].oid.split(":")[1]
    assert sim.twap_state(tid) == "terminated"
    run(sim, eng, 3_000)
    w = eng._work["n1"]
    assert w.state == "cancelled" and w.children["n1.tw"].status == "cancelled" and w.finalized
    assert 0 < w.filled() < 1.0
    assert w.filled() == pytest.approx(sim.account().position("ETH"))


def test_kill_stops_it(tmp_path):
    sim, eng = make(tmp_path)
    eng.submit(twap())
    run(sim, eng, 31_000)
    eng.kill("test")
    tid = eng._work["n1"].children["n1.tw"].oid.split(":")[1]
    assert sim.twap_state(tid) == "terminated" and eng.status("n1").state == "killed"


def test_a_restart_cancels_the_previous_runs_twap(tmp_path):
    sim, eng = make(tmp_path)
    eng.submit(twap())
    run(sim, eng, 31_000)
    tid = eng._work["n1"].children["n1.tw"].oid.split(":")[1]
    assert (tmp_path / "state" / TWAP_FILE).exists()
    _, eng2 = make(tmp_path, name="b", sim=sim)                          # same state_dir, new process
    eng2.step()
    assert sim.twap_state(tid) == "terminated"
    assert any(r["data"].get("scope") == "native_twap" and r["data"]["ok"] for r in recs(eng2, "cancel"))
    assert (tmp_path / "state" / TWAP_FILE).read_text(encoding="utf-8") == ""


def test_lost_start_answer_stays_in_exposure_and_is_reported(tmp_path):
    sim, eng = make(tmp_path, default_coin_notional_usd=5_000)
    sim.lose_next_place_responses(1)
    r = eng.submit(twap())
    assert r.state == "working" and "NATIVE_TWAP_OUTCOME_UNKNOWN" in r.message
    run(sim, eng, 1_000)
    assert "COIN_NOTIONAL_CAP" in eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET)).message
    run(sim, eng, 5 * 60_000 + TWAP_GRACE_MS + 2_000)
    r = eng.status("n1")
    assert r.state == "done" and "NATIVE_TWAP_OUTCOME_UNKNOWN" in r.message
    assert sim.account().position("ETH") > 0                             # it did run: the venue is the record


def test_preview_shows_one_protocol_order_and_no_builder_fee(tmp_path):
    sim, eng = make(tmp_path)
    pv = eng.preview(twap())
    row = pv["children"][0]
    assert len(pv["children"]) == 1 and row["tif"] == "Twap" and row["allow"]
    assert row["native_twap"]["suborders"] == 10 and pv["est_fee_usd"]["builder"] == 0.0
    _, off = make(tmp_path, name="b", allow=False)
    assert "NATIVE_TWAP_DISABLED: policy.allow_native_twap is false" in off.preview(twap())["children"][0]["reasons"]


def test_venue_without_native_twap_refuses(tmp_path):
    class Plain(SimVenue):
        place_twap = property(lambda self: (_ for _ in ()).throw(AttributeError()))
    sim, eng = make(tmp_path, sim=Plain(SimConfig(seed=1), coins=quiet()))
    assert eng.submit(twap()).message.startswith("NATIVE_TWAP_UNSUPPORTED")


def test_agent_native_twap_always_waits_for_a_human(tmp_path):
    from test_mcp import FakeEngine, make_cfg, make_eligible
    from omx.approvals import AgentBudget, ApprovalQueue
    from omx.mcp_server import AgentGateway
    cfg = make_cfg(tmp_path, **{"allow_native_twap = false": "allow_native_twap = true"})
    assert cfg.policy.allow_native_twap
    make_eligible(cfg.state_dir)
    eng = FakeEngine(mid=100.0)
    gw = AgentGateway(eng, cfg, ApprovalQueue(cfg.state_dir), AgentBudget(cfg.state_dir, 1e9),
                      clock=lambda: eng.now_s)
    args = {"coin": "ETH", "side": "buy", "size": 2.0, "algo": "native_twap", "duration_s": 600}
    pv = gw.preview_order("agent:t", args)
    assert pv["would_need_human_approval"] and "NATIVE_TWAP_NEEDS_HUMAN" in pv["routing_reasons"]
    out = gw.submit_order("agent:t", args)                                 # 200 USD: far under any threshold
    assert out["status"] == "pending_approval" and "NATIVE_TWAP_NEEDS_HUMAN" in out["routing_reasons"]
    assert eng.submitted == []
    tw = gw.preview_order("agent:t", {**args, "algo": "twap", "slices": 10})
    assert tw["alternatives"][0]["available_in_omx"] is True


def test_notional_bound_uses_the_protocol_slippage():
    from omx.approvals import notional_bound_usd
    assert notional_bound_usd(twap(max_slippage_bps=50.0), 100.0) == pytest.approx(103.0)


def test_hyperliquid_twap_wire_and_answers(agent):
    from test_hyperliquid import FakeTransport, venue
    from omx.types import ChildOrder
    from omx.venue.base import VenueError
    ok = {"status": "ok", "response": {"type": "twapOrder", "data": {"status": {"running": {"twapId": 77738308}}}}}
    bad = {"status": "ok", "response": {"type": "twapOrder", "data": {"status": {"error": "Invalid TWAP duration"}}}}
    cancelled = {"status": "ok", "response": {"type": "twapCancel", "data": {"status": "success"}}}
    slices = [{"fill": {"coin": "ETH", "px": "1891.4", "sz": "0.1", "side": "B", "time": 1, "tid": 11,
                        "fee": "0.08", "crossed": True}, "twapId": 77738308},
              {"fill": {"coin": "ETH", "px": "1892.0", "sz": "0.2", "side": "B", "time": 2, "tid": 12,
                        "fee": "0.17", "crossed": True}, "twapId": 5}]
    t = FakeTransport(info={"userTwapSliceFills": slices}, exchange=[ok, bad, cancelled])
    v = venue(agent, t)
    c = ChildOrder("n1.tw", "n1", "ETH", Side.BUY, 1.23456, 1891.4, "Twap")
    assert v.place_twap(c, 30) == "77738308"
    sent = t.calls[-1][1]["action"]
    assert sent == {"type": "twapOrder", "twap": {"a": 1, "b": True, "s": "1.2345", "r": False, "m": 30, "t": False}}
    assert "builder" not in sent
    with pytest.raises(VenueError, match="Invalid TWAP"):
        v.place_twap(c, 30)
    assert v.cancel_twap("ETH", "77738308") is True
    assert t.calls[-1][1]["action"] == {"type": "twapCancel", "a": 1, "t": 77738308}
    fills = v.twap_fills("77738308")
    assert [f.fill_id for f in fills] == ["11"] and fills[0].builder_fee_usd == 0.0
    with pytest.raises(VenueError):
        v.build_twap_action(ChildOrder("x", "n1", "ETH", Side.BUY, 1.0, 1.0, "Twap", builder=("0x" + "be" * 20, 10)), 30)
