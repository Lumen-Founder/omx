"""Route alternatives in preview_order: listed from the intent alone, with OMX's own fee shown but
never deciding what is listed."""
import inspect

from omx import mcp_server as M
from omx.types import Algo, Intent, Side


def twap(duration_s, size=1.0, algo=Algo.TWAP):
    return Intent("r1", "ETH", Side.BUY, size, algo=algo, duration_s=duration_s, slices=10)


def test_patient_twap_lists_the_native_twap_with_zero_builder_fee():
    alts = M.route_alternatives(twap(3600), 3000.0, 0.3)
    assert [a["route"] for a in alts] == ["hyperliquid_native_twap"]
    a = alts[0]
    assert a["builder_fee_usd"] == 0.0 and a["omx_builder_fee_bound_usd"] == 0.3
    assert a["available_in_omx"] is False
    assert any("do not cover" in f for f in a["facts"]) and "not a forecast" in a["evidence"]


def test_outside_the_native_range_nothing_is_listed():
    assert M.route_alternatives(twap(60), 3000.0, 0.3) == []                  # < 5 minutes
    assert M.route_alternatives(twap(8 * 86400), 3000.0, 0.3) == []           # > 7 days
    assert M.route_alternatives(twap(3600), 50.0, 0.005) == []                # < $100
    assert M.route_alternatives(twap(3600, algo=Algo.MARKET), 3000.0, 0.3) == []
    assert [a["route"] for a in M.route_alternatives(twap(600, algo=Algo.POV), 3000.0, 0.3)] == \
        ["hyperliquid_native_twap"]


def test_our_fee_does_not_change_what_is_listed():
    for fee in (0.0, 0.3, 1e6):
        assert [a["route"] for a in M.route_alternatives(twap(3600), 3000.0, fee)] == ["hyperliquid_native_twap"]
    src = inspect.getsource(M.route_alternatives)
    body = src[src.index('"""', src.index('"""') + 3) + 3: src.index("out.append")]
    assert "omx_builder_fee_usd" not in body                                   # not in the listing condition


def test_preview_order_carries_the_alternatives(tmp_path, monkeypatch):
    from test_demo import call
    from omx.demo import DemoRuntime
    monkeypatch.chdir(tmp_path)
    with DemoRuntime(start=False) as rt:
        pv = call(rt.server(), "preview_order", {"coin": "ETH", "side": "buy", "size": 0.1, "algo": "twap",
                                                 "duration_s": 600, "slices": 10})
        assert pv["ok"] and [a["route"] for a in pv["alternatives"]] == ["hyperliquid_native_twap"]
        short = call(rt.server(), "preview_order", {"coin": "ETH", "side": "buy", "size": 0.1, "algo": "twap",
                                                    "duration_s": 10, "slices": 5})
        assert short["alternatives"] == []
