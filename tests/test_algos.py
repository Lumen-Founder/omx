import pytest

from omx.algos import AlgoState, Cancel, Ctx, Place, Working, cap_px, plan, round_px, round_sz, schedule, validate
from omx.types import Algo, Intent, Side

T0 = 1_800_000_000_000


def it(**kw):
    base = dict(intent_id="i", coin="ETH", side=Side.BUY, size=1.0, algo=Algo.TWAP)
    base.update(kw)
    return Intent(**base)


def ctx(now=T0, **kw):
    base = dict(now_ms=now, start_ms=T0, arrival_mid=3000.0, mid=3000.0, best_bid=2999.9, best_ask=3000.1,
                tick=0.1, sz_decimals=4)
    base.update(kw)
    return Ctx(**base)


def places(p):
    return [a for a in p.actions if isinstance(a, Place)]


def cancels(p):
    return [a for a in p.actions if isinstance(a, Cancel)]


def test_rounding():
    assert round_px(3000.07, 0.1, "down") == 3000.0
    assert round_px(3000.01, 0.1, "up") == 3000.1
    assert round_px(3000.0, 0.1, "up") == 3000.0
    assert round_px(0.123456, 0.0001, "near") == 0.1235
    assert round_sz(1.23456789, 4) == 1.2345
    assert round_sz(0.3, 1) == 0.3


def test_cap_px_uses_arrival_current_mid_and_limit():
    assert cap_px(it(max_slippage_bps=10), ctx()) == 3003.0
    assert cap_px(it(max_slippage_bps=10), ctx(mid=2990.0)) == round_px(2990 * 1.001, 0.1, "down")
    assert cap_px(it(max_slippage_bps=10, limit_px=3001.0), ctx()) == 3001.0
    assert cap_px(it(side=Side.SELL, max_slippage_bps=10), ctx()) == 2997.0


def test_market_single_capped_ioc_then_done():
    p = plan(it(algo=Algo.MARKET, size=1.23456), AlgoState(), ctx())
    assert places(p) == [Place(1.2345, 3015.0, "Ioc")]
    p2 = plan(it(algo=Algo.MARKET), p.state, ctx(filled=0.5))
    assert p2.done and not p2.actions


def test_limit_tif():
    assert places(plan(it(algo=Algo.LIMIT, limit_px=2990.05), AlgoState(), ctx()))[0] == Place(1.0, 2990.0, "Gtc")
    p = plan(it(algo=Algo.LIMIT, limit_px=2990.0, post_only=True, side=Side.SELL), AlgoState(), ctx())
    assert places(p)[0].tif == "Alo"


def test_twap_slices_and_deficit_rollover():
    i = it(size=1.0, duration_s=50, slices=5)                    # one slice every 10 s
    p = plan(i, AlgoState(), ctx())
    assert places(p) == [Place(0.2, 3015.0, "Ioc")]
    assert plan(i, p.state, ctx(now=T0 + 9_999, filled=0.1)).actions == ()
    p2 = plan(i, p.state, ctx(now=T0 + 10_000, filled=0.1))     # target 0.4, filled 0.1 -> 0.3
    assert places(p2)[0].size == pytest.approx(0.3)
    p3 = plan(i, p2.state, ctx(now=T0 + 30_000, filled=0.4))    # skipped slice 3: catch up to 0.8
    assert places(p3)[0].size == pytest.approx(0.4) and p3.state.slices_sent == 4


def test_twap_post_only_reprices_and_ends():
    i = it(size=1.0, duration_s=20, slices=2, post_only=True)
    old = Working("i.1", 2999.5, 0.5, "Alo")
    p = plan(i, AlgoState(children=1, slices_sent=1), ctx(now=T0 + 10_000, working=(old,)))
    assert cancels(p) == [Cancel("i.1", "REPRICE")]
    assert places(p) == [Place(0.5, 2999.9, "Alo")]
    end = AlgoState(children=2, slices_sent=2)
    p_end = plan(i, end, ctx(now=T0 + 20_000, working=(old,)))
    assert cancels(p_end) and not p_end.done
    assert plan(i, end, ctx(now=T0 + 20_000, filled=0.4)).done


def test_pov_sizing_interval_and_bound():
    i = it(algo=Algo.POV, pov_rate=0.1, size=5.0)
    p = plan(i, AlgoState(), ctx(market_volume=10.0))
    assert places(p)[0].size == pytest.approx(1.0)
    assert plan(i, p.state, ctx(now=T0 + 500, market_volume=30.0)).actions == ()     # min interval
    p2 = plan(i, p.state, ctx(now=T0 + 1_000, market_volume=30.0, filled=1.0))
    assert places(p2)[0].size == pytest.approx(2.0)
    pb = plan(it(algo=Algo.POV, pov_rate=0.5, size=5.0, slices=10), AlgoState(), ctx(market_volume=100.0))
    assert places(pb)[0].size == pytest.approx(0.5)                                  # bounded by size / slices
    assert plan(i, AlgoState(), ctx(market_volume=0.001)).actions == ()              # below 10 USD minimum


def test_chase_reprice_waits_for_cancel_and_respects_limit():
    i = it(algo=Algo.CHASE, size=1.0, limit_px=3000.0)
    p = plan(i, AlgoState(), ctx())
    assert places(p) == [Place(1.0, 2999.9, "Alo")]
    w = Working("i.1", 2999.9, 1.0, "Alo")
    assert plan(i, p.state, ctx(best_bid=2999.9, working=(w,))).actions == ()          # touch unchanged
    p2 = plan(i, p.state, ctx(best_bid=3000.2, best_ask=3000.3, working=(w,)))
    assert cancels(p2) == [Cancel("i.1", "REPRICE")] and not places(p2)              # cancel first
    wp = Working("i.1", 2999.9, 1.0, "Alo", cancel_pending=True)
    assert plan(i, p2.state, ctx(best_bid=3000.2, working=(wp,))).actions == ()       # wait for cancel latency
    p3 = plan(i, p2.state, ctx(best_bid=3000.2, best_ask=3000.3))
    assert places(p3) == [Place(1.0, 3000.0, "Alo")]                                  # stops at limit_px
    wl = Working("i.2", 3000.0, 1.0, "Alo")
    assert plan(i, p3.state, ctx(best_bid=3001.0, working=(wl,))).actions == ()


def test_validate_and_schedule():
    assert validate(it(size=0)) == "SIZE_NOT_POSITIVE"
    assert validate(it(algo=Algo.LIMIT)) == "LIMIT_NEEDS_LIMIT_PX"
    assert validate(it(algo=Algo.POV, pov_rate=0.0)) == "POV_RATE_OUT_OF_RANGE"
    assert validate(it(duration_s=10, slices=3)) is None
    s = schedule(it(size=1.0, duration_s=30, slices=3), ctx())
    assert [x["t_ms"] for x in s] == [0, 10_000, 20_000]
    assert sum(x["size"] for x in s) == pytest.approx(1.0)
