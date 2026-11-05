"""Fix round R1, lane X: regression tests for review findings F2, F3, F4, F6, F7 and the engine
side of F1 (kill scope, orphans, views). Each review scenario is ported with the fixed behaviour
asserted. Offline: sockets refused, throwaway keys only."""
import json

import pytest
from eth_account import Account

from omx.audit import AuditLog
from omx.engine import UNKNOWN_RESEND_MS, Engine
from omx.killswitch import kill_from_outside
from omx.policy import PolicyConfig
from omx.types import Algo, ChildOrder, Intent, Side
from omx.venue import hyperliquid as hlmod
from omx.venue.base import VenueAborted, VenueAmbiguous, VenueError, cloid_for, is_omx_cloid
from omx.venue.hyperliquid import TESTNET_API_URL, HyperliquidVenue, LocalKeySigner, RequestsTransport, TokenBucket
from omx.venue.sim import CoinSpec, SimConfig, SimVenue

from test_hyperliquid import BOOK, META, NOW, USER, FakeClock, FakeTransport, ok_order

BIG = PolicyConfig(default_coin_notional_usd=1e6, max_account_notional_usd=1e6, max_child_notional_usd=1e6)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def quiet(**kw):
    spec = dict(vol_bps_sqrt_s=0.0, trade_rate_hz=0.0)
    spec.update(kw)
    return (CoinSpec("ETH", 3000.0, 0.1, 4, **spec), CoinSpec("SOL", 150.0, 0.01, 2, level_sz=100.0, **spec))


def run(sim, eng, ms, step=100):
    for _ in range(ms // step):
        sim.advance(step)
        eng.step()


def recs(path, kind=None):
    out = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x]
    return [r for r in out if kind is None or r["kind"] == kind]


def mk_sim(tmp_path, policy=None, equity=1e6, tag="a"):
    sim = SimVenue(SimConfig(seed=1, start_equity_usd=equity), coins=quiet())
    eng = Engine(sim, policy or BIG, AuditLog(tmp_path / f"{tag}.jsonl"), state_dir=tmp_path / f"s{tag}")
    return sim, eng


def co(cid, side, size, px, tif):
    return ChildOrder(cid, "i", "ETH", side, size, px, tif)


# --- HL fakes ----------------------------------------------------------------

def cs_of(equity="10000"):
    return {"assetPositions": [], "marginSummary": {"accountValue": equity}, "time": NOW}


def hl_engine(tmp_path, info=None, exchange=(), limiter=None, policy=None):
    base = {"clearinghouseState": cs_of(), "frontendOpenOrders": [], "userFillsByTime": [],
            "userNonFundingLedgerUpdates": [], "orderStatus": {"status": "unknownOid"}}
    base.update(info or {})
    t = FakeTransport(info=base, exchange=exchange)
    clk = FakeClock()
    v = HyperliquidVenue(TESTNET_API_URL, USER, LocalKeySigner(Account.create()), transport=t,
                         limiter=limiter or TokenBucket(clock=clk, sleep=clk.sleep), clock_ms=lambda: NOW,
                         sleep=clk.sleep)
    # the venue clock is frozen at NOW here, so the R2 poll schedule would never re-read: read every step
    eng = Engine(v, policy or PolicyConfig(), AuditLog(tmp_path / "hl.jsonl"), state_dir=tmp_path / "state",
                 poll=None)
    return eng, v, t


def exchange_orders(t):
    return [b["action"] for p, b in t.calls if p == "/exchange" and b["action"]["type"] == "order"]


# =============================================================================== F2
def test_F2_kill_during_rate_limit_wait_aborts_before_send(tmp_path):
    state = tmp_path / "state"
    clk = FakeClock()

    class KillOnExchangeWait(TokenBucket):              # human trips the switch while place() waits
        def acquire(self, weight):
            if weight == hlmod.exchange_weight(1):
                kill_from_outside(state, "human pressed kill")
            return super().acquire(weight)

    eng, v, t = hl_engine(tmp_path, exchange=[ok_order({"filled": {"totalSz": "0.01", "avgPx": "123457", "oid": 5}})],
                          limiter=KillOnExchangeWait(clock=clk, sleep=clk.sleep))
    r = eng.submit(Intent("k1", "BTC", Side.BUY, 0.01, algo=Algo.MARKET))
    assert exchange_orders(t) == [], "an order left the machine after the kill switch was set"
    assert r.state == "killed"
    assert recs(tmp_path / "hl.jsonl", "child_aborted")
    assert recs(tmp_path / "hl.jsonl", "kill")[0]["data"]["source"] == "external"


def test_F2_review_scenario_posts_nothing(tmp_path):
    """The review's own setup (66.5-weight bucket, kill inside sleep), now with the ledger present."""
    state = tmp_path / "state"
    clk = FakeClock()

    def sleep(s):
        kill_from_outside(state, "human pressed kill")
        clk.sleep(s)
    eng, v, t = hl_engine(tmp_path, exchange=[ok_order({"filled": {"totalSz": "0.01", "avgPx": "123457", "oid": 5}})],
                          limiter=TokenBucket(capacity=66.5, clock=clk, sleep=sleep))
    eng.submit(Intent("k1", "BTC", Side.BUY, 0.01, algo=Algo.MARKET))
    eng.step()
    assert exchange_orders(t) == []


def test_F2_adapter_abort_checked_after_signing_and_wait():
    t = FakeTransport()
    clk = FakeClock()
    v = HyperliquidVenue(TESTNET_API_URL, USER, LocalKeySigner(Account.create()), transport=t,
                         limiter=TokenBucket(clock=clk, sleep=clk.sleep), clock_ms=lambda: NOW, sleep=clk.sleep)
    c = ChildOrder("c-1", "i", "BTC", Side.BUY, 0.01, 123456.0, "Ioc")
    with pytest.raises(VenueAborted):
        v.place(c, abort=lambda: True)
    assert not [p for p, _ in t.calls if p == "/exchange"]
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with pytest.raises(VenueAborted):
        sim.place(co("x", Side.BUY, 1.0, 3000.0, "Ioc"), abort=lambda: True)
    assert sim.place_calls == 0 and sim.account().open_orders == ()


def test_F2_ack_after_kill_is_cancelled_at_once(tmp_path):
    sim, eng = mk_sim(tmp_path)

    class KillInFlight:                                  # kill lands while the request is in flight
        def __getattr__(self, k):
            return getattr(sim, k)

        def place(self, child, abort=None):
            oid = sim.place(child, abort=abort)
            kill_from_outside(eng.state_dir, "kill during round trip")
            return oid
    eng.venue = KillInFlight()
    r = eng.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    assert r.state == "killed" and sim.cancel_calls == 1
    sim.advance(1_000)
    assert sim.account().open_orders == () and sim.account().position("ETH") == 0.0


# =============================================================================== F3
class TimeoutAfterAccept:
    def __init__(self, sim):
        self.sim = sim

    def __getattr__(self, k):
        return getattr(self.sim, k)

    def place(self, child):
        self.sim.place(child)
        raise TimeoutError("read timed out")


def test_F3_timeout_after_accept_does_not_overfill(tmp_path):
    sim = SimVenue(SimConfig(seed=1, start_equity_usd=1e6), coins=quiet())
    eng = Engine(TimeoutAfterAccept(sim), BIG, AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, 30_000)
    assert sim.account().position("ETH") == pytest.approx(1.0)
    assert sim.place_calls == 1
    r = eng.status("m1")
    assert r.state == "done" and r.filled_size == pytest.approx(1.0)
    assert recs(tmp_path / "a.jsonl", "child_unknown") and recs(tmp_path / "a.jsonl", "child_resolved")
    assert AuditLog.verify(tmp_path / "a.jsonl")[0]


def test_F3_sim_lost_response_resolved_by_order_status(tmp_path):
    sim, eng = mk_sim(tmp_path)
    sim.lose_next_place_responses(1)
    eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, 10_000)
    assert sim.place_calls == 1 and sim.account().position("ETH") == pytest.approx(1.0)
    assert eng.status("m1").filled_size == pytest.approx(1.0)


def test_F3_request_never_arrived_is_resent_with_same_cloid(tmp_path):
    sim, eng = mk_sim(tmp_path)
    sim.drop_next_places(1)
    eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, UNKNOWN_RESEND_MS - 500)
    assert sim.place_calls == 1                            # nothing resent before resolution
    run(sim, eng, 5_000)
    sent = [x["data"]["child_id"] for x in recs(tmp_path / "a.jsonl", "child_sent")]
    assert sent == ["m1.1", "m1.1"] and sim.place_calls == 2
    assert sim.account().position("ETH") == pytest.approx(1.0)
    assert eng.status("m1").state == "done"


def test_F3_unknown_child_counts_in_exposure(tmp_path):
    pol = PolicyConfig(default_coin_notional_usd=5_000, max_account_notional_usd=1e6, max_child_notional_usd=5_000)
    sim, eng = mk_sim(tmp_path, policy=pol)
    sim.drop_next_places(1)
    eng.submit(Intent("u1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    r = eng.submit(Intent("u2", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    assert r.state == "rejected" and "COIN_NOTIONAL_CAP" in r.block_reasons
    d = recs(tmp_path / "a.jsonl", "child_decision")[-1]["data"]
    assert d["checks"]["coin_worst_case_usd"] == pytest.approx(2 * 3000.0, rel=1e-3)


def test_F3_unknown_is_never_resent_after_kill(tmp_path):
    sim, eng = mk_sim(tmp_path)
    sim.drop_next_places(1)
    eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    eng.kill("stop")
    run(sim, eng, 3 * UNKNOWN_RESEND_MS)
    assert sim.place_calls == 1 and sim.account().position("ETH") == 0.0
    assert eng.status("m1").state == "killed"
    assert recs(tmp_path / "a.jsonl", "intent_done")


def test_F3_unknown_that_lands_after_user_cancel_is_cancelled(tmp_path):
    sim, eng = mk_sim(tmp_path)
    sim.lose_next_place_responses(1)
    eng.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    assert eng.cancel("g1").state == "cancelled"           # no oid yet: cannot cancel blind
    run(sim, eng, 1_500)
    assert sim.account().open_orders == ()
    assert recs(tmp_path / "a.jsonl", "intent_done")


def test_F3_hl_ambiguous_errors_and_order_status(tmp_path):
    def timeout(p):
        raise TimeoutError("read timed out")
    t = FakeTransport(exchange=[timeout, ok_order({"filled": {"totalSz": "0.01", "avgPx": "123457.0", "oid": 9}})])
    clk = FakeClock()
    v = HyperliquidVenue(TESTNET_API_URL, USER, LocalKeySigner(Account.create()), transport=t,
                         limiter=TokenBucket(clock=clk, sleep=clk.sleep), clock_ms=lambda: NOW, sleep=clk.sleep)
    c = ChildOrder("c-1", "i", "BTC", Side.BUY, 0.01, 123456.0, "Ioc")
    with pytest.raises(VenueAmbiguous):
        v.place(c)
    ack = v.place(c)                                       # resend: same cloid on the wire
    assert ack == "9" and ack.filled_sz == 0.01 and ack.avg_px == 123457.0
    a, b = exchange_orders(t)
    assert a["orders"][0]["c"] == b["orders"][0]["c"] == cloid_for("c-1")
    states = {"open": "open", "triggered": "open", "filled": "filled", "canceled": "cancelled",
              "marginCanceled": "cancelled", "rejected": "rejected", "perpMarginRejected": "rejected"}
    for hl, want in states.items():
        t.info["orderStatus"] = {"status": "order", "order": {"order": {"oid": 9, "origSz": "0.01", "sz": "0.004",
                                                                        "cloid": cloid_for("c-1")}, "status": hl}}
        st = v.order_status("c-1")
        assert st["state"] == want and st["oid"] == "9" and st["filled_sz"] == pytest.approx(0.006)
    q = t.calls[-1][1]
    assert q == {"type": "orderStatus", "user": USER, "oid": cloid_for("c-1")}
    assert hlmod.info_weight("orderStatus") == 2
    t.info["orderStatus"] = {"status": "unknownOid"}
    assert v.order_status("c-1")["state"] == "unknown"


def test_F3_requests_transport_5xx_and_timeouts_are_ambiguous():
    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, str(body)

        def json(self):
            return self._body
    tr = RequestsTransport(TESTNET_API_URL, timeout_s=3)
    queue = [Resp(502, "bad gateway"), Resp(422, "bad")]
    tr.session.post = lambda url, json, timeout: queue.pop(0)
    with pytest.raises(VenueAmbiguous, match="HTTP 502"):
        tr("/exchange", {})
    with pytest.raises(VenueError) as ei:
        tr("/exchange", {})
    assert not isinstance(ei.value, VenueAmbiguous)


def test_cloid_scheme_marks_omx_orders():
    c = cloid_for("i-1.1")
    assert c.startswith("0x4f4d5801") and len(c) == 34 and is_omx_cloid(c) and is_omx_cloid("0x" + c[2:].upper())
    assert hlmod.cloid_for is cloid_for and hlmod.is_omx_cloid is is_omx_cloid
    assert not is_omx_cloid("0x" + "ab" * 16) and not is_omx_cloid("") and not is_omx_cloid(None)


# =============================================================================== F4
class Skewed(TimeoutAfterAccept):
    def place(self, child):
        return self.sim.place(child)

    def now_ms(self):
        return self.sim.now_ms() + 1_500


def test_F4_local_clock_ahead_keeps_every_fill(tmp_path):
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    eng = Engine(Skewed(sim), PolicyConfig(), AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    eng.submit(Intent("t1", "ETH", Side.BUY, 1.0, algo=Algo.TWAP, duration_s=10, slices=2))
    run(sim, eng, 15_000)
    pos = sim.account().position("ETH")
    assert pos == pytest.approx(1.0) and eng.status("t1").filled_size == pytest.approx(pos)
    fills = recs(tmp_path / "a.jsonl", "fill")
    assert len(fills) == len(sim.fills_since(0))           # overlapping cursor, each fill once
    assert len({f["data"]["fill"]["fill_id"] for f in fills}) == len(fills)


def test_F4_place_response_fill_applied_and_deduped_by_tid(tmp_path):
    rows = []
    fill = {"coin": "BTC", "px": "123457.0", "sz": "0.01", "side": "B", "time": NOW + 5, "fee": "0.5",
            "crossed": True, "oid": 5, "tid": 77, "cloid": cloid_for("f1.1"), "hash": "0x1"}
    status = {"status": "order", "order": {"order": {"oid": 5, "origSz": "0.01", "sz": "0.0"}, "status": "filled"}}
    eng, v, t = hl_engine(tmp_path, info={"userFillsByTime": lambda p: list(rows), "orderStatus": status},
                          exchange=[ok_order({"filled": {"totalSz": "0.01", "avgPx": "123457", "oid": 5}})])
    r = eng.submit(Intent("f1", "BTC", Side.BUY, 0.01, algo=Algo.MARKET))
    assert r.filled_size == pytest.approx(0.01)            # from the place response, before any fill row
    eng.step()
    assert eng.status("f1").state == "done"
    rows.append(fill)
    for _ in range(3):                                     # the same row comes back on every poll
        eng.step()
    r = eng.status("f1")
    assert r.filled_size == pytest.approx(0.01) and r.avg_px == pytest.approx(123457.0)
    assert len(recs(tmp_path / "hl.jsonl", "fill")) == 1
    assert len(exchange_orders(t)) == 1


# =============================================================================== F6
def test_F6_deposit_does_not_mask_daily_loss_on_hyperliquid(tmp_path):
    equity = {"v": "10000"}
    ledger = [{"time": NOW - 1, "hash": "0x1", "delta": {"type": "deposit", "usdc": "1000.0"}}]
    eng, v, t = hl_engine(tmp_path, info={"clearinghouseState": lambda p: cs_of(equity["v"]),
                                          "userNonFundingLedgerUpdates": ledger},
                          policy=PolicyConfig(max_daily_loss_usd=500))
    eng.step()
    equity["v"] = "10400"                                  # lost 600, then deposited 1000
    eng.step()
    assert eng._acct.day_pnl_usd() == pytest.approx(-600.0)
    q = [b for p, b in t.calls if p == "/info" and b["type"] == "userNonFundingLedgerUpdates"][-1]
    assert q["user"] == USER and q["startTime"] < NOW
    r = eng.submit(Intent("d1", "BTC", Side.BUY, 0.01, algo=Algo.MARKET))
    assert r.state == "rejected" and "DAILY_LOSS_LIMIT" in r.block_reasons


def test_F6_hl_ledger_types_to_perp_flows(tmp_path):
    other = "0x" + "cd" * 20
    d = lambda **kw: {"time": NOW, "hash": "0x", "delta": kw}
    ledger = [d(type="deposit", usdc="1000"), d(type="withdraw", usdc="200", nonce=1, fee="1"),
              d(type="internalTransfer", usdc="50", user=other, destination=USER, fee="0"),
              d(type="internalTransfer", usdc="30", user=USER, destination=other, fee="1"),
              d(type="subAccountTransfer", usdc="7", user=USER, destination=other),
              d(type="accountClassTransfer", usdc="100", toPerp=True),
              d(type="accountClassTransfer", usdc="40", toPerp=False),
              d(type="vaultDeposit", vault=other, usdc="10"),
              d(type="spotTransfer", token="PURR", amount="5", usdcValue="5", user=other, destination=USER, fee="0"),
              d(type="liquidation", liquidatedNtlPos="1", accountValue="1", leverageType="Cross")]
    eng, v, t = hl_engine(tmp_path, info={"userNonFundingLedgerUpdates": ledger})
    assert v.external_flows_usd(NOW - 1) == pytest.approx(1000 - 200 + 50 - 30 - 7 + 100 - 40 - 10)


def test_F6_flows_around_the_day_start_read(tmp_path):
    from omx.engine import FLOW_GRACE_MS
    ledger = [{"time": NOW - FLOW_GRACE_MS - 1, "hash": "a", "delta": {"type": "deposit", "usdc": "500"}},
              {"time": NOW - 5, "hash": "b", "delta": {"type": "withdraw", "usdc": "300"}},
              {"time": NOW + 1, "hash": "c", "delta": {"type": "withdraw", "usdc": "100"}}]
    eng, v, t = hl_engine(tmp_path, info={"userNonFundingLedgerUpdates": ledger})
    eng.step()
    # old deposit already in the day-start equity; a withdrawal just before the read is ambiguous
    # and left out (lower P&L reading); the withdrawal after the read counts
    assert eng._acct.day_net_flows_usd == pytest.approx(-100.0)


def test_F6_sim_deposit_and_withdraw_kept_out_of_day_pnl(tmp_path):
    sim, eng = mk_sim(tmp_path, policy=PolicyConfig(max_daily_loss_usd=50.0, **{
        "default_coin_notional_usd": 50_000, "max_account_notional_usd": 100_000, "max_child_notional_usd": 20_000}),
        equity=10_000.0)
    eng.step()
    eng.submit(Intent("d1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, 500)
    sim.shock_mid("ETH", -300.0)                           # ~ -89 USD
    sim.advance(100)
    sim.deposit(1_000.0)
    sim.advance(100)
    r = eng.submit(Intent("d2", "ETH", Side.BUY, 0.1, algo=Algo.MARKET))
    assert r.state == "rejected" and "DAILY_LOSS_LIMIT" in r.block_reasons
    view = eng.account_view()
    assert view["day_net_flows_usd"] == pytest.approx(1_000.0)
    assert view["day_pnl_usd"] == pytest.approx(view["equity_usd"] - 10_000.0 - 1_000.0)
    assert view["day_pnl_usd"] < -50.0
    sim.withdraw(400.0)
    sim.advance(100)
    assert eng.account_view()["day_net_flows_usd"] == pytest.approx(600.0)


# =============================================================================== F7
def test_F7_other_coin_position_valued_at_mark(tmp_path):
    sim = SimVenue(SimConfig(seed=1, start_equity_usd=1e6), coins=quiet())
    sim._pos["SOL"] = [100.0, 150.0]
    sim.shock_mid("SOL", 4055)                             # SOL ~ +50 %
    pol = PolicyConfig(default_coin_notional_usd=1e5, max_account_notional_usd=25_000, max_child_notional_usd=1e5,
                       max_leverage=50)
    eng = Engine(sim, pol, AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s")
    eng.submit(Intent("e1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    d = recs(tmp_path / "a.jsonl", "child_decision")[0]["data"]
    assert not d["allow"] and "ACCOUNT_NOTIONAL_CAP" in d["reasons"]
    assert d["checks"]["account_worst_case_usd"] == pytest.approx(100 * sim._mid["SOL"] + 3000.0, rel=1e-3)
    assert sim.place_calls == 0


def test_F7_hl_marks_for_all_coins_and_policy_blocks_without_mark(tmp_path):
    from omx.policy import evaluate
    from omx.types import AccountState, BookLevel, MarketSnapshot, Position
    eng, v, t = hl_engine(tmp_path)
    marks = v.mark_prices()
    assert marks["ETH"] == 1891.4 and marks["SOL"] == 150.25 and len(marks) == len(META[0]["universe"])
    snap = MarketSnapshot("ETH", NOW, (BookLevel(99.99, 1e5),), (BookLevel(100.01, 1e5),), day_ntl_usd=1e9)
    acct = AccountState(NOW, 1e6, (Position("SOL", 10.0, 150.0),), ())
    child = ChildOrder("c", "i", "ETH", Side.BUY, 1.0, 100.01, "Ioc")
    it = Intent("i", "ETH", Side.BUY, 1.0)
    d = evaluate(child, it, snap, acct, PolicyConfig(), NOW, False, {})
    assert "NO_MARK:SOL" in d.reasons
    assert evaluate(child, it, snap, acct, PolicyConfig(), NOW, False, {"SOL": 150.0}).allow


# =============================================================================== kill scope / orphans / views
def test_kill_scope_omx_and_all_cancel_venue_orders_not_in_memory(tmp_path):
    sim = SimVenue(SimConfig(seed=1, start_equity_usd=1e6), coins=quiet())
    first = Engine(sim, BIG, AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s1")
    first.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    sim.place_foreign(co("manual", Side.BUY, 1.0, 2980.0, "Gtc"))
    sim.advance(500)
    second = Engine(sim, BIG, AuditLog(tmp_path / "b.jsonl"), state_dir=tmp_path / "s2")   # knows nothing
    res = second.kill("stop", scope="omx")
    assert res["cancels_requested"] == 1 and res["scope"] == "omx"
    sim.advance(1_000)
    assert [o.child_id for o in sim.account().open_orders] == ["manual"]
    res = second.kill("stop all", scope="all")
    sim.advance(1_000)
    assert res["cancels_requested"] == 1 and sim.account().open_orders == ()
    with pytest.raises(ValueError):
        second.kill("x", scope="everything")
    assert AuditLog.verify(tmp_path / "b.jsonl")[0]


def test_orphans_from_previous_run_cancelled_at_start(tmp_path):
    sim = SimVenue(SimConfig(seed=1, start_equity_usd=1e6), coins=quiet())
    first = Engine(sim, BIG, AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s1")
    first.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    sim.place_foreign(co("manual", Side.BUY, 1.0, 2980.0, "Gtc"))
    sim.advance(500)
    # the restart of the same instance (same state_dir, so the same cloid instance tag, R3-5)
    second = Engine(sim, BIG, AuditLog(tmp_path / "b.jsonl"), state_dir=tmp_path / "s1")
    second.step()
    sim.advance(1_000)
    assert [o.child_id for o in sim.account().open_orders] == ["manual"]   # foreign orders untouched
    o = recs(tmp_path / "b.jsonl", "orphans_cancelled")
    assert o and o[0]["data"]["count"] == 1 and o[0]["data"]["orders"][0]["child_id"] == "g1.1"


def test_kill_file_present_at_start_sweeps_account(tmp_path):
    sim = SimVenue(SimConfig(seed=1, start_equity_usd=1e6), coins=quiet())
    first = Engine(sim, BIG, AuditLog(tmp_path / "a.jsonl"), state_dir=tmp_path / "s1")
    first.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    sim.advance(500)
    kill_from_outside(tmp_path / "s2", "stopped while down")
    second = Engine(sim, BIG, AuditLog(tmp_path / "b.jsonl"), state_dir=tmp_path / "s2")
    second.step()
    sim.advance(1_000)
    assert sim.account().open_orders == ()
    k = recs(tmp_path / "b.jsonl", "kill")
    assert k and k[0]["data"]["reason"] == "stopped while down" and k[0]["data"]["cancels_requested"] == 1


def test_kill_file_reread_every_step_also_cancels_unknown_orders(tmp_path):
    sim, eng = mk_sim(tmp_path)
    eng.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    sim.place(co("stray", Side.BUY, 1.0, 2985.0, "Gtc"))  # OMX cloid, unknown to this engine
    run(sim, eng, 500)
    assert len(sim.account().open_orders) == 2
    kill_from_outside(eng.state_dir, "cli")
    eng.step()
    sim.advance(1_000)
    assert sim.account().open_orders == ()


def test_views(tmp_path):
    sim, eng = mk_sim(tmp_path)
    eng.submit(Intent("g1", "ETH", Side.BUY, 1.0, algo=Algo.LIMIT, limit_px=2990.0))
    eng.submit(Intent("m1", "ETH", Side.BUY, 1.0, algo=Algo.MARKET))
    run(sim, eng, 500)
    s = eng.snapshot("ETH")
    assert s.coin == "ETH" and s.best_bid == 2999.9
    v = eng.account_view()
    assert v["positions"][0]["coin"] == "ETH" and v["positions"][0]["mark_px"] == pytest.approx(3000.0, rel=1e-3)
    assert [o["child_id"] for o in v["open_orders"]] == ["g1.1"]
    assert v["day_start_equity_usd"] == 1e6 and v["day_net_flows_usd"] == 0.0
    assert v["day_pnl_usd"] == pytest.approx(v["equity_usd"] - 1e6)
    assert v["killed"] is False
    json.dumps(v)
