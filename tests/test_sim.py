import math

import pytest

from omx.types import ChildOrder, Side
from omx.venue.base import VenueError
from omx.venue.sim import CoinSpec, SimConfig, SimVenue


def quiet(**kw):
    """ETH with a frozen mid and no synthetic flow: matching is fully predictable."""
    spec = dict(vol_bps_sqrt_s=0.0, trade_rate_hz=0.0)
    spec.update(kw)
    return SimVenue(SimConfig(seed=1, start_equity_usd=1e6), coins=(CoinSpec("ETH", 3000.0, 0.1, 4, **spec),))


def co(cid, side, size, px, tif, ro=False):
    return ChildOrder(cid, "i", "ETH", side, size, px, tif, ro)


def test_book_shape_and_tick():
    v = quiet()
    s = v.snapshot("ETH")
    assert (s.best_bid, s.best_ask) == (2999.9, 3000.0)
    assert len(s.bids) == len(s.asks) == 10
    assert [lv.sz for lv in s.asks[:3]] == [5.0, 7.5, 10.0]
    assert s.ts_ms == v.now_ms() and s.day_ntl_usd == 1e9


def test_determinism_same_seed():
    def run(seed):
        v = SimVenue(SimConfig(seed=seed))
        v.advance(30_000)
        return [v.mid_at("ETH", v.cfg.start_ms + t) for t in range(0, 30_001, 1000)], \
               [(t.ts_ms, t.px, t.sz, t.side) for t in v.trades_since("ETH", 0)]
    assert run(3) == run(3)
    assert run(3) != run(4)


def test_place_latency_then_ioc_walks_book_with_fees():
    v = quiet()
    v.set_builder_fee(10)                                  # 1 bp
    v.place(co("a", Side.BUY, 8.0, 3000.1, "Ioc"))
    v.advance(200)
    assert v.fills_since(0) == []                          # not live before 212 ms
    v.advance(100)
    fills = v.fills_since(0)
    assert [(f.px, f.size) for f in fills] == [(3000.0, 5.0), (3000.1, 3.0)]
    f = fills[0]
    assert f.crossed and f.ts_ms == v.cfg.start_ms + 300
    assert f.fee_usd == pytest.approx(5 * 3000.0 * 4.5e-4)
    assert f.builder_fee_usd == pytest.approx(5 * 3000.0 * 1e-4)
    assert v.account().position("ETH") == pytest.approx(8.0)


def test_ioc_price_cap_and_partial_expiry():
    v = quiet()
    oid = v.place(co("a", Side.SELL, 20.0, 2999.8, "Ioc"))
    v.advance(300)
    assert sum(f.size for f in v.fills_since(0)) == pytest.approx(12.5)   # 5 @ 2999.9 + 7.5 @ 2999.8
    assert min(f.px for f in v.fills_since(0)) >= 2999.8
    assert v.order_status(oid)["status"] == "expired"
    assert v.account().open_orders == ()


def test_alo_rejected_when_crossing_and_queue_position():
    v = quiet()
    bad = v.place(co("x", Side.BUY, 1.0, 3000.0, "Alo"))
    good = v.place(co("y", Side.BUY, 1.0, 2999.9, "Alo"))
    v.advance(300)
    assert v.order_status(bad) == {**v.order_status(bad), "status": "rejected", "reason": "ALO_WOULD_CROSS"}
    st = v.order_status(good)
    assert st["status"] == "live" and st["queue"] == pytest.approx(5.0)   # all displayed size at the level
    v.inject_trade("ETH", Side.SELL, 3.0)
    assert v.fills_since(0) == []                                           # queue 5 -> 2
    v.inject_trade("ETH", Side.SELL, 3.0)
    f = v.fills_since(0)
    assert len(f) == 1 and f[0].size == pytest.approx(1.0) and not f[0].crossed
    assert f[0].fee_usd == pytest.approx(2999.9 * 1.5e-4)


def test_price_through_level_fills_remainder():
    v = quiet()
    v.place(co("y", Side.BUY, 2.0, 2999.7, "Alo"))
    v.advance(300)
    assert v.fills_since(0) == []
    v.shock_mid("ETH", -5.0)                                                # ~1.5 USD down: asks below 2999.7
    f = v.fills_since(0)
    assert sum(x.size for x in f) == pytest.approx(2.0) and all(x.px == 2999.7 and not x.crossed for x in f)


def test_cancel_latency_and_block_ordering():
    v = quiet()
    r = v.place(co("r", Side.SELL, 1.0, 3000.5, "Alo"))
    v.advance(300)
    assert v.cancel("ETH", r)                                               # t = 300, effective 778
    v.advance(266)                                                          # t = 566
    v.place(co("x", Side.BUY, 1.0, 3000.0, "Ioc"))                          # all three due at 778
    v.place(co("y", Side.BUY, 1.0, 2999.5, "Alo"))
    v.advance(200)                                                          # tick 766: nothing due yet
    assert v.order_status(r)["status"] == "live"
    v.advance(100)                                                          # tick 866 holds the block
    block = [(k, c) for t, k, c in v.events if t == v.now_ms()]
    assert block == [("cancel", "r"), ("Alo", "y"), ("Ioc", "x")]
    assert v.order_status(r)["status"] == "cancelled"


def test_validation_and_injected_errors():
    v = quiet()
    with pytest.raises(VenueError):
        v.place(co("a", Side.BUY, 1.0, 3000.05, "Gtc"))                    # off tick
    with pytest.raises(VenueError):
        v.place(co("b", Side.BUY, 0.001, 3000.0, "Gtc"))                   # < 10 USD
    with pytest.raises(VenueError):
        v.place(co("c", Side.BUY, 1.00001, 3000.0, "Gtc"))                 # off lot
    v.fail_next_places(1)
    with pytest.raises(VenueError):
        v.place(co("d", Side.BUY, 1.0, 2999.0, "Gtc"))
    assert v.place(co("e", Side.BUY, 1.0, 2999.0, "Gtc"))
    assert v.place_calls == 5


def test_stale_snapshot_knob():
    v = SimVenue(SimConfig(seed=2))
    v.advance(10_000)
    v.stale_ms = 5_000
    assert v.snapshot("ETH").ts_ms == v.now_ms() - 5_000
    v.stale_ms = 0
    assert v.snapshot("ETH").ts_ms == v.now_ms()


def test_toxic_flow_moves_mid_with_trades():
    def drift(toxic):
        v = SimVenue(SimConfig(seed=5), coins=(CoinSpec("ETH", 3000.0, 0.1, 4, vol_bps_sqrt_s=0.0,
                                                        buy_prob=0.2, toxic_bps=toxic),))
        v.advance(30_000)
        return math.log(v.mid_at("ETH", v.now_ms()) / 3000.0) * 1e4
    assert drift(0.0) == pytest.approx(0.0)
    assert drift(0.5) < -10.0


def test_equity_marks_to_mid_and_realises_pnl():
    v = quiet()
    v.place(co("a", Side.BUY, 1.0, 3000.0, "Ioc"))
    v.advance(300)
    fee = 3000.0 * 4.5e-4
    assert v.equity() == pytest.approx(1e6 - fee + (2999.95 - 3000.0))
    v.shock_mid("ETH", 100.0)
    v.place(co("b", Side.SELL, 1.0, v.snapshot("ETH").best_bid, "Ioc", ro=True))
    v.advance(300)
    assert v.account().position("ETH") == pytest.approx(0.0)
    sell = v.fills_since(0)[-1]
    assert v.cash == pytest.approx(1e6 - fee - sell.fee_usd + (sell.px - 3000.0))
