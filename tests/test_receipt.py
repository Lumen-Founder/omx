"""Execution receipts (omx.receipt): what an intent asked for and what happened, self-checked,
hashed, anchored once in the audit log, and reachable from the agent gateway."""
import json

import pytest

from omx import receipt as R
from omx.audit import AuditLog, FORBIDDEN_KEYS
from omx.engine import Engine
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.sim import CoinSpec, SimConfig, SimVenue

BUILDER = "0x" + "be" * 20
FEE = 10                                   # tenths of a bp = 1 bp
CAPS = dict(default_coin_notional_usd=50_000, max_account_notional_usd=100_000, max_child_notional_usd=20_000)


def engine(tmp_path, builder=True):
    sim = SimVenue(SimConfig(seed=3, start_equity_usd=10_000.0),
                   coins=(CoinSpec("ETH", 3000.0, 0.1, 4, vol_bps_sqrt_s=0.0, trade_rate_hz=0.0),))
    eng = Engine(sim, PolicyConfig(**CAPS), AuditLog(tmp_path / "audit.jsonl"),
                 builder_fee_tenths_bps=FEE if builder else 0, builder_address=BUILDER if builder else None,
                 state_dir=tmp_path / "state")
    return sim, eng


def run(sim, eng, ms, step=100):
    for _ in range(ms // step):
        sim.advance(step)
        eng.step()


def kinds(eng, kind):
    lines = eng.audit.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines if x and json.loads(x)["kind"] == kind]


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def test_final_twap_receipt_adds_up_and_is_anchored_once(tmp_path):
    sim, eng = engine(tmp_path)
    eng.submit(Intent("tw1", "ETH", Side.BUY, 0.5, algo=Algo.TWAP, duration_s=4, slices=4))
    run(sim, eng, 8_000)
    rec = eng.receipt("tw1", network="testnet")
    assert rec["schema"] == R.SCHEMA and rec["final"] and rec["state"] == "done" and rec["network"] == "testnet"
    assert len(rec["children"]) >= 4 and rec["fills"]
    assert sum(f["size"] for f in rec["fills"]) == pytest.approx(0.5)
    assert rec["checks"]["fills_match"] and rec["checks"]["builder_fee_within_rate"]
    assert rec["checks"]["children_builder_as_configured"]
    assert all(c["builder"] == [BUILDER, FEE] for c in rec["children"])
    s = rec["summary"]
    assert s["builder_fee_usd"] == pytest.approx(s["notional_usd"] * FEE / 1e5)
    assert rec["builder"] == {"address": BUILDER, "fee_tenths_bps": FEE}
    assert rec["receipt_hash"] == R.receipt_hash(rec)          # anyone can recompute it
    assert rec["audit_anchor"] and not set(walk_keys(rec)) & FORBIDDEN_KEYS
    again = eng.receipt("tw1", network="testnet")
    assert again["receipt_hash"] == rec["receipt_hash"] and again["audit_anchor"] == rec["audit_anchor"]
    anchored = kinds(eng, "receipt")
    assert len(anchored) == 1 and anchored[0]["data"] == {"intent_id": "tw1", "receipt_hash": rec["receipt_hash"]}
    ok, _, msg = AuditLog.verify(eng.audit.path)
    assert ok, msg


def test_working_receipt_is_not_final_and_not_anchored(tmp_path):
    sim, eng = engine(tmp_path)
    eng.submit(Intent("tw2", "ETH", Side.SELL, 0.4, algo=Algo.TWAP, duration_s=60, slices=6))
    run(sim, eng, 500)
    rec = eng.receipt("tw2")
    assert not rec["final"] and rec["audit_anchor"] is None and not kinds(eng, "receipt")


def test_tampering_changes_the_hash(tmp_path):
    sim, eng = engine(tmp_path)
    eng.submit(Intent("m1", "ETH", Side.BUY, 0.2, algo=Algo.MARKET))
    run(sim, eng, 1_000)
    rec = eng.receipt("m1")
    rec["fills"][0]["builder_fee_usd"] = 0.0
    assert R.receipt_hash(rec) != rec["receipt_hash"]


def test_no_builder_receipt(tmp_path):
    sim, eng = engine(tmp_path, builder=False)
    eng.submit(Intent("m2", "ETH", Side.BUY, 0.2, algo=Algo.MARKET))
    run(sim, eng, 1_000)
    rec = eng.receipt("m2")
    assert rec["builder"] is None and rec["summary"]["builder_fee_usd"] == 0
    assert rec["checks"]["children_builder_as_configured"] and rec["checks"]["builder_fee_within_rate"]


def test_checks_flag_an_overcharge_and_a_wrong_builder():
    intent = {"intent_id": "x"}
    fills = [{"fill_id": "t1", "child_id": "x.1", "px": 3000.0, "size": 1.0, "fee_usd": 1.35,
              "builder_fee_usd": 0.6, "crossed": True, "ts_ms": 1}]        # 2 bp charged on a 1 bp rate
    children = [{"child_id": "x.1", "builder": ["0x" + "11" * 20, FEE]}]
    rec = R.build(intent, "done", {"filled_size": 1.0}, None, children, fills, (BUILDER, FEE))
    assert rec["checks"]["builder_fee_over_rate"] == ["t1"] and not rec["checks"]["builder_fee_within_rate"]
    assert rec["checks"]["children_builder_mismatch"] == ["x.1"]
    assert not R.build(intent, "done", {"filled_size": 2.0}, None, [], fills, None)["checks"]["fills_match"]


def test_unknown_intent_raises_key_error(tmp_path):
    _, eng = engine(tmp_path)
    with pytest.raises(KeyError):
        eng.receipt("nope")
