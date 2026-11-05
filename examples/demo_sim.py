"""Offline demo: the same 10 ETH buy executed three ways on the local simulator.

Shows what OMX reports to a user: execution cost vs arrival mid, fee split
(exchange vs builder), fill ratio and post-fill markouts. Simulator market
dynamics are model assumptions (see omx/venue/sim.py), not Hyperliquid data.

    .venv\\Scripts\\python.exe -B examples\\demo_sim.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omx.audit import AuditLog
from omx.engine import Engine
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.sim import SimConfig, SimVenue

CAPS = PolicyConfig(default_coin_notional_usd=60_000, max_account_notional_usd=60_000,
                    max_child_notional_usd=31_000, max_leverage=5.0)
RUNS = [
    ("market", Intent("demo-mkt", "ETH", Side.BUY, 10.0, algo=Algo.MARKET, max_slippage_bps=30)),
    ("twap_ioc", Intent("demo-twap", "ETH", Side.BUY, 10.0, algo=Algo.TWAP, duration_s=120, slices=12,
                        max_slippage_bps=30)),
    ("twap_post_only", Intent("demo-twap-po", "ETH", Side.BUY, 10.0, algo=Algo.TWAP, duration_s=120, slices=12,
                              post_only=True, max_slippage_bps=30)),
]


def main():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, intent in RUNS:
            sim = SimVenue(SimConfig(seed=7, start_equity_usd=20_000.0))
            audit = AuditLog(Path(tmp) / f"{name}.jsonl")
            eng = Engine(sim, CAPS, audit, builder_fee_tenths_bps=10, state_dir=Path(tmp) / name)
            eng.submit(intent)
            for _ in range(1_800):                  # 180 s at 100 ms steps
                sim.advance(100)
                eng.step()
            rep = eng.status(intent.intent_id)
            ok, n, _ = AuditLog.verify(audit.path)
            out[name] = {"state": rep.state, "filled": rep.filled_size, "avg_px": rep.avg_px,
                         "shortfall_bps": rep.shortfall_bps, "exch_fee_usd": round(rep.exch_fee_usd, 4),
                         "builder_fee_usd": round(rep.builder_fee_usd, 4), "children_sent": rep.children_sent,
                         "children_blocked": rep.children_blocked, "audit_records": n, "audit_ok": ok,
                         "tca": eng.tca(intent.intent_id)}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
