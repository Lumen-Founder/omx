"""Hyperliquid account abstraction modes: equity per mode, refusal of unsupported modes, kill in every
mode, equity-basis changes in day P&L, doctor output, and the read-only measurement tool (offline)."""
import importlib.util
import io
import json
import time
from pathlib import Path

import pytest
from eth_account import Account

from omx import account_mode as AM
from omx.audit import AuditLog
from omx.config import render
from omx.doctor import checks
from omx.engine import DAY_MS, Engine, PollSchedule
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.base import VenueError, cloid_for, instance_tag
from omx.venue.hyperliquid import TESTNET_API_URL, HyperliquidVenue, LocalKeySigner, TokenBucket

from test_doctor import BUILDER, FakeInfo, by_name
from test_doctor import setup  # noqa: F401  (fixture)
from test_hyperliquid import BOOK, NOW, USER, FakeClock, FakeTransport, ok_order

CANCEL_OK = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
FILLED = ok_order({"filled": {"totalSz": "0.01", "avgPx": "123457.0", "oid": 9}})
UNV = AM.EQUITY_BASIS_UNVERIFIED

SPEC = importlib.util.spec_from_file_location("measure_account_mode",
                                              Path(__file__).resolve().parents[1] / "tools" / "measure_account_mode.py")
measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def pos(coin="BTC", szi="0.01", upnl="-50.0", margin="400.0", kind="cross"):
    return {"type": "oneWay", "position": {"coin": coin, "szi": szi, "entryPx": "123456.0", "positionValue": "1234.56",
                                           "leverage": {"type": kind, "value": 3}, "unrealizedPnl": upnl,
                                           "marginUsed": margin}}


def spot_of(total, hold="0.0"):
    return {"balances": [{"coin": "USDC", "token": 0, "hold": hold, "total": total, "entryNtl": "0.0"},
                         {"coin": "PURR", "token": 1, "hold": "0.0", "total": "5.0", "entryNtl": "1.0"}]}


def hl(tmp_path, st, exchange=(), policy=None, state="s"):
    """Real Engine on the real HL adapter; st holds the fake account (mode, perp value, spot total, positions)."""
    clk = FakeClock()
    now_ms = lambda: NOW + round((clk.t - 1000) * 1000)            # noqa: E731
    info = {"userAbstraction": lambda p: st["mode"],
            "clearinghouseState": lambda p: {"assetPositions": list(st.get("pos", [])), "time": now_ms(),
                                             "marginSummary": {"accountValue": st.get("perp", "0.0")},
                                             "crossMarginSummary": {"accountValue": st.get("perp", "0.0")}},
            "spotClearinghouseState": lambda p: spot_of(st.get("spot", "0.0")),
            "frontendOpenOrders": lambda p: list(st.get("oo", [])), "userFillsByTime": [],
            "userNonFundingLedgerUpdates": [], "l2Book": lambda p: {**BOOK, "time": now_ms() - 100},
            "orderStatus": {"status": "unknownOid"}}
    t = FakeTransport(info=info, exchange=exchange)
    v = HyperliquidVenue(TESTNET_API_URL, USER, LocalKeySigner(Account.create()), transport=t,
                         limiter=TokenBucket(clock=clk, sleep=clk.sleep), clock_ms=now_ms, sleep=clk.sleep)
    eng = Engine(v, policy or PolicyConfig(min_depth_usd=0.0), AuditLog(tmp_path / "a.jsonl"),
                 state_dir=tmp_path / state)
    return eng, v, clk, t


def market(iid="hu-1", reduce_only=False, side=Side.BUY):
    return Intent(iid, "BTC", side, 0.01, algo=Algo.MARKET, max_slippage_bps=30.0, reduce_only=reduce_only)


def sent(t):
    return [b["action"] for path, b in t.calls if path == "/exchange"]


def recs(path, kind):
    if not path.exists():                                                     # nothing audited yet
        return []
    return [r for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()) if r["kind"] == kind]


# ---------------------------------------------------------------- equity basis (pure)
def test_unified_equity_takes_the_lower_candidate_and_flags_it():
    lose = AM.unified_equity(spot_of("1000.0", "12.5"), {"assetPositions": [pos(upnl="-50.0")]})
    assert lose["equity_usd"] == pytest.approx(950.0) and lose["flags"] == (UNV,)
    assert lose["basis"] == AM.BASIS_UNIFIED_CONSERVATIVE and lose["spot_usdc_hold"] == 12.5
    assert lose["candidates"] == {AM.BASIS_SPOT_TOTAL: 1000.0, AM.BASIS_SPOT_PLUS_UPNL: 950.0}
    win = AM.unified_equity(spot_of("1000.0"), {"assetPositions": [pos(upnl="30.0"), pos("ETH", upnl="5.0")]})
    assert win["equity_usd"] == pytest.approx(1000.0) and win["perp_upnl_usd"] == pytest.approx(35.0)
    flat = AM.unified_equity(spot_of("743.877"), {"assetPositions": [pos(szi="0", upnl="99")]})
    assert flat["equity_usd"] == pytest.approx(743.877)                       # closed rows carry no P&L
    assert AM.unified_equity({"balances": []}, {})["equity_usd"] == 0.0       # no USDC row: nothing


def test_unified_equity_after_a_measurement_drops_the_flag():
    a = AM.unified_equity(spot_of("1000"), {"assetPositions": [pos(upnl="-50")]}, verified=AM.BASIS_SPOT_TOTAL)
    b = AM.unified_equity(spot_of("1000"), {"assetPositions": [pos(upnl="-50")]}, verified=AM.BASIS_SPOT_PLUS_UPNL)
    assert (a["equity_usd"], a["flags"]) == (1000.0, ()) and (b["equity_usd"], b["flags"]) == (950.0, ())
    assert AM.VERIFIED_UNIFIED_BASIS is None                                  # not measured yet: fail closed


@pytest.mark.parametrize("spot,perp", [({}, {}), ({"balances": [{"coin": "USDC", "total": "nan"}]}, {}),
                                       (spot_of("1"), {"assetPositions": [pos(upnl="x")]})])
def test_unified_equity_refuses_malformed_input(spot, perp):
    with pytest.raises(ValueError):
        AM.unified_equity(spot, perp)


# ---------------------------------------------------------------- adapter
@pytest.mark.parametrize("mode,equity,basis,flags", [
    ("disabled", 1000.0, AM.BASIS_PERP, ()),
    ("default", 1000.0, AM.BASIS_PERP, ()),
    ("unifiedAccount", 5950.0, AM.BASIS_UNIFIED_CONSERVATIVE, (UNV,)),
    ("portfolioMargin", 1000.0, AM.BASIS_PERP, (AM.ACCOUNT_MODE_UNSUPPORTED,)),
    ("dexAbstraction", 1000.0, AM.BASIS_PERP, (AM.ACCOUNT_MODE_UNSUPPORTED,)),
])
def test_adapter_equity_per_mode_positions_from_the_perp_state(tmp_path, mode, equity, basis, flags):
    st = {"mode": mode, "perp": "1000.0", "spot": "6000.0", "pos": [pos(upnl="-50.0")]}
    _, v, _, t = hl(tmp_path, st)
    a = v.account_core()
    assert (a.account_mode, a.equity_usd, a.equity_basis, a.equity_flags) == (mode, pytest.approx(equity), basis, flags)
    assert a.position("BTC") == pytest.approx(0.01)                           # positions: clearinghouseState
    assert t.count("spotClearinghouseState") == (1 if mode == "unifiedAccount" else 0)


def test_adapter_reads_the_mode_at_start_then_every_10_minutes(tmp_path):
    st = {"mode": "unifiedAccount", "spot": "100"}
    _, v, clk, t = hl(tmp_path, st)
    v.account_core()
    clk.t += 599
    v.account_core()
    assert t.count("userAbstraction") == 1
    st["mode"] = "portfolioMargin"
    clk.t += 2
    assert v.account_core().account_mode == "portfolioMargin" and t.count("userAbstraction") == 2


def test_adapter_unreadable_mode_is_unknown_and_retried_after_a_minute(tmp_path):
    st = {"mode": VenueError("HTTP 500"), "perp": "10"}
    _, v, clk, t = hl(tmp_path, st)
    a = v.account_core()                                                      # the read itself still works
    assert a.account_mode == AM.UNKNOWN_MODE and a.equity_flags == (AM.ACCOUNT_MODE_UNSUPPORTED,)
    clk.t += 30
    v.account_core()
    assert t.count("userAbstraction") == 1
    st["mode"] = "disabled"
    clk.t += 31
    assert v.account_core().account_mode == "disabled" and t.count("userAbstraction") == 2
    st["mode"] = 42                                                           # malformed answer
    assert v.account_mode(force=True) == AM.UNKNOWN_MODE


# ---------------------------------------------------------------- engine: refuse / allow / kill
@pytest.mark.parametrize("mode", ["portfolioMargin", "dexAbstraction", "somethingNew", VenueError("HTTP 500")])
def test_unsupported_mode_refuses_new_intents(tmp_path, mode):
    eng, _, _, t = hl(tmp_path, {"mode": mode, "perp": "10000", "spot": "10000"}, exchange=[FILLED])
    eng.step()
    rep = eng.submit(market())
    assert rep.state == "rejected" and rep.message.startswith(AM.ACCOUNT_MODE_UNSUPPORTED), rep
    assert sent(t) == []
    view = eng.account_view()
    assert view["account_mode_block"].startswith(AM.ACCOUNT_MODE_UNSUPPORTED)
    assert AM.ACCOUNT_MODE_UNSUPPORTED in view["equity_flags"]


@pytest.mark.parametrize("mode", ["disabled", "default", "unifiedAccount"])
def test_supported_modes_trade(tmp_path, mode):
    eng, _, _, t = hl(tmp_path, {"mode": mode, "perp": "10000" if mode != "unifiedAccount" else "0.0",
                                 "spot": "10000"}, exchange=[FILLED])
    eng.step()
    rep = eng.submit(market())
    assert [a["type"] for a in sent(t)] == ["order"], rep
    view = eng.account_view()
    assert view["equity_usd"] == pytest.approx(10000.0) and view["account_mode"] == mode
    assert view["account_mode_block"] is None
    assert view["equity_flags"] == ([UNV] if mode == "unifiedAccount" else [])
    assert view["equity_basis"] == (AM.BASIS_UNIFIED_CONSERVATIVE if mode == "unifiedAccount" else AM.BASIS_PERP)


def test_unified_losing_position_lowers_equity_for_the_policy(tmp_path):
    st = {"mode": "unifiedAccount", "spot": "1000", "pos": [pos(upnl="-200.0")]}
    eng, _, _, _ = hl(tmp_path, st, policy=PolicyConfig(min_depth_usd=0.0, max_leverage=1.5))
    eng.step()
    assert eng.account_view()["equity_usd"] == pytest.approx(800.0)
    p = eng.preview(market())                                                 # 2 x 1234 / 800 > 1.5
    assert any(r.startswith("LEVERAGE_CAP") for r in p["children"][0]["reasons"])


def test_policy_blocks_every_child_in_an_unsupported_mode_reduce_only_included(tmp_path):
    st = {"mode": "portfolioMargin", "perp": "10000", "pos": [pos(upnl="0")]}
    eng, _, _, _ = hl(tmp_path, st)
    eng.step()
    p = eng.preview(market(reduce_only=True, side=Side.SELL))
    assert "ACCOUNT_MODE_UNSUPPORTED:portfolioMargin" in p["children"][0]["reasons"]


def test_switch_to_an_unsupported_mode_mid_run_refuses_after_the_next_mode_read(tmp_path):
    st = {"mode": "disabled", "perp": "10000"}
    eng, _, clk, t = hl(tmp_path, st, exchange=[FILLED])
    eng.step()
    assert eng.submit(market("a")).state != "rejected"
    st["mode"] = "portfolioMargin"
    clk.t += 601
    eng.step()
    rep = eng.submit(market("b"))
    assert rep.state == "rejected" and rep.message == "ACCOUNT_MODE_UNSUPPORTED:portfolioMargin"
    assert len(sent(t)) == 1


@pytest.mark.parametrize("mode", ["portfolioMargin", "dexAbstraction", VenueError("HTTP 500"), "unifiedAccount"])
def test_kill_cancels_omx_orders_in_every_mode(tmp_path, mode):
    other = cloid_for("old.1", instance_tag(tmp_path / "other"))              # not swept at start: kill only
    st = {"mode": mode, "perp": "10000", "spot": "10000",
          "oo": [{"coin": "BTC", "side": "B", "sz": "0.01", "limitPx": "120000", "oid": 7, "cloid": other,
                  "reduceOnly": False}]}
    eng, _, _, t = hl(tmp_path, st, exchange=[CANCEL_OK])
    eng.step()
    assert sent(t) == []
    eng.kill("stop")
    assert [a["type"] for a in sent(t)] == ["cancel"] and sent(t)[0]["cancels"][0]["o"] == 7


# ---------------------------------------------------------------- equity basis changes in day P&L
def test_switch_standard_to_unified_books_the_rise_not_as_pnl(tmp_path):
    st = {"mode": "disabled", "perp": "1000", "spot": "5000"}               # spot 5000 sits outside perps
    eng, _, clk, _ = hl(tmp_path, st)
    eng.step()
    st.update(mode="unifiedAccount", spot="6000")                            # one balance now: 6000
    clk.t += 601
    eng.step()
    v = eng.account_view()
    assert v["equity_usd"] == pytest.approx(6000.0) and v["day_pnl_usd"] == pytest.approx(0.0)
    ch = recs(tmp_path / "a.jsonl", "equity_basis_change")
    assert len(ch) == 1 and ch[0]["data"]["booked_as_flow_usd"] == pytest.approx(5000.0)
    st["spot"] = "5900"                                                       # a real loss afterwards counts
    clk.t += 3
    eng.step()
    assert eng.account_view()["day_pnl_usd"] == pytest.approx(-100.0)
    # the adjustment survives a restart (day file)
    eng2, _, _, _ = hl(tmp_path, st)
    eng2.step()
    assert eng2.account_view()["day_pnl_usd"] == pytest.approx(-100.0)
    d = json.loads((tmp_path / "s" / "engine_day.json").read_text(encoding="utf-8"))
    assert d["basis"] == AM.BASIS_UNIFIED_CONSERVATIVE and d["basis_adj"] == pytest.approx(5000.0)


def test_switch_unified_to_standard_keeps_the_fall_in_day_pnl(tmp_path):
    st = {"mode": "unifiedAccount", "perp": "0", "spot": "6000"}
    eng, _, clk, t = hl(tmp_path, st, exchange=[FILLED])
    eng.step()
    st.update(mode="disabled", perp="1000")                                   # spot 5000 left the equity
    clk.t += 601
    eng.step()
    assert eng.account_view()["day_pnl_usd"] == pytest.approx(-5000.0)       # conservative: limit trips
    eng.submit(market())
    assert sent(t) == []
    assert any("DAILY_LOSS_LIMIT" in r["data"]["reasons"] for r in recs(tmp_path / "a.jsonl", "child_decision"))


def test_legacy_day_file_from_a_zero_perp_read_does_not_turn_the_unified_balance_into_pnl(tmp_path):
    """The 2026-11-05 E2E situation: v1 took the day start from the perp value (0) of a unified account."""
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "engine_day.json").write_text(json.dumps({"day": NOW // DAY_MS, "equity_usd": 0.0,
                                                               "ts_ms": NOW - 60_000}), encoding="utf-8")
    eng, _, _, _ = hl(tmp_path, {"mode": "unifiedAccount", "spot": "743.877"})
    eng.step()
    v = eng.account_view()
    assert v["equity_usd"] == pytest.approx(743.877) and v["day_pnl_usd"] == pytest.approx(0.0)


def test_legacy_day_file_in_the_perp_basis_is_not_a_change(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "engine_day.json").write_text(json.dumps({"day": NOW // DAY_MS, "equity_usd": 1100.0,
                                                               "ts_ms": NOW - 60_000}), encoding="utf-8")
    eng, _, _, _ = hl(tmp_path, {"mode": "disabled", "perp": "1000"})
    eng.step()
    assert eng.account_view()["day_pnl_usd"] == pytest.approx(-100.0)
    assert recs(tmp_path / "a.jsonl", "equity_basis_change") == []


# ---------------------------------------------------------------- rate plan
def test_unified_idle_reads_fit_the_unified_plan(tmp_path):
    eng, _, clk, t = hl(tmp_path, {"mode": "unifiedAccount", "spot": "10000"})
    eng.step()
    t.calls.clear()
    for _ in range(300):                                                      # one minute of 200 ms steps
        clk.t += 0.2
        eng.step()
    w = {"clearinghouseState": 2, "spotClearinghouseState": 2, "frontendOpenOrders": 20, "userFillsByTime": 20,
         "userNonFundingLedgerUpdates": 20, "metaAndAssetCtxs": 20, "l2Book": 2, "userAbstraction": 20}
    per_min = sum(w[b["type"]] for path, b in t.calls if path == "/info")
    assert t.count("spotClearinghouseState") == t.count("clearinghouseState") == 30
    assert per_min == 560 <= PollSchedule().planned(False, unified=True) == 642 <= PollSchedule().budget_per_min


# ---------------------------------------------------------------- doctor
def unified_info(account, agent, mode="unifiedAccount", upnl="-50.0"):
    return FakeInfo(account, agent, {
        ("userAbstraction", account.lower()): mode,
        ("clearinghouseState", account.lower()): {"marginSummary": {"accountValue": "0.0"},
                                                  "assetPositions": [pos(upnl=upnl)]},
        ("spotClearinghouseState", account.lower()): spot_of("743.877")})


def test_doctor_unified_ok_with_basis_and_unverified_flag(setup):  # noqa: F811
    p, account, agent, _, _ = setup
    res = by_name(checks(p, unified_info(account, agent).factory))
    m, v = res["account mode"], res["account value"]
    assert m.status == "OK" and "unifiedAccount" in m.detail and UNV in m.detail
    assert AM.BASIS_UNIFIED_CONSERVATIVE in m.detail and "693.88" in m.detail and "743.88" in m.detail
    assert v.status == "OK" and "693.88" in v.detail and UNV in v.detail     # 743.877 - 50
    assert res["builder account mode"].status == "OK"                         # builder check unchanged


def test_doctor_unified_with_nothing_fails_the_value_check(setup):  # noqa: F811
    p, account, agent, _, _ = setup
    fake = unified_info(account, agent)
    fake.r[("spotClearinghouseState", account.lower())] = {"balances": []}
    res = by_name(checks(p, fake.factory))
    assert res["account mode"].status == "OK" and res["account value"].status == "FAIL"


@pytest.mark.parametrize("mode", ["portfolioMargin", "dexAbstraction", "somethingNew"])
def test_doctor_unsupported_modes_fail_with_the_fix(setup, mode):  # noqa: F811
    p, account, agent, _, _ = setup
    res = by_name(checks(p, FakeInfo(account, agent, {("userAbstraction", account.lower()): mode}).factory))
    m = res["account mode"]
    assert m.status == "FAIL" and AM.ACCOUNT_MODE_UNSUPPORTED in m.detail
    assert "Unified or Manual / Standard" in m.fix


@pytest.mark.parametrize("mode", ["disabled", "default"])
def test_doctor_standard_modes_ok(setup, mode):  # noqa: F811
    p, account, agent, _, _ = setup
    res = by_name(checks(p, FakeInfo(account, agent, {("userAbstraction", account.lower()): mode}).factory))
    assert res["account mode"].status == "OK" and res["account value"].detail == "perp account value 743.88 USDC"


# ---------------------------------------------------------------- measurement tool
class Seq:
    """Fake /info for the measurement tool: one (spot total, upnl) pair per sample."""

    def __init__(self, rows, mode="unifiedAccount"):
        self.rows, self.mode, self.i, self.calls = list(rows), mode, -1, []

    def __call__(self, path, payload):
        assert path == "/info"
        self.calls.append(payload["type"])
        if payload["type"] == "userAbstraction":
            self.i += 1
            return self.mode
        total, upnl = self.rows[self.i]
        if payload["type"] == "spotClearinghouseState":
            return spot_of(str(total), "1.5")
        if payload["type"] == "clearinghouseState":
            return {"marginSummary": {"accountValue": "0.0", "totalMarginUsed": "0.0"},
                    "crossMarginSummary": {"accountValue": "0.0"}, "crossMaintenanceMarginUsed": "4.1",
                    "withdrawable": "0.0", "assetPositions": [pos(upnl=str(upnl), margin="411.5")] if upnl is not None else []}
        raise AssertionError(payload)


def run_measure(tmp_path, fake, *args):
    cfg = tmp_path / "omx.toml"
    # the key path points at a file that does not exist: the tool must never need it
    cfg.write_text(render(USER, "testnet", key_path=str(tmp_path / "no_such.key")), encoding="utf-8")
    out, slept = io.StringIO(), []
    code = measure.main(["--config", str(cfg), *args], transport=fake, out=out, sleep=slept.append)
    return code, out.getvalue(), slept


def test_measure_prints_every_field_without_a_key(tmp_path):
    fake = Seq([(1000.0, -10.0)])
    code, out, slept = run_measure(tmp_path, fake)
    assert code == 0 and slept == []
    for s in ("userAbstraction: 'unifiedAccount'", "USDC", "total 1000.0", "hold 1.5", "marginSummary",
              "crossMarginSummary", "crossMaintenanceMarginUsed: 4.1", "unrealizedPnl -10.0", "marginUsed 411.5",
              "candidate equity A (spot_usdc_total): 1000.000000",
              "candidate equity B (spot_usdc_total_plus_upnl): 990.000000", UNV, "no key is read"):
        assert s in out, (s, out)
    assert set(fake.calls) == {"userAbstraction", "spotClearinghouseState", "clearinghouseState"}


def test_measure_verdict_total_moves_with_upnl(tmp_path):
    code, out, slept = run_measure(tmp_path, Seq([(1000.0, -10.0), (990.0, -20.0), (1005.0, -5.0)]),
                                   "--samples", "3", "--interval", "20")
    assert code == 0 and slept == [20.0, 20.0]
    assert "basis spot_usdc_total (candidate A)" in out


def test_measure_verdict_total_ignores_upnl(tmp_path):
    _, out, _ = run_measure(tmp_path, Seq([(1000.0, -10.0), (1000.0, -20.0), (1000.0, 3.0)]), "--samples", "3")
    assert "basis spot_usdc_total_plus_upnl (candidate B)" in out


def test_measure_inconclusive_without_a_moving_position(tmp_path):
    _, out, _ = run_measure(tmp_path, Seq([(1000.0, None), (1000.0, None)]), "--samples", "2")
    assert "INCONCLUSIVE" in out and "open a small position" in out
    _, out, _ = run_measure(tmp_path, Seq([(1000.0, -10.0), (995.0, -20.0)]), "--samples", "2")
    assert "INCONCLUSIVE" in out                                              # ratio 0.5: funding / fill noise


def test_measure_standard_and_unsupported_modes(tmp_path):
    _, out, _ = run_measure(tmp_path, Seq([(10.0, None)], mode="disabled"))
    assert "perp_account_value" in out and "standard account" in out
    _, out, _ = run_measure(tmp_path, Seq([(10.0, None)], mode="portfolioMargin"))
    assert AM.ACCOUNT_MODE_UNSUPPORTED in out
