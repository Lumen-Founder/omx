import pytest

from omx import tca
from omx.audit import AuditLog
from omx.engine import Engine
from omx.policy import PolicyConfig
from omx.types import Algo, Fill, Intent, Side
from omx.venue.sim import CoinSpec, SimConfig, SimVenue, Trade

T = 1_800_000_000_000


def fill(px, sz=1.0, side=Side.BUY, fee=0.0, bfee=0.0, ts=T, crossed=True):
    return Fill("c", "ETH", side, sz, px, fee, bfee, ts, crossed)


def it(side=Side.BUY, size=2.0):
    return Intent("i", "ETH", side, size, algo=Algo.TWAP)


def test_shortfall_signs_and_fees():
    assert tca.shortfall_bps(Side.BUY, [fill(101.0)], 100.0, include_fees=False) == pytest.approx(100.0)
    assert tca.shortfall_bps(Side.SELL, [fill(99.0, side=Side.SELL)], 100.0, include_fees=False) == pytest.approx(100.0)
    assert tca.shortfall_bps(Side.BUY, [fill(99.0)], 100.0, include_fees=False) == pytest.approx(-100.0)
    assert tca.shortfall_bps(Side.BUY, [fill(100.0, fee=0.05, bfee=0.01)], 100.0) == pytest.approx(6.0)
    assert tca.shortfall_bps(Side.BUY, [], 100.0) is None


def test_report_fees_vwap_ratio_and_time():
    fills = [fill(100.0, fee=0.045, ts=T + 1_000), fill(100.2, fee=0.015, bfee=0.01, ts=T + 3_000, crossed=False)]
    trades = [Trade("ETH", T + 500, 100.0, 3.0, Side.BUY), Trade("ETH", T + 600, 101.0, 1.0, Side.BUY),
              Trade("ETH", T + 1_000, 100.0, 1.0, Side.BUY, own=True)]
    rep = tca.build_report(it(size=4.0), fills, 100.0, T, T + 5_000, trades=trades)
    assert rep["filled_size"] == 2.0 and rep["fill_ratio"] == 0.5
    assert rep["avg_px"] == pytest.approx(100.1)
    assert rep["interval_vwap"] == pytest.approx(100.25)                 # own print excluded
    assert rep["vs_vwap_bps"] == pytest.approx((100.1 - 100.25) / 100.25 * 1e4)
    assert rep["fees"]["exchange_usd"] == pytest.approx(0.06) and rep["fees"]["builder_usd"] == pytest.approx(0.01)
    assert rep["fees"]["maker_share"] == pytest.approx(100.2 / 200.2)
    assert rep["time_to_complete_ms"] == 3_000
    assert rep["price_shortfall_bps"] == pytest.approx(10.0)
    assert "markout" in tca.summary(rep) and "i buy ETH" in tca.summary(rep)


def test_markout_sign_and_pending():
    mids = {T + 5_000: 99.0}
    rep = tca.markouts([fill(100.0)], Side.BUY, lambda c, t: mids.get(t), (5_000, 60_000))
    assert rep["5000"]["bps"] == pytest.approx(-100.0) and rep["5000"]["n_fills"] == 1
    assert rep["60000"]["bps"] is None and rep["60000"]["n_pending"] == 1
    sell = tca.markouts([fill(100.0, side=Side.SELL)], Side.SELL, lambda c, t: 99.0, (5_000,))
    assert sell["5000"]["bps"] == pytest.approx(100.0)


def test_adverse_selection_resting_fill_has_negative_markout(tmp_path):
    """Toxic flow: takers mostly sell and each sell pushes mid down. Our passive bid gets filled
    exactly when the price is about to fall, so the post-fill markout is negative."""
    sim = SimVenue(SimConfig(seed=9), coins=(CoinSpec("ETH", 3000.0, 0.1, 4, vol_bps_sqrt_s=0.3, buy_prob=0.15,
                                                      toxic_bps=0.5),))
    audit = AuditLog(tmp_path / "audit.jsonl")
    eng = Engine(sim, PolicyConfig(default_coin_notional_usd=50_000, max_account_notional_usd=100_000,
                                   max_child_notional_usd=20_000), audit)
    bid = round(sim.snapshot("ETH").best_bid - 0.5, 1)                    # 5 ticks below touch: rests first
    eng.submit(Intent("tox", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=bid, post_only=True))
    for _ in range(700):                                                   # 70 s: fill + 60 s horizon
        sim.advance(100)
        eng.step()
    assert eng.status("tox").filled_size == pytest.approx(1.0)
    rep = eng.tca("tox")
    assert rep["fees"]["maker_share"] == pytest.approx(1.0)
    assert rep["markouts"]["5000"]["bps"] < 0 and rep["markouts"]["60000"]["bps"] < 0
    assert rep["markouts"]["60000"]["bps"] < rep["markouts"]["5000"]["bps"]
    assert rep["markouts"]["60000"]["n_pending"] == 0
    ok, n, msg = AuditLog.verify(audit.path)
    assert ok and n > 0, msg
