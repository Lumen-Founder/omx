"""Demo mode (omx.demo): real tools on the in-process simulator, no key, config, daemon or network."""
import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp import Client, Implementation

from omx.demo import DemoRuntime
from omx.mcp_server import InspectGateway, build_server
from omx.venue.sim import SimVenue

ROOT = Path(__file__).resolve().parents[1]
SMALL_TWAP = {"coin": "ETH", "side": "buy", "size": 0.1, "algo": "twap", "duration_s": 4, "slices": 4}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse every socket connect. asyncio's Windows event loop builds its self-pipe with
    socket.socketpair(), which connects over loopback inside the call; only that is let through."""
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
        raise AssertionError(f"network access attempted in the demo: {addr}")

    def create_connection(addr, *a, **k):
        attempts.append(addr)
        raise AssertionError(f"network access attempted in the demo: {addr}")

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
def rt(cwd):
    with DemoRuntime(start=False) as r:      # tests advance time themselves with r.step(ms)
        yield r


def call(server, tool, args=None, client="demo-test"):
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


def run_for(rt, seconds, step_ms=200):
    for _ in range(int(seconds * 1000 / step_ms)):
        rt.step(step_ms)


def test_tool_list_is_the_normal_one(rt):
    assert tool_defs(rt.server()) == tool_defs(build_server(InspectGateway()))


def test_venue_is_the_simulator_and_no_key_is_configured(rt):
    assert isinstance(rt.engine.venue, SimVenue)
    assert rt.cfg.signer_key_path is None and rt.cfg.signer_key_env is None and rt.cfg.builder_address is None
    lim = call(rt.server(), "policy_limits")
    assert lim["network"] == "demo" and lim["demo"] is True and "No network" in lim["demo_note"]
    assert lim["builder_fee_tenths_bps"] == 0


def test_get_quote_returns_a_two_sided_book(rt):
    q = call(rt.server(), "get_quote", {"coin": "ETH"})
    assert q["ok"] and q["demo"] is True
    assert 0 < q["best_bid"] < q["mid"] < q["best_ask"]
    assert q["ask_depth_usd"] > 0 and q["age_ms"] >= 0


def test_every_tool_answer_is_marked_demo(rt):
    srv = rt.server()
    for tool, args in [("get_quote", {"coin": "BTC"}), ("preview_order", SMALL_TWAP), ("account_summary", {}),
                       ("policy_limits", {}), ("order_status", {"intent_id": "nope"}),
                       ("approval_status", {"approval_id": "ap-000000000000"}),
                       ("cancel_order", {"intent_id": "nope"})]:
        assert call(srv, tool, args)["demo"] is True, tool


def test_small_twap_runs_to_a_full_fill(rt):
    srv = rt.server()
    res = call(srv, "submit_order", {**SMALL_TWAP, "client_intent_id": "demo-twap-01"})
    assert res["ok"] and res["status"] == "submitted" and res["demo"] is True
    iid = res["intent_id"]
    assert iid == "ag-demo-twap-01"
    first = call(srv, "order_status", {"intent_id": iid})["report"]
    assert first["state"] in ("accepted", "working") and first["filled_size"] < 0.1
    run_for(rt, 8)
    rep = call(srv, "order_status", {"intent_id": iid})["report"]
    assert rep["state"] == "done" and rep["filled_size"] == pytest.approx(0.1)
    assert rep["children_sent"] >= 4 and rep["avg_px"] > 0
    acct = call(srv, "account_summary")
    assert any(p["coin"] == "ETH" and p["size"] == pytest.approx(0.1) for p in acct["positions"])
    assert acct["agent_budget_used_usd"] > 0
    assert call(srv, "submit_order", {**SMALL_TWAP, "client_intent_id": "demo-twap-01"})["status"] == "duplicate"
    assert rt.errors == []


def test_background_stepper_moves_the_market_and_fills(cwd):
    with DemoRuntime() as r:
        srv = r.server()
        t0, mid0 = r.sim.now_ms(), r.sim.mark_prices()["BTC"]
        res = call(srv, "submit_order", {"coin": "SOL", "side": "buy", "size": 2.0, "algo": "twap",
                                         "duration_s": 2, "slices": 2})
        assert res["status"] == "submitted"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            rep = call(srv, "order_status", {"intent_id": res["intent_id"]})["report"]
            if rep["state"] == "done":
                break
            time.sleep(0.2)
        assert rep["state"] == "done" and rep["filled_size"] == pytest.approx(2.0)
        assert r.sim.now_ms() - t0 >= 1_000 and r.loops > 5          # time ran without any test step
        assert r.sim.mark_prices()["BTC"] != mid0                     # and the simulated market moved
    assert list(cwd.iterdir()) == []


def test_above_threshold_waits_for_a_human_and_says_the_demo_has_none(rt):
    srv = rt.server()
    res = call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})      # ~3k USD > 1k
    assert res["status"] == "pending_approval" and res["routing_reasons"] == ["ABOVE_APPROVAL_THRESHOLD"]
    assert "no approval tool" in res["message"] and res["demo"] is True
    st = call(srv, "approval_status", {"approval_id": res["approval_id"]})
    assert st["state"] == "pending" and "no approval tool" in st["message"]
    run_for(rt, 2)
    assert call(srv, "order_status", {"intent_id": res["intent_id"]})["block_reasons"] == ["UNKNOWN_INTENT_ID"]
    assert rt.sim.place_calls == 0                                           # nothing reached the venue


def test_kill_switch_stops_new_orders(rt):
    srv = rt.server()
    k = call(srv, "kill_switch", {"reason": "demo test"})
    assert k["ok"] and "omx unkill" in k["message"] and k["demo"] is True
    res = call(srv, "submit_order", SMALL_TWAP)
    assert res["ok"] is False and "KILL_SWITCH_ACTIVE" in res["block_reasons"]
    run_for(rt, 2)
    assert rt.sim.place_calls == 0
    assert (rt.state_dir / "KILL").exists()


def test_nothing_is_written_to_the_working_dir_and_the_temp_dir_goes_away(cwd):
    with DemoRuntime(start=False) as r:
        srv = r.server()
        call(srv, "submit_order", SMALL_TWAP)
        call(srv, "submit_order", {"coin": "ETH", "side": "buy", "size": 1.0})
        run_for(r, 6)
        call(srv, "kill_switch", {"reason": "cleanup test"})
        tmp = Path(r._tmp)
        assert (r.state_dir / "audit.jsonl").exists()
        assert not tmp.resolve().is_relative_to(cwd.resolve())
    assert list(cwd.iterdir()) == []
    assert not tmp.exists()


def test_omx_demo_and_omx_mcp_demo_run_the_same_server_without_a_config(cwd, monkeypatch):
    import omx.demo
    from omx import cli, mcp_server
    ran = []
    monkeypatch.setattr(omx.demo, "serve_demo", lambda err=None: ran.append("demo") or 0)
    monkeypatch.delenv("OMX_CONFIG", raising=False)
    assert mcp_server.main(["--demo"]) == 0
    assert cli.main(["demo"]) == 0
    assert ran == ["demo", "demo"] and list(cwd.iterdir()) == []


def test_omx_hyperliquid_without_any_config_serves_the_demo_but_omx_mcp_does_not(cwd, monkeypatch, capsys):
    import omx.demo
    from omx import mcp_server
    ran = []
    monkeypatch.setattr(omx.demo, "serve_demo", lambda err=None: ran.append("demo") or 0)
    monkeypatch.delenv("OMX_CONFIG", raising=False)
    assert mcp_server.hyperliquid_main([]) == 0
    assert ran == ["demo"] and "DEMO" in capsys.readouterr().err
    assert mcp_server.main([]) == 2                      # omx-mcp: a missing config stays an error
    assert ran == ["demo"]


def test_omx_hyperliquid_with_a_named_config_never_falls_back_to_the_demo(cwd, monkeypatch):
    import omx.demo
    from omx import mcp_server
    ran = []
    monkeypatch.setattr(omx.demo, "serve_demo", lambda err=None: ran.append("demo") or 0)
    monkeypatch.delenv("OMX_CONFIG", raising=False)
    assert mcp_server.hyperliquid_main(["--config", "missing.toml"]) == 2
    monkeypatch.setenv("OMX_CONFIG", "missing.toml")
    assert mcp_server.hyperliquid_main([]) == 2
    monkeypatch.delenv("OMX_CONFIG")
    (cwd / "omx.toml").write_text("not = [valid", encoding="utf-8")
    assert mcp_server.hyperliquid_main([]) == 2         # ./omx.toml exists: it is used, never replaced by the demo
    assert ran == []


def test_stdio_process_answers_a_tool_call(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    cwd = tmp_path / "stdio-cwd"
    cwd.mkdir()
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE",
                                                       "HOME", "LOCALAPPDATA", "APPDATA")}
    env["PYTHONPATH"] = str(ROOT)
    params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "omx.mcp_server", "--demo"],
                                   cwd=str(cwd), env=env)

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                q = await s.call_tool("get_quote", {"coin": "BTC"})
                return names, q.structured_content

    names, q = asyncio.run(asyncio.wait_for(go(), 60))
    assert "submit_order" in names and "kill_switch" in names
    assert q["ok"] and q["demo"] is True and q["mid"] > 0
    assert list(cwd.iterdir()) == []
