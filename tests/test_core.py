import json

import pytest

from omx.audit import AuditLog
from omx.eligibility import Attestation, check
from omx.policy import PolicyConfig, evaluate
from omx.types import (AccountState, Algo, BookLevel, ChildOrder, Intent, MarketSnapshot,
                       OpenOrder, Position, Side)

NOW = 1_800_000_000_000


def snap(mid=100.0, ts=NOW, depth=10_000.0, day=1e9, coin="ETH"):
    return MarketSnapshot(coin=coin, ts_ms=ts,
                          bids=(BookLevel(mid - 0.01, depth),), asks=(BookLevel(mid + 0.01, depth),),
                          day_ntl_usd=day)


def intent(**kw):
    base = dict(intent_id="i1", coin="ETH", side=Side.BUY, size=1.0, algo=Algo.TWAP)
    base.update(kw)
    return Intent(**base)


def child(size=1.0, px=100.01, side=Side.BUY, coin="ETH", reduce_only=False):
    return ChildOrder("c1", "i1", coin, side, size, px, "Ioc", reduce_only)


def acct(eq=10_000.0, positions=(), orders=(), day_start=None):
    return AccountState(NOW, eq, tuple(positions), tuple(orders), day_start)


CFG = PolicyConfig()


def test_clean_order_passes():
    d = evaluate(child(), intent(), snap(), acct(), CFG, NOW, killed=False)
    assert d.allow, d.reasons


@pytest.mark.parametrize("kw,reason", [
    (dict(killed=True), "KILL_SWITCH_ACTIVE"),
])
def test_kill(kw, reason):
    d = evaluate(child(), intent(), snap(), acct(), CFG, NOW, **kw)
    assert reason in d.reasons


def test_allowlist_and_stale_and_liquidity():
    d = evaluate(child(coin="DOGE"), intent(coin="DOGE"), snap(coin="DOGE", ts=NOW - 10_000, day=1e6), acct(), CFG, NOW, False)
    assert any(r.startswith("COIN_NOT_ALLOWED") for r in d.reasons)
    assert any(r.startswith("STALE_DATA") for r in d.reasons)
    assert "LIQUIDITY_BELOW_FLOOR" in d.reasons


def test_price_band_and_slippage():
    d = evaluate(child(px=102.0), intent(max_slippage_bps=50), snap(), acct(), CFG, NOW, False)
    assert any(r.startswith("PRICE_OUTSIDE_BAND") for r in d.reasons)
    assert any(r.startswith("SLIPPAGE_CAP") for r in d.reasons)


def test_resting_orders_count_toward_caps():
    # 90 ETH of resting buys at 100 = 9,000 USD; a 20 ETH child takes worst case to 11,000 > 10,000 coin cap
    orders = [OpenOrder("o", "ETH", Side.BUY, 90.0, 100.0)]
    d = evaluate(child(size=20.0, px=100.01), intent(size=20.0),
                 snap(depth=1e6), acct(eq=1e6, orders=orders), PolicyConfig(max_child_notional_usd=1e9), NOW, False)
    assert "COIN_NOTIONAL_CAP" in d.reasons


def test_account_total_and_leverage_across_coins():
    pos = [Position("BTC", 0.2, 100_000.0)]   # 20,000 USD
    cfg = PolicyConfig(max_coin_notional_usd={"BTC": 1e9})
    # 20,000 BTC + 6,000 ETH child = 26,000 > 25,000 account cap; 26,000 / 5,000 = 5.2x > 3x
    d = evaluate(child(size=60.0, px=100.01), intent(size=60.0), snap(depth=1e6),
                 acct(eq=5_000.0, positions=pos), cfg, NOW, False, ref_px_by_coin={"BTC": 100_000.0})
    assert "ACCOUNT_NOTIONAL_CAP" in d.reasons
    assert any(r.startswith("LEVERAGE_CAP") for r in d.reasons)


def test_daily_loss_excludes_deposits():
    a = AccountState(NOW, 10_400.0, (), (), day_start_equity_usd=10_000.0, day_net_flows_usd=1_000.0)
    assert a.day_pnl_usd() == -600.0
    d = evaluate(child(), intent(), snap(), a, CFG, NOW, False)
    assert "DAILY_LOSS_LIMIT" in d.reasons


def test_reduce_only_must_reduce():
    d = evaluate(child(side=Side.BUY, reduce_only=True), intent(reduce_only=True), snap(),
                 acct(positions=[Position("ETH", 2.0, 100.0)]), CFG, NOW, False)
    assert "REDUCE_ONLY_WOULD_INCREASE" in d.reasons
    d2 = evaluate(child(side=Side.SELL, px=99.99, reduce_only=True), intent(side=Side.SELL, reduce_only=True), snap(),
                  acct(positions=[Position("ETH", 2.0, 100.0)]), CFG, NOW, False)
    assert d2.allow, d2.reasons


def test_audit_chain_detects_tamper(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p)
    for i in range(5):
        log.append("intent", {"i": i})
    assert AuditLog.verify(p) == (True, 5, "ok")
    # resume continues the chain
    AuditLog(p).append("intent", {"i": 5})
    assert AuditLog.verify(p)[0]
    lines = p.read_bytes().splitlines()
    rec = json.loads(lines[2]); rec["data"]["i"] = 99
    lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
    p.write_bytes(b"\n".join(lines) + b"\n")
    ok, n, msg = AuditLog.verify(p)
    assert not ok and "line 4" in msg


def test_audit_refuses_secrets(tmp_path):
    with pytest.raises(ValueError):
        AuditLog(tmp_path / "a.jsonl").append("x", {"nested": {"private_key": "0xabc"}})


@pytest.mark.parametrize("att,obs,ok", [
    (Attestation("KR", accepts_terms=True), "KR", True),
    (Attestation("US", accepts_terms=True), "", False),
    (Attestation("CA", "CA-ON", accepts_terms=True), "CA", False),
    (Attestation("CA", "CA-BC", accepts_terms=True), "CA", True),
    (Attestation("DE", citizenships=("IR",), accepts_terms=True), "DE", False),
    (Attestation("SG", accepts_terms=True), "US", False),                 # observed US
    (Attestation("SG", accepts_terms=True, uses_vpn_or_proxy=True), "SG", False),
    (Attestation("GB", is_entity=True, entity_incorporation="US", accepts_terms=True), "GB", False),
    (Attestation("JP", accepts_terms=False), "JP", False),
])
def test_eligibility(att, obs, ok):
    assert check(att, observed_country=obs)[0] is ok
