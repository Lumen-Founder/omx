"""Paper mode (omx.paper / omx.venue.paper): real tools, simulated fills against public HL books.

Every test here injects a fake /info transport and a fake clock; the no_network fixture refuses
any socket. One test at the end talks to Hyperliquid mainnet's public /info, only with
OMX_TEST_NETWORK=1."""
import asyncio
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from mcp import Client, Implementation

from omx.mcp_server import InspectGateway, build_server
from omx.paper import PAPER_NOTE, PaperRuntime
from omx.policy import PolicyConfig
from omx.types import ChildOrder, Side
from omx.venue.base import VenueError
from omx.venue.paper import HLPublicFeed, PaperConfig, PaperGuardError, PaperVenue, PublicInfoOnly

NETWORK = os.environ.get("OMX_TEST_NETWORK") == "1"
DAY_NTL = 1e9


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def levels(rows):
    return [{"px": str(px), "sz": str(sz), "n": 1} for px, sz in rows]


class FakeInfo:
    """Hyperliquid /info stand-in. Books are set per coin; `down` makes every call fail."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.calls: list[tuple[str, dict]] = []
        self.down = False
        self.books = {}
        self.set_book("ETH", [(3000.0, 30), (2999.5, 40), (2999.0, 50)],
                      [(3000.1, 30), (3000.5, 40), (3001.0, 50)])
        self.set_book("BTC", [(100_000.0, 2), (99_990.0, 3)], [(100_010.0, 2), (100_020.0, 3)])
        self.set_book("SOL", [(150.0, 2000), (149.99, 3000)], [(150.01, 2000), (150.02, 3000)])

    def set_book(self, coin, bids, asks):
        self.books[coin] = (bids, asks)
        self._book_t = getattr(self, "_book_t", 0) + 1

    def __call__(self, path, payload):
        self.calls.append((path, dict(payload)))
        if self.down:
            raise ConnectionError("fake network down")
        t = payload["type"]
        if t == "metaAndAssetCtxs":
            names = [("BTC", 5), ("ETH", 4), ("SOL", 2)]
            return [{"universe": [{"name": n, "szDecimals": d} for n, d in names]},
                    [{"dayNtlVlm": str(DAY_NTL), "markPx": str(self._mid(n))} for n, _ in names]]
        if t == "l2Book":
            bids, asks = self.books[payload["coin"]]
            return {"coin": payload["coin"], "time": int(self.clock() * 1000) - 50 + self._book_t,
                    "levels": [levels(bids), levels(asks)]}
        if t == "allMids":
            return {**{c: str(self._mid(c)) for c in self.books}, "@107": "1.0"}
        raise AssertionError(f"unexpected /info {payload}")

    def _mid(self, coin):
        bids, asks = self.books[coin]
        return (bids[0][0] + asks[0][0]) / 2

    def count(self, kind):
        return sum(1 for _, p in self.calls if p["type"] == kind)


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    if request.node.name == "test_live_public_books":       # opt-in, OMX_TEST_NETWORK=1
        yield []
        return
    inside_pair = threading.local()
    attempts = []
    real_pair, real_connect = socket.socketpair, socket.socket.connect

    def pair(*a, **k):
        inside_pair.on = True
        try:
            return real_pair(*a, **k)
        finally:
            inside_pair.on = False

    def connect(self, addr):
        if getattr(inside_pair, "on", False):
            return real_connect(self, addr)
        attempts.append(addr)
        raise AssertionError(f"network access attempted: {addr}")

    def create_connection(addr, *a, **k):
        attempts.append(addr)
        raise AssertionError(f"network access attempted: {addr}")

    monkeypatch.setattr(socket, "socketpair", pair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    yield attempts
    assert attempts == []


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    d = tmp_path / "cwd"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def info(clock):
    return FakeInfo(clock)


@pytest.fixture
def rt(cwd, clock, info):
    with PaperRuntime(start=False, transport=info, clock=clock) as r:
        yield r


def run_for(rt, clock, seconds, step_s=0.2):
    for _ in range(int(round(seconds / step_s))):
        clock.advance(step_s)
        rt.step()


def call(server, tool, args=None, client="paper-test"):
    async def go():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            return await c.call_tool(tool, args or {})
    r = asyncio.run(go())
    assert not r.is_error, r.content
    return r.structured_content


def tool_defs(server):
    async def go():
        async with Client(server) as c:
            return [(t.name, t.description, t.input_schema) for t in (await c.list_tools()).tools]
    return asyncio.run(go())


def venue(clock, info, **cfg):
    return PaperVenue(HLPublicFeed(info, clock=clock), PaperConfig(**cfg))


def child(cid, side, size, px, tif="Ioc", coin="ETH", reduce_only=False):
    return ChildOrder(cid, "i", coin, side, size, px, tif, reduce_only)


# ---------------------------------------------------------------- guard: public /info only

def test_guard_allows_only_public_market_data_reads():
    sent = []
    g = PublicInfoOnly(lambda p, x: sent.append((p, x)) or {})
    g("/info", {"type": "l2Book", "coin": "ETH"})
    g("/info", {"type": "allMids"})
    g("/info", {"type": "metaAndAssetCtxs"})
    for path, payload in [("/exchange", {"type": "allMids"}),
                          ("/info", {"type": "clearinghouseState", "user": "0x" + "1" * 40}),
                          ("/info", {"type": "l2Book", "coin": "ETH", "user": "0x" + "1" * 40}),
                          ("/info", {"type": "userFills", "user": "0x" + "1" * 40}),
                          ("/info", {"type": "allMids", "dex": "x"})]:
        with pytest.raises(PaperGuardError):
            g(path, payload)
    assert len(sent) == 3


def test_paper_builds_no_signer_or_hyperliquid_adapter(rt, info):
    import omx.venue.hyperliquid as hl
    assert type(rt.engine.venue) is PaperVenue and not isinstance(rt.engine.venue, hl.HyperliquidVenue)
    assert rt.cfg.signer_key_path is None and rt.cfg.signer_key_env is None and rt.cfg.builder_address is None
    srv = rt.server()
    call(srv, "get_quote", {"coin": "ETH"})
    call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market"})
    run_for(rt, rt.clock, 2)
    assert {p for p, _ in info.calls} == {"/info"}
    assert {x["type"] for _, x in info.calls} <= {"l2Book", "allMids", "metaAndAssetCtxs"}
    assert all("user" not in x for _, x in info.calls)


def test_startup_and_idle_send_nothing(cwd, clock, info):
    with PaperRuntime(start=False, transport=info, clock=clock) as r:
        run_for(r, clock, 5)
        assert info.calls == []


# ---------------------------------------------------------------- tools

def test_tool_list_is_the_normal_one_and_answers_are_marked_paper(rt):
    srv = rt.server()
    assert tool_defs(srv) == tool_defs(build_server(InspectGateway()))
    for tool, args in [("get_quote", {"coin": "BTC"}),
                       ("preview_order", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market"}),
                       ("account_summary", {}), ("policy_limits", {}), ("order_status", {"intent_id": "nope"}),
                       ("approval_status", {"approval_id": "ap-000000000000"}),
                       ("cancel_order", {"intent_id": "nope"})]:
        res = call(srv, tool, args)
        assert res["paper"] is True and res["paper_note"] == PAPER_NOTE and "demo" not in res, tool
    lim = call(srv, "policy_limits")
    assert lim["network"] == "paper" and lim["builder_fee_tenths_bps"] == 0
    assert lim["policy"]["allowed_coins"] == ["BTC", "ETH", "SOL"]


def test_get_quote_is_the_public_book(rt):
    q = call(rt.server(), "get_quote", {"coin": "ETH"})
    assert q["ok"] and q["best_bid"] == 3000.0 and q["best_ask"] == 3000.1
    assert q["mid"] == pytest.approx(3000.05) and q["day_ntl_usd"] == DAY_NTL
    assert q["ask_depth_usd"] == pytest.approx(30 * 3000.1 + 40 * 3000.5 + 50 * 3001.0)


def test_market_buy_fills_at_the_ask_with_the_taker_fee(rt, clock):
    srv = rt.server()
    res = call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market",
                                     "client_intent_id": "paper-mkt-01"})
    assert res["ok"] and res["status"] == "submitted" and res["paper"] is True
    run_for(rt, clock, 2)
    rep = call(srv, "order_status", {"intent_id": res["intent_id"]})["report"]
    assert rep["state"] == "done" and rep["filled_size"] == pytest.approx(0.01)
    assert rep["avg_px"] == pytest.approx(3000.1)
    assert rep["exch_fee_usd"] == pytest.approx(0.01 * 3000.1 * 4.5e-4)
    acct = call(srv, "account_summary")
    assert [(p["coin"], p["size"]) for p in acct["positions"]] == [("ETH", pytest.approx(0.01))]
    # equity = 10000 - fee + 0.01 * (mid - entry)
    assert acct["equity_usd"] == pytest.approx(10_000 - 0.01 * 3000.1 * 4.5e-4 + 0.01 * (3000.05 - 3000.1))
    assert rt.errors == []


def test_small_twap_runs_to_a_full_fill(rt, clock):
    srv = rt.server()
    res = call(srv, "submit_order", {"coin": "ETH", "side": "sell", "size": 0.05, "algo": "twap",
                                     "duration_s": 4, "slices": 4})
    run_for(rt, clock, 8)
    rep = call(srv, "order_status", {"intent_id": res["intent_id"]})["report"]
    assert rep["state"] == "done" and rep["filled_size"] == pytest.approx(0.05)
    assert rep["children_sent"] >= 4 and rep["avg_px"] == pytest.approx(3000.0)


# ---------------------------------------------------------------- venue: book walk, partial, through

def test_taker_walks_the_book_and_leaves_the_rest_unfilled(clock, info):
    v = venue(clock, info)
    v.place(child("a.1", Side.BUY, 50.0, 3000.5))
    clock.advance(0.3)
    v.sync()
    fills = v.fills_since(0)
    assert [(f.size, f.px, f.crossed) for f in fills] == [(30.0, 3000.1, True), (20.0, 3000.5, True)]
    assert v.order_status("a.1")["state"] == "filled"
    # same book (no new read within 1 s): what we took stays taken
    v.place(child("a.2", Side.BUY, 30.0, 3000.5))
    clock.advance(0.3)
    st = v.order_status("a.2")
    assert st["status"] == "expired" and st["reason"] == "IOC_REMAINDER" and st["filled_sz"] == pytest.approx(20.0)
    # a newer book is assumed refilled
    clock.advance(1.0)
    v.place(child("a.3", Side.BUY, 10.0, 3000.1))
    clock.advance(0.3)
    assert v.order_status("a.3")["state"] == "filled"
    assert v.fills_since(0)[-1].px == 3000.1
    fee = sum(f.fee_usd for f in v.fills_since(0))
    assert fee == pytest.approx(sum(f.size * f.px for f in v.fills_since(0)) * 4.5e-4)


def test_ioc_beyond_its_limit_does_not_fill_and_gtc_remainder_rests(clock, info):
    v = venue(clock, info)
    v.place(child("b.1", Side.BUY, 1.0, 3000.0))                       # below the ask: nothing to take
    v.place(child("b.2", Side.BUY, 40.0, 3000.1, tif="Gtc"))           # 30 at the ask, 10 rest
    clock.advance(0.3)
    assert v.order_status("b.1")["status"] == "expired" and v.order_status("b.1")["filled_sz"] == 0
    st = v.order_status("b.2")
    assert st["status"] == "live" and st["filled_sz"] == pytest.approx(30.0) and st["remaining"] == pytest.approx(10.0)
    assert [o.size for o in v.account().open_orders] == [pytest.approx(10.0)]


def test_resting_limit_fills_only_when_the_book_trades_through(clock, info):
    v = venue(clock, info)
    v.place(child("c.1", Side.BUY, 1.0, 2999.0, tif="Alo"))
    clock.advance(0.3)
    assert v.order_status("c.1")["status"] == "live"
    info.set_book("ETH", [(2998.5, 10)], [(2999.0, 10)])     # ask touches our price: no fill (back of queue)
    clock.advance(1.1)
    v.sync()
    assert v.order_status("c.1")["filled_sz"] == 0
    info.set_book("ETH", [(2998.0, 10)], [(2998.5, 10)])     # ask below our price: traded through
    clock.advance(1.1)
    v.sync()
    st = v.order_status("c.1")
    assert st["state"] == "filled"
    f = v.fills_since(0)[-1]
    assert (f.px, f.crossed) == (2999.0, False) and f.fee_usd == pytest.approx(2999.0 * 1.5e-4)


def test_alo_that_would_cross_is_rejected_and_small_orders_are_refused(clock, info):
    v = venue(clock, info)
    v.place(child("d.1", Side.SELL, 1.0, 3000.0, tif="Alo"))
    clock.advance(0.3)
    assert v.order_status("d.1")["reason"] == "ALO_WOULD_CROSS"
    with pytest.raises(VenueError, match="minimum value"):
        v.place(child("d.2", Side.BUY, 0.003, 3000.0))
    # off-grid price moves toward the passive side, like the live adapter
    v.place(child("d.3", Side.BUY, 0.01, 2999.97, tif="Alo"))
    assert v.account().open_orders[-1].px == 2999.9


def test_cancel_takes_effect_after_the_cancel_latency(clock, info):
    v = venue(clock, info)
    oid = v.place(child("e.1", Side.BUY, 1.0, 2990.0, tif="Gtc"))
    clock.advance(0.3)
    assert v.cancel("ETH", oid) is True
    clock.advance(0.2)
    assert v.order_status("e.1")["state"] == "open"
    clock.advance(0.4)
    assert v.order_status("e.1")["state"] == "cancelled"
    assert v.cancel("ETH", oid) is False


# ---------------------------------------------------------------- kill, failures, files

def test_kill_switch_cancels_resting_orders_and_stops_new_ones(rt, clock):
    srv = rt.server()
    res = call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.1, "algo": "limit",
                                     "limit_px": 2995.0})
    run_for(rt, clock, 1)
    assert len(rt.venue.account().open_orders) == 1
    k = call(srv, "kill_switch", {"reason": "paper test"})
    assert k["ok"] and "omx unkill" in k["message"] and k["paper"] is True
    run_for(rt, clock, 1)
    assert rt.venue.account().open_orders == ()
    assert call(srv, "order_status", {"intent_id": res["intent_id"]})["report"]["state"] == "killed"
    places = rt.venue.place_calls
    res2 = call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market"})
    assert res2["ok"] is False and "KILL_SWITCH_ACTIVE" in res2["block_reasons"]
    run_for(rt, clock, 1)
    assert rt.venue.place_calls == places and (rt.state_dir / "KILL").exists()


def test_market_data_failure_is_a_clear_refusal_and_the_process_lives(rt, clock, info):
    srv = rt.server()
    info.down = True
    q = call(srv, "get_quote", {"coin": "ETH"})
    assert q["ok"] is False and q["block_reasons"][0].startswith("MARKET_DATA_UNAVAILABLE") and q["paper"] is True
    n = len(info.calls)
    q = call(srv, "get_quote", {"coin": "ETH"})                 # inside the backoff: nothing is sent
    assert q["block_reasons"][0].startswith("MARKET_DATA_UNAVAILABLE") and len(info.calls) == n
    res = call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market"})
    assert res["ok"] is False and res["block_reasons"][0].startswith("MARKET_DATA_UNAVAILABLE")
    assert rt.venue.place_calls == 0
    run_for(rt, clock, 3)
    info.down = False
    clock.advance(31)                                            # past the longest backoff
    q = call(srv, "get_quote", {"coin": "ETH"})
    assert q["ok"] and q["mid"] == pytest.approx(3000.05)


def test_book_older_than_the_stale_limit_is_not_served(clock, info):
    feed = HLPublicFeed(info, clock=clock)
    feed.book("ETH")
    info.down = True
    clock.advance(2.0)
    assert feed.book("ETH").bids[0][0] == 3000.0                # served from cache (fetch failed, < 3 s)
    clock.advance(1.5)
    with pytest.raises(VenueError, match="MARKET_DATA_UNAVAILABLE"):
        feed.book("ETH")


def test_a_pending_order_without_market_data_is_rejected_not_filled_later(clock, info):
    v = venue(clock, info)
    v.place(child("f.1", Side.BUY, 1.0, 3001.0))
    info.down = True
    clock.advance(3.5)                                           # cached book too old by the time it lands
    st = v.order_status("f.1")
    assert st["state"] == "rejected" and st["reason"].startswith("MARKET_DATA_UNAVAILABLE")
    assert v.fills_since(0) == []


def test_requests_are_rate_capped(rt, clock, info):
    srv = rt.server()
    for _ in range(20):
        call(srv, "get_quote", {"coin": "ETH"})
    assert info.count("l2Book") == 1 and info.count("metaAndAssetCtxs") == 1
    clock.advance(1.0)
    call(srv, "get_quote", {"coin": "ETH"})
    assert info.count("l2Book") == 2


def test_paper_equity_is_configurable_and_checked(cwd, clock, info):
    with PaperRuntime(start=False, transport=info, clock=clock, equity_usd=2_500) as r:
        assert call(r.server(), "account_summary")["equity_usd"] == pytest.approx(2_500)
    for bad in (0, -1, float("nan"), float("inf"), 2e9, "x"):
        with pytest.raises(ValueError):
            PaperRuntime(start=False, transport=info, clock=clock, equity_usd=bad)
    assert list(cwd.iterdir()) == []


def test_nothing_is_written_to_the_working_dir_and_the_temp_dir_goes_away(cwd, clock, info):
    with PaperRuntime(start=False, transport=info, clock=clock) as r:
        srv = r.server()
        call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.02, "algo": "twap",
                                   "duration_s": 2, "slices": 2})
        call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})          # pending approval
        run_for(r, clock, 4)
        call(srv, "kill_switch", {"reason": "cleanup test"})
        tmp = Path(r._tmp)
        assert (r.state_dir / "audit.jsonl").exists()
        assert not tmp.resolve().is_relative_to(cwd.resolve())
    assert list(cwd.iterdir()) == []
    assert not tmp.exists()


def test_cli_entry_points_reach_serve_paper(cwd, monkeypatch, capsys):
    import omx.paper
    from omx import cli, mcp_server
    ran = []
    monkeypatch.setattr(omx.paper, "serve_paper", lambda err=None, equity_usd=None: ran.append(equity_usd) or 0)
    monkeypatch.delenv("OMX_CONFIG", raising=False)
    assert mcp_server.main(["--paper"]) == 0
    assert mcp_server.hyperliquid_main(["--paper", "--paper-equity", "500"]) == 0
    assert cli.main(["paper", "--paper-equity", "750"]) == 0
    assert ran == [10_000.0, 500.0, 750.0]
    for argv in (["--paper", "--demo"], ["--paper", "--inspect"], ["--paper-equity", "5"]):
        with pytest.raises(SystemExit):
            mcp_server.main(argv)
    assert ran == [10_000.0, 500.0, 750.0] and list(cwd.iterdir()) == []


def test_default_policy_is_unchanged(rt):
    assert rt.cfg.policy == PolicyConfig()


# ---------------------------------------------------------------- live public data (opt-in)

@pytest.mark.skipif(not NETWORK, reason="set OMX_TEST_NETWORK=1 to read Hyperliquid mainnet public /info")
def test_live_public_books(cwd):
    """get_quote ETH mid vs allMids, and a 0.01 ETH paper market buy vs the best ask."""
    with PaperRuntime(start=False) as r:
        srv = r.server()
        q = call(srv, "get_quote", {"coin": "ETH"})
        assert q["ok"], q
        mids = r.feed.transport("/info", {"type": "allMids"})
        all_mid = float(mids["ETH"])
        gap_bps = abs(q["mid"] - all_mid) / all_mid * 1e4
        res = call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 0.01, "algo": "market"})
        assert res["ok"], res
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.2)
            r.step()
            rep = call(srv, "order_status", {"intent_id": res["intent_id"]})["report"]
            if rep["state"] == "done":
                break
        print(f"\nLIVE quote bid={q['best_bid']} ask={q['best_ask']} mid={q['mid']} age_ms={q['age_ms']} "
              f"allMids={all_mid} gap_bps={gap_bps:.3f}; fill avg={rep['avg_px']} state={rep['state']} "
              f"vs ask {(rep['avg_px'] - q['best_ask']) / q['best_ask'] * 1e4 if rep['avg_px'] else None} bps; "
              f"requests={r.feed.requests} weight={r.feed.weight_sent}")
        assert gap_bps < 5
        assert rep["state"] == "done" and rep["filled_size"] == pytest.approx(0.01)
        assert abs(rep["avg_px"] - q["best_ask"]) / q["best_ask"] * 1e4 < 10
    assert list(cwd.iterdir()) == []
