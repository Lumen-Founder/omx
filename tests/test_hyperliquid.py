"""Offline tests for the Hyperliquid adapter. No network: every response is a fake
shaped like the HL docs / SDK docstrings. Keys are throwaway, created in-test."""
import pickle

import msgpack
import pytest
from eth_account import Account
from hyperliquid.utils.signing import (float_to_wire, order_request_to_order_wire, order_wires_to_order_action,
                                       recover_agent_or_user_from_l1_action, sign_l1_action)
from hyperliquid.utils.types import Cloid

from omx.types import ChildOrder, Side
from omx.venue import hyperliquid as hlmod
from omx.venue.base import VenueError
from omx.venue.hyperliquid import (MAINNET_API_URL, TESTNET_API_URL, HyperliquidVenue, LocalKeySigner, RateLimited,
                                   TokenBucket, cloid_for, round_px, round_sz, wire_str)

USER = "0x" + "ab" * 20
BUILDER = "0x" + "Cd" * 20
NOW = 1_800_000_000_000


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any socket connect during these tests is a bug."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

META = [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                      {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                      {"name": "ATOM", "szDecimals": 2, "maxLeverage": 5},
                      {"name": "MATIC", "szDecimals": 1, "maxLeverage": 5},
                      {"name": "DYDX", "szDecimals": 1, "maxLeverage": 5},
                      {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
                      {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10}]},
        [{"dayNtlVlm": "2100000000.5", "markPx": "123456.0", "midPx": "123456.5", "funding": "0.0000125",
          "openInterest": "30000", "oraclePx": "123400", "premium": "0.0001", "prevDayPx": "120000",
          "impactPxs": ["123450", "123460"]},
         {"dayNtlVlm": "900000000.0", "markPx": "1891.4", "funding": "0", "openInterest": "1", "oraclePx": "1891",
          "prevDayPx": "1800"},
         {"dayNtlVlm": "1", "markPx": "7.5", "oraclePx": "7.5", "prevDayPx": "7"},
         {"dayNtlVlm": "1", "markPx": "0.5", "oraclePx": "0.5", "prevDayPx": "0.5"},
         {"dayNtlVlm": "1", "markPx": "1.2", "oraclePx": "1.2", "prevDayPx": "1.2"},
         {"dayNtlVlm": "350000000", "markPx": "150.25", "oraclePx": "150.2", "prevDayPx": "149"},
         {"dayNtlVlm": "1000", "markPx": "0.012345", "oraclePx": "0.012345", "prevDayPx": "0.012"}]]

BOOK = {"coin": "BTC", "time": NOW - 250,
        "levels": [[{"px": "123455.0", "sz": "1.5", "n": 3}, {"px": "123450.0", "sz": "2.0", "n": 5}],
                   [{"px": "123457.0", "sz": "0.8", "n": 2}, {"px": "123460.0", "sz": "3.1", "n": 4}]]}


def ok_order(status):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}


class FakeTransport:
    def __init__(self, info=None, exchange=()):
        self.info = {"metaAndAssetCtxs": META, "l2Book": BOOK, "userAbstraction": "disabled"}
        self.info.update(info or {})
        self.exchange = list(exchange)
        self.calls = []

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path == "/exchange":
            r = self.exchange.pop(0)
        else:
            r = self.info[payload["type"]]
        r = r(payload) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return r

    def count(self, kind):
        return sum(1 for p, b in self.calls if p == "/info" and b["type"] == kind)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def agent():
    return LocalKeySigner(Account.create())


def venue(agent, transport=None, builder=None, base=MAINNET_API_URL, **kw):
    clk = FakeClock()
    kw.setdefault("limiter", TokenBucket(clock=clk, sleep=clk.sleep))
    return HyperliquidVenue(base, USER, agent, builder=builder, transport=transport or FakeTransport(),
                            clock_ms=lambda: NOW, sleep=clk.sleep, **kw)


def child(coin="BTC", side=Side.BUY, size=0.0123456, px=123456.7, tif="Ioc", ro=False, cid="c-1"):
    return ChildOrder(cid, "i-1", coin, side, size, px, tif, ro)


# --- wire rounding ---------------------------------------------------------

@pytest.mark.parametrize("px,szd,mode,want", [
    (123456.7, 5, "nearest", "123460"),      # 5 significant figures
    (123456.7, 5, "down", "123450"),
    (123456.7, 5, "up", "123460"),
    (1891.456, 4, "nearest", "1891.5"),     # 5 sig figs and <= 2 decimals
    (150.2549, 2, "nearest", "150.25"),
    (0.0123456, 0, "nearest", "0.012346"),  # 6 decimals allowed at szDecimals 0
    (0.00012345678, 0, "nearest", "0.000123"),  # decimals cap binds before sig figs
    (99999.7, 5, "up", "100000"),
    (7.0, 2, "nearest", "7"),
])
def test_round_px(px, szd, mode, want):
    got = round_px(px, szd, mode=mode)
    assert wire_str(got) == want
    assert float_to_wire(float(got)) == want          # identical to the SDK's text


def test_round_px_nearest_matches_sdk_slippage_formula():
    for px, szd in [(123456.7, 5), (1891.456, 4), (150.2549, 2), (0.0123456, 0), (42.123456, 1)]:
        sdk = round(float(f"{px:.5g}"), 6 - szd)       # Exchange._slippage_price, perps branch
        assert float(round_px(px, szd)) == sdk


def test_round_sz_floors_to_sz_decimals():
    assert wire_str(round_sz(0.0123456, 5)) == "0.01234"
    assert wire_str(round_sz(0.1 + 0.2, 5)) == "0.3"
    assert wire_str(round_sz(12.99, 0)) == "12"
    with pytest.raises(VenueError):
        round_px(0.0, 2)


def test_cloid_is_deterministic_16_bytes():
    c = cloid_for("c-1")
    assert c == cloid_for("c-1") != cloid_for("c-2")
    assert Cloid(c).to_raw() == c                      # SDK validator: 0x + 32 hex


# --- order action == SDK construction --------------------------------------

def sdk_action(c, asset, szd, builder=None):
    is_buy = c.side is Side.BUY
    req = {"coin": c.coin, "is_buy": is_buy, "sz": float(round_sz(c.size, szd)),
           "limit_px": float(round_px(c.px, szd, mode="down" if is_buy else "up")),
           "order_type": {"limit": {"tif": c.tif}}, "reduce_only": c.reduce_only,
           "cloid": Cloid(cloid_for(c.child_id))}
    b = dict(builder) if builder else None
    if b:
        b["b"] = b["b"].lower()                        # Exchange.bulk_orders lowercases the builder
    return order_wires_to_order_action([order_request_to_order_wire(req, asset)], b)


@pytest.mark.parametrize("c,asset,szd", [
    (child(), 0, 5),
    (child(coin="ETH", side=Side.SELL, size=1.23456, px=1891.456, tif="Alo", ro=True, cid="x"), 1, 4),
    (child(coin="SOL", size=3.0, px=150.2549, tif="Gtc"), 5, 2),
])
@pytest.mark.parametrize("builder", [None, (BUILDER, 10)])
def test_order_action_and_signature_match_sdk(agent, c, asset, szd, builder):
    v = venue(agent, builder=builder)
    ours = v.build_order_action(c)
    theirs = sdk_action(c, asset, szd, {"b": builder[0], "f": builder[1]} if builder else None)
    assert ours == theirs
    assert msgpack.packb(ours) == msgpack.packb(theirs)    # key order matters for the hash
    nonce = NOW + 7
    for mainnet in (True, False):
        assert agent.sign_l1_action(ours, nonce, None, mainnet) == \
            sign_l1_action(agent._account, theirs, None, nonce, None, mainnet)
    if builder:
        assert ours["builder"] == {"b": BUILDER.lower(), "f": 10}
    else:
        assert "builder" not in ours


def test_place_posts_signed_body_and_returns_resting_oid(agent):
    t = FakeTransport(exchange=[ok_order({"resting": {"oid": 77738308}})])
    v = venue(agent, t, builder=(BUILDER, 25), base=TESTNET_API_URL)
    assert v.place(child()) == "77738308"
    path, body = t.calls[-1]
    assert path == "/exchange" and body["nonce"] == NOW and body["vaultAddress"] is None
    assert body["action"]["builder"] == {"b": BUILDER.lower(), "f": 25}
    assert body["action"]["orders"][0]["p"] == "123450"    # buy rounded down, never above approved px
    assert body["expiresAfter"] == NOW + hlmod.ORDER_EXPIRY_MS            # R3-4: signed with an expiry
    signer = recover_agent_or_user_from_l1_action(body["action"], body["signature"], None, body["nonce"],
                                                  body["expiresAfter"], False)
    assert signer == agent.address
    # nonces are strictly increasing even within one millisecond
    t.exchange.append(ok_order({"filled": {"totalSz": "0.01234", "avgPx": "123457.0", "oid": 9}}))
    assert v.place(child(cid="c-2")) == "9"
    assert t.calls[-1][1]["nonce"] == NOW + 1


def test_place_status_errors(agent):
    t = FakeTransport(exchange=[ok_order({"error": "Order must have minimum value of $10."}),
                                {"status": "err", "response": "User or API Wallet 0x12 does not exist."},
                                ok_order("waitingForFill")])
    v = venue(agent, t)
    with pytest.raises(VenueError, match=r"minimum value of \$10"):
        v.place(child())
    with pytest.raises(VenueError, match="does not exist"):
        v.place(child())
    with pytest.raises(VenueError, match="UNKNOWN_STATUS"):
        v.place(child())


def test_place_refusals(agent):
    v = venue(agent)
    with pytest.raises(VenueError, match="UNKNOWN_COIN"):
        v.place(child(coin="NOPE"))
    with pytest.raises(VenueError, match="SIZE_ROUNDS_TO_ZERO"):
        v.place(child(size=0.000001))
    with pytest.raises(VenueError, match="UNSUPPORTED_TIF"):
        v.place(child(tif="Fok"))


@pytest.mark.parametrize("builder", [(BUILDER, 101), (BUILDER, -1), (BUILDER, True), (BUILDER, 1.5),
                                     ("0x1234", 10), ("cd" * 20, 10), ("0x" + "zz" * 20, 10)])
def test_builder_refused(agent, builder):
    with pytest.raises(VenueError):
        venue(agent, builder=builder)


def test_builder_at_cap_accepted(agent):
    assert venue(agent, builder=(BUILDER, 100)).builder == {"b": BUILDER.lower(), "f": 100}


def test_main_wallet_signer_refused_by_default():
    main = LocalKeySigner(Account.create())
    with pytest.raises(VenueError, match="agent"):
        HyperliquidVenue(MAINNET_API_URL, main.address, main, transport=FakeTransport())
    HyperliquidVenue(MAINNET_API_URL, main.address, main, transport=FakeTransport(), allow_main_wallet_signer=True)


def test_cancel(agent):
    t = FakeTransport(exchange=[
        {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}},
        {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [
            {"error": "Order was never placed, already canceled, or filled."}]}}}])
    v = venue(agent, t)
    assert v.cancel("ETH", "77738308") is True
    assert t.calls[-1][1]["action"] == {"type": "cancel", "cancels": [{"a": 1, "o": 77738308}]}
    assert v.cancel("ETH", "1") is False


# --- reads -----------------------------------------------------------------

def test_snapshot_uses_venue_time_and_caches_ctx(agent):
    t = FakeTransport()
    v = venue(agent, t)
    s = v.snapshot("BTC")
    assert s.ts_ms == NOW - 250                         # book's own time, not the local clock
    assert s.best_bid == 123455.0 and s.best_ask == 123457.0 and s.bids[1].sz == 2.0
    assert s.mark_px == 123456.0 and s.day_ntl_usd == 2100000000.5
    v.snapshot("BTC")
    assert t.count("l2Book") == 2 and t.count("metaAndAssetCtxs") == 1


def test_meta_helpers(agent):
    v = venue(agent)
    assert v.size_decimals("BTC") == 5 and v.size_decimals("kPEPE") == 0
    assert v.tick_size("BTC") == 10.0                   # 5 sig figs at 123456
    assert v.tick_size("ETH") == 0.1
    assert v.tick_size("kPEPE") == 1e-6


def test_account(agent):
    cs = {"assetPositions": [
            {"position": {"coin": "ETH", "szi": "-0.5", "entryPx": "1890.0", "leverage": {"type": "cross", "value": 5},
                          "positionValue": "945.7", "unrealizedPnl": "-0.7", "marginUsed": "189.1"},
             "type": "oneWay"},
            {"position": {"coin": "BTC", "szi": "0.0", "entryPx": None, "leverage": {"type": "cross", "value": 20}},
             "type": "oneWay"}],
          "marginSummary": {"accountValue": "10234.56", "totalNtlPos": "945.7", "totalRawUsd": "11180.2",
                            "totalMarginUsed": "189.1"},
          "withdrawable": "10000.0", "time": NOW - 40}
    oo = [{"coin": "ETH", "limitPx": "1900.0", "oid": 9, "side": "A", "sz": "0.2", "timestamp": NOW - 1000},
          {"coin": "BTC", "limitPx": "120000", "oid": 10, "side": "B", "sz": "0.01", "timestamp": NOW,
           "cloid": cloid_for("c-9")}]
    v = venue(agent, FakeTransport(info={"clearinghouseState": cs, "frontendOpenOrders": oo}))
    v._child_by_cloid[cloid_for("c-9")] = "c-9"
    a = v.account()
    assert a.ts_ms == NOW - 40 and a.equity_usd == 10234.56 and a.day_start_equity_usd is None
    assert len(a.positions) == 1 and a.position("ETH") == -0.5
    assert a.positions[0].entry_px == 1890.0 and a.positions[0].leverage == 5.0
    o1, o2 = a.open_orders
    assert (o1.child_id, o1.side, o1.size, o1.px, o1.reduce_only) == ("oid:9", Side.SELL, 0.2, 1900.0, False)
    assert (o2.child_id, o2.side) == ("c-9", Side.BUY)


def fill_row(**kw):
    r = {"closedPnl": "0.0", "coin": "BTC", "crossed": True, "dir": "Open Long", "hash": "0xabc", "oid": 77738308,
         "px": "123457.0", "side": "B", "startPosition": "0.0", "sz": "0.01234", "time": NOW + 5,
         "fee": "0.685", "feeToken": "USDC", "tid": 1}
    r.update(kw)
    return r


def test_fills_mapping_with_builder_fee(agent):
    rows = [fill_row(cloid=cloid_for("c-1"), builderFee="0.152"),
            fill_row(side="A", crossed=False, fee="-0.01", tid=2, time=NOW + 9, oid=5, dir="Close Long")]
    t = FakeTransport(info={"userFillsByTime": rows})
    v = venue(agent, t)
    v._child_by_cloid[cloid_for("c-1")] = "c-1"
    f1, f2 = v.fills_since(NOW)
    assert (f1.child_id, f1.side, f1.size, f1.px, f1.fee_usd, f1.builder_fee_usd, f1.ts_ms, f1.crossed) == \
        ("c-1", Side.BUY, 0.01234, 123457.0, pytest.approx(0.685 - 0.152), 0.152, NOW + 5, True)
    assert (f2.child_id, f2.side, f2.fee_usd, f2.builder_fee_usd, f2.crossed) == ("oid:5", Side.SELL, -0.01, 0.0, False)
    q = t.calls[-1][1]
    assert q == {"type": "userFillsByTime", "user": USER, "startTime": NOW, "endTime": None, "aggregateByTime": False}


def test_fills_paginate_and_dedupe(agent, monkeypatch):
    monkeypatch.setattr(hlmod, "FILLS_PAGE_MAX", 2)
    pages = {NOW: [fill_row(tid=1, time=NOW), fill_row(tid=2, time=NOW + 1)],
             NOW + 1: [fill_row(tid=2, time=NOW + 1), fill_row(tid=3, time=NOW + 2)],
             NOW + 2: [fill_row(tid=3, time=NOW + 2)]}
    v = venue(agent, FakeTransport(info={"userFillsByTime": lambda p: pages[p["startTime"]]}))
    assert [f.ts_ms for f in v.fills_since(NOW)] == [NOW, NOW + 1, NOW + 2]


# --- rate limiting ---------------------------------------------------------

def test_token_bucket_with_fake_clock():
    clk = FakeClock()
    b = TokenBucket(capacity=1200, period_s=60, clock=clk, sleep=clk.sleep)
    for _ in range(60):
        assert b.acquire(20) == 0.0
    assert b.acquire(20) == pytest.approx(1.0)          # 20 weight per second refill
    clk.t += 30
    assert b.acquire(600) == pytest.approx(0.0)
    b.penalize(5)
    assert b.acquire(2) == pytest.approx(5.0)           # waits out the block; refill covers the rest
    with pytest.raises(ValueError):
        b.acquire(5000)


def test_weights_follow_docs():
    assert hlmod.info_weight("l2Book") == 2 and hlmod.info_weight("clearinghouseState") == 2
    assert hlmod.info_weight("metaAndAssetCtxs") == 20 and hlmod.info_weight("openOrders") == 20
    assert hlmod.exchange_weight(1) == 1 and hlmod.exchange_weight(40) == 2


def test_info_backs_off_on_429_then_succeeds(agent):
    clk = FakeClock()
    seq = [RateLimited("HTTP 429"), RateLimited("HTTP 429"), BOOK]
    t = FakeTransport(info={"l2Book": lambda p: seq.pop(0)})
    v = HyperliquidVenue(MAINNET_API_URL, USER, agent, transport=t, clock_ms=lambda: NOW,
                         limiter=TokenBucket(clock=clk, sleep=clk.sleep), sleep=clk.sleep)
    assert v.snapshot("BTC").ts_ms == NOW - 250
    assert clk.slept[:2] == [pytest.approx(1.0), pytest.approx(2.0)]


def test_info_gives_up_after_retries(agent):
    t = FakeTransport(info={"l2Book": RateLimited("HTTP 429")})
    v = venue(agent, t, max_retries=2)
    with pytest.raises(VenueError, match="RATE_LIMITED:info"):
        v.snapshot("BTC")
    assert t.count("l2Book") == 3


def test_exchange_429_is_not_retried(agent):
    t = FakeTransport(exchange=[RateLimited("HTTP 429")])
    v = venue(agent, t)
    with pytest.raises(VenueError, match="RATE_LIMITED:exchange"):
        v.place(child())
    assert sum(1 for p, _ in t.calls if p == "/exchange") == 1
    assert v.limiter.blocked_until > v.limiter.clock()


# --- signer hygiene --------------------------------------------------------

def test_signer_from_key_file_never_reveals_key(tmp_path):
    acct = Account.create()                             # throwaway, exists only in this test
    key_hex = acct.key.hex()
    p = tmp_path / "agent.key"
    p.write_text(key_hex + "\n", encoding="utf-8")
    s = LocalKeySigner.from_key_file(p)
    assert s.address == acct.address
    bare = key_hex.removeprefix("0x")
    for text in (repr(s), str(s), f"{s}"):
        assert bare not in text and acct.address in text
    assert not hasattr(s, "__dict__")
    with pytest.raises(TypeError):
        pickle.dumps(s)
    bad = tmp_path / "bad.key"
    bad.write_text("not-a-key-0123", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        LocalKeySigner.from_key_file(bad)
    assert "not-a-key" not in str(ei.value) and ei.value.__cause__ is None


def test_requests_transport_maps_status_codes():
    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, str(body)

        def json(self):
            return self._body

    t = hlmod.RequestsTransport(TESTNET_API_URL, timeout_s=3)
    sent = []
    queue = [Resp(429, {}), Resp(500, "boom"), Resp(200, {"ok": 1})]
    t.session.post = lambda url, json, timeout: (sent.append((url, timeout)), queue.pop(0))[1]
    with pytest.raises(RateLimited):
        t("/info", {"type": "l2Book", "coin": "BTC"})
    with pytest.raises(VenueError, match="HTTP 500"):
        t("/info", {})
    assert t("/info", {}) == {"ok": 1}
    assert sent[0] == (TESTNET_API_URL + "/info", 3)
