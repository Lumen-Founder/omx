import json

import pytest

from omx.audit import AuditLog
from omx.engine import Engine
from omx.killswitch import CONFIRM_PHRASE, kill_from_outside
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.base import VenueError
from omx.venue.sim import CoinSpec, SimConfig, SimVenue

CAPS = dict(default_coin_notional_usd=50_000, max_account_notional_usd=100_000, max_child_notional_usd=20_000)
BIG = dict(default_coin_notional_usd=1e6, max_account_notional_usd=1e6, max_child_notional_usd=1e6)


def quiet_coins(**kw):
    spec = dict(vol_bps_sqrt_s=0.0, trade_rate_hz=0.0)
    spec.update(kw)
    return (CoinSpec("ETH", 3000.0, 0.1, 4, **spec),)


@pytest.fixture
def mk(tmp_path):
    made = []

    def make(seed=1, coins=None, policy=None, builder=0, equity=10_000.0):
        n = len(made)
        sim = SimVenue(SimConfig(seed=seed, start_equity_usd=equity), coins=coins)
        audit = AuditLog(tmp_path / f"audit{n}.jsonl")
        eng = Engine(sim, policy or PolicyConfig(**CAPS), audit, builder_fee_tenths_bps=builder,
                     builder_address=("0x" + "be" * 20) if builder else None,
                     state_dir=tmp_path / f"state{n}")
        made.append(audit)
        return sim, eng

    yield make
    for a in made:                                   # every e2e log must verify at the end
        ok, n, msg = AuditLog.verify(a.path)
        assert ok and n > 0, msg


def run(sim, eng, ms, step=100):
    for _ in range(ms // step):
        sim.advance(step)
        eng.step()


def recs(eng, kind=None):
    out = [json.loads(line) for line in eng.audit.path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in out if kind is None or r["kind"] == kind]


def test_market_e2e_fills_with_fees_in_shortfall(mk):
    sim, eng = mk()
    r = eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    assert r.state == "working" and r.children_sent == 1 and sim.place_calls == 1
    run(sim, eng, 1_000)
    r = eng.status("m1")
    assert r.state == "done" and r.filled_size == pytest.approx(1.0)
    assert r.avg_px <= r.arrival_mid * 1.005
    expect = (r.avg_px - r.arrival_mid) / r.arrival_mid * 1e4 + r.exch_fee_usd / r.arrival_mid * 1e4
    assert r.shortfall_bps == pytest.approx(expect)
    assert r.exch_fee_usd == pytest.approx(r.avg_px * 4.5e-4)
    kinds = {x["kind"] for x in recs(eng)}
    assert {"intent_received", "child_decision", "child_sent", "child_ack", "fill", "intent_done"} <= kinds


def test_market_price_cap_gives_partial(mk):
    sim, eng = mk(coins=quiet_coins(), policy=PolicyConfig(**BIG), equity=1e6)
    eng.submit(Intent("m2", "ETH", Side.BUY, 30.0, algo=Algo.MARKET, max_slippage_bps=1.0))
    run(sim, eng, 1_000)
    r = eng.status("m2")
    assert r.state == "done" and r.filled_size == pytest.approx(22.5)    # 5 + 7.5 + 10 up to 3000.2
    assert max(f.px for f in sim.fills_since(0)) == 3000.2
    assert r.message.startswith("partial")


def test_limit_alo_fills_as_maker_and_gtc_crosses(mk):
    sim, eng = mk(coins=quiet_coins())
    eng.submit(Intent("l1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2999.9, post_only=True))
    run(sim, eng, 300)
    assert eng.status("l1").state == "working"
    sim.inject_trade("ETH", Side.SELL, 6.0)                                # queue 5, then us
    run(sim, eng, 300)
    r = eng.status("l1")
    assert r.state == "done" and r.filled_size == pytest.approx(1.0)
    assert r.exch_fee_usd == pytest.approx(2999.9 * 1.5e-4)
    eng.submit(Intent("l2", "ETH", Side.SELL, 1.0, algo=Algo.LIMIT, limit_px=2999.9))
    run(sim, eng, 500)
    r2 = eng.status("l2")
    assert r2.state == "done" and r2.avg_px == 2999.9 and r2.exch_fee_usd == pytest.approx(2999.9 * 4.5e-4)


def test_twap_ioc_slices_on_schedule(mk):
    sim, eng = mk(seed=3)
    eng.submit(Intent("t1", "ETH", Side.BUY, 2.0, algo=Algo.TWAP, duration_s=30, slices=6))
    run(sim, eng, 35_000)
    r = eng.status("t1")
    assert r.state == "done" and r.filled_size == pytest.approx(2.0) and r.children_sent >= 6
    sent = [x["data"]["now_ms"] for x in recs(eng, "child_sent")]
    assert all(b - a >= 5_000 for a, b in zip(sent, sent[1:6]))
    caps = [x["data"]["child"]["px"] for x in recs(eng, "child_decision")]
    assert all(px <= r.arrival_mid * 1.005 for px in caps)
    assert all(f.crossed for f in sim.fills_since(0))


def test_twap_post_only_unfilled_ends_clean(mk):
    sim, eng = mk(coins=quiet_coins())
    eng.submit(Intent("t2", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=30, slices=3, post_only=True))
    run(sim, eng, 32_000)
    r = eng.status("t2")
    assert r.state == "done" and r.filled_size == 0.0 and r.message.startswith("partial")
    assert sim.account().open_orders == ()
    assert recs(eng, "intent_done")


def test_pov_tracks_share_of_volume(mk):
    sim, eng = mk(seed=4)
    t0 = sim.now_ms()
    eng.submit(Intent("p1", "ETH", Side.BUY, 5.0, algo=Algo.POV, pov_rate=0.1, duration_s=60))
    run(sim, eng, 61_000)
    r = eng.status("p1")
    vol = sum(t.sz for t in sim.trades_between("ETH", t0 + 1, sim.now_ms()) if not t.own)
    assert r.state == "done"
    assert r.filled_size <= 0.1 * vol + 1e-9
    assert r.filled_size >= 0.5 * min(5.0, 0.1 * vol)


def test_chase_reprices_after_cancel_latency_and_stops_at_limit(mk):
    sim, eng = mk(seed=6, coins=(CoinSpec("ETH", 3000.0, 0.1, 4, vol_bps_sqrt_s=1.5, buy_prob=0.7,
                                          toxic_bps=0.3),))
    arrival = sim.snapshot("ETH").mid
    limit = round(arrival * 1.0005, 1)
    eng.submit(Intent("c1", "ETH", Side.BUY, 1.0, algo=Algo.CHASE, limit_px=limit, duration_s=30))
    run(sim, eng, 31_000)
    sent = [x["data"]["now_ms"] for x in recs(eng, "child_sent")]
    reprices = [x["data"]["now_ms"] for x in recs(eng, "cancel") if x["data"].get("reason") == "REPRICE"]
    assert reprices, "price ran away; chase should have re-priced"
    for t in reprices:
        nxt = [s for s in sent if s > t]
        assert not nxt or nxt[0] - t >= 478
    assert all(x["data"]["child"]["px"] <= limit for x in recs(eng, "child_decision"))
    assert all(x["data"]["child"]["tif"] == "Alo" for x in recs(eng, "child_decision"))
    assert sim.account().open_orders == ()


def test_policy_blocks_never_reach_the_venue(mk):
    sim, eng = mk(policy=PolicyConfig(allowed_coins=frozenset({"BTC"}), **CAPS))
    r = eng.submit(Intent("b1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    assert r.state == "rejected" and "COIN_NOT_ALLOWED:ETH" in r.block_reasons
    sim2, eng2 = mk(policy=PolicyConfig(**{**CAPS, "max_child_notional_usd": 100.0}))
    eng2.submit(Intent("b2", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=20, slices=5))
    run(sim2, eng2, 30_000)
    r2 = eng2.status("b2")
    assert sim.place_calls == 0 and sim2.place_calls == 0
    assert r2.children_sent == 0 and r2.children_blocked >= 3 and "CHILD_NOTIONAL_CAP" in r2.block_reasons
    assert r2.state in ("done", "rejected")
    assert all(not x["data"]["allow"] for x in recs(eng2, "child_decision"))


def test_kill_mid_twap_cancels_respecting_latency(mk):
    sim, eng = mk(coins=quiet_coins())
    eng.submit(Intent("k1", "ETH", Side.BUY, 1.2, algo=Algo.TWAP, duration_s=60, slices=6, post_only=True))
    run(sim, eng, 25_000)
    assert len(sim.account().open_orders) == 3
    calls = sim.place_calls
    res = eng.kill("test")
    assert res["killed"] and res["cancels_requested"] == 3 and eng.status("k1").state == "killed"
    run(sim, eng, 300)
    assert len(sim.account().open_orders) == 3                            # cancels not effective yet (478 ms)
    run(sim, eng, 200)
    assert sim.account().open_orders == ()
    run(sim, eng, 60_000)
    assert sim.place_calls == calls and sim.cancel_calls == 3
    assert recs(eng, "intent_done")[-1]["data"]["report"]["state"] == "killed"
    assert eng.submit(Intent("k2", "ETH", Side.BUY, 0.1, algo=Algo.MARKET)).state == "rejected"
    assert sim.place_calls == calls
    assert not eng.unkill("yes")["ok"] and eng.killed
    assert eng.unkill(CONFIRM_PHRASE)["ok"] and not eng.killed
    eng.submit(Intent("k3", "ETH", Side.BUY, 0.1, algo=Algo.MARKET))
    assert sim.place_calls == calls + 1
    assert eng.status("k1").state == "killed"                             # killed intents stay killed
    run(sim, eng, 500)


def test_external_kill_file_stops_engine(mk):
    sim, eng = mk(seed=2)
    eng.submit(Intent("x1", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=40, slices=8))
    run(sim, eng, 10_000)
    kill_from_outside(eng.state_dir, "human at the CLI")
    calls = sim.place_calls
    run(sim, eng, 40_000)
    assert eng.status("x1").state == "killed" and sim.place_calls == calls
    k = recs(eng, "kill")
    assert k and k[0]["data"]["source"] == "external" and k[0]["data"]["reason"] == "human at the CLI"


def test_stale_data_blocks(mk):
    sim, eng = mk()
    sim.advance(10_000)
    sim.stale_ms = 5_000
    r = eng.submit(Intent("s1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    assert r.state == "rejected" and any(x.startswith("STALE_DATA") for x in r.block_reasons)
    assert sim.place_calls == 0


def test_daily_loss_limit_trips_after_losses(mk):
    sim, eng = mk(coins=quiet_coins(), policy=PolicyConfig(**{**CAPS, "max_daily_loss_usd": 50.0}))
    eng.submit(Intent("d1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, 500)
    assert eng.status("d1").filled_size == pytest.approx(1.0)
    sim.shock_mid("ETH", -300.0)                                           # ~ -89 USD on 1 ETH
    r = eng.submit(Intent("d2", "ETH", Side.BUY, 0.1, algo=Algo.MARKET))
    assert r.state == "rejected" and "DAILY_LOSS_LIMIT" in r.block_reasons and sim.place_calls == 1
    r3 = eng.submit(Intent("d3", "ETH", Side.SELL, 1.0, algo=Algo.MARKET, reduce_only=True))
    assert r3.state == "working"                                           # reducing risk stays allowed
    run(sim, eng, 500)
    assert sim.account().position("ETH") == pytest.approx(0.0)
    day = json.loads((eng.state_dir / "engine_day.json").read_text())
    assert day["equity_usd"] == pytest.approx(10_000.0)


def test_venue_errors_are_recorded_and_backed_off(mk):
    sim, eng = mk(seed=5)
    sim.fail_next_places(3)
    real, n = sim.account, {"k": 0}

    def flaky():
        n["k"] += 1
        if n["k"] == 5:
            raise VenueError("account endpoint down")
        return real()

    sim.account = flaky
    eng.submit(Intent("e1", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=20, slices=4))
    run(sim, eng, 30_000)
    r = eng.status("e1")
    assert r.state == "done" and r.filled_size == pytest.approx(1.0) and "venue errors: 3" in r.message
    assert len(recs(eng, "child_error")) == 3 and recs(eng, "venue_error")
    sent = [x["data"]["now_ms"] for x in recs(eng, "child_sent")]
    assert sent[1] - sent[0] >= 500 and sent[2] - sent[1] >= 1_000      # exponential backoff


def _scenario(tmp_path, seed, tag):
    sim = SimVenue(SimConfig(seed=seed))
    eng = Engine(sim, PolicyConfig(**CAPS), AuditLog(tmp_path / f"det_{tag}.jsonl"))
    eng.submit(Intent("a", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=20, slices=4, post_only=True))
    eng.submit(Intent("b", "ETH", Side.SELL, 0.5, algo=Algo.CHASE, duration_s=20))
    run(sim, eng, 25_000)
    assert AuditLog.verify(eng.audit.path)[0]
    return [(f.child_id, f.ts_ms, f.size, f.px) for f in sim.fills_since(0)], \
           [eng.status(i).__dict__ for i in ("a", "b")]


def test_determinism_same_seed(tmp_path):
    assert _scenario(tmp_path, 11, "x") == _scenario(tmp_path, 11, "y")
    assert _scenario(tmp_path, 11, "x2")[0] != _scenario(tmp_path, 12, "z")[0]


def test_preview_sends_nothing_and_flags_cumulative_caps(mk):
    sim, eng = mk(policy=PolicyConfig(**{**CAPS, "default_coin_notional_usd": 1_500.0}))
    p = eng.preview(Intent("v1", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=50, slices=5))
    assert sim.place_calls == 0 and p["sent"] == 0 and len(p["children"]) == 5
    assert [c["allow"] for c in p["children"]][:3] == [True, True, False]
    assert "COIN_NOTIONAL_CAP" in p["children"][2]["reasons"]
    assert p["est_fee_usd"]["exchange"] == pytest.approx(p["notional_usd"] * 4.5e-4)
    assert recs(eng, "preview") and not recs(eng, "child_sent")


def test_builder_fee_and_intent_cancel(mk):
    sim, eng = mk(builder=10)
    eng.submit(Intent("f1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, 500)
    r = eng.status("f1")
    assert r.builder_fee_usd == pytest.approx(r.avg_px * 1.0 * 1e-4)
    eng.submit(Intent("f2", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2985.0, post_only=True))
    run(sim, eng, 500)
    assert eng.cancel("f2").state == "cancelled"
    run(sim, eng, 600)
    assert sim.account().open_orders == ()
    assert any(x["data"]["report"]["intent_id"] == "f2" for x in recs(eng, "intent_done"))


def test_alo_reject_is_reported(mk):
    sim, eng = mk(coins=quiet_coins())
    eng.submit(Intent("r1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=3000.0, post_only=True))  # at the ask
    run(sim, eng, 500)
    r = eng.status("r1")
    assert r.state == "done" and r.filled_size == 0 and "ALO_WOULD_CROSS" in r.message
    assert recs(eng, "child_error")[0]["data"]["error"] == "ALO_WOULD_CROSS"
