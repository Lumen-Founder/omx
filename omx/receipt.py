"""Execution receipt: one verifiable record of what an intent asked for and what happened.

A receipt joins the intent, its state, every child order (cloid, venue oid, price, size, time in
force, status, builder field), every fill (venue trade id, price, size, exchange fee, builder fee,
maker/taker), the transaction cost report and three self-checks:

- fills_match: the fills add up to the filled size the engine reports;
- builder_fee_within_rate: no fill was charged a builder fee above the configured rate;
- children_builder_as_configured: every child carried exactly the configured builder (or none). A native
  TWAP record (route hyperliquid_native_twap) is exempt: the protocol's twapOrder has no builder field, so it
  carries none by design and earns OMX nothing.

receipt_hash is the sha256 of the canonical JSON of the receipt without `receipt_hash`,
`audit_anchor` and `post_trade`, so anyone can recompute it. `post_trade` holds the TCA markouts, which
fill in as their horizons pass after the intent ends; they are kept out of the hash so a final receipt
has one hash. A final receipt (intent in a terminal state) is anchored
once in the hash-chained audit log as a `receipt` record; `audit_anchor` is that record's line hash.
Fill ids and cloids let a third party match every line against the venue's own fill history.
Receipts hold no keys or signatures.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

SCHEMA = "omx.receipt/1"
TERMINAL = ("done", "cancelled", "rejected", "killed")
FEE_EPS_USD = 1e-6                    # venue fees are reported rounded


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


UNHASHED = ("receipt_hash", "audit_anchor", "post_trade")


def receipt_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k not in UNHASHED}
    return hashlib.sha256(canonical(body)).hexdigest()


def fee_rate(tenths_bps: int) -> float:
    """Builder fee as a fraction of notional: 10 tenths of a bp = 1 bp = 0.0001."""
    return tenths_bps / 100_000.0


def build(intent: dict, state: str, report: dict, tca_report: Optional[dict], children: list, fills: list,
          builder: Optional[tuple], network: Optional[str] = None) -> dict:
    """intent: to_record(Intent); report: IntentReport as a dict; children / fills: lists of plain dicts
    (see Engine.receipt); builder: (address, fee tenths bps) or None."""
    filled = sum(f["size"] for f in fills)
    rate = fee_rate(builder[1]) if builder else 0.0
    over = [f["fill_id"] or f["child_id"] for f in fills
            if f["builder_fee_usd"] > f["size"] * f["px"] * rate + FEE_EPS_USD]
    want = [builder[0], builder[1]] if builder else None
    tca_report = dict(tca_report) if isinstance(tca_report, dict) else tca_report
    markouts = tca_report.pop("markouts", None) if isinstance(tca_report, dict) else None
    wrong = [c["child_id"] for c in children
             if c["builder"] != (None if c.get("route") == "hyperliquid_native_twap" else want)]
    rec = {
        "schema": SCHEMA,
        "network": network,
        "intent_id": intent["intent_id"],
        "final": state in TERMINAL,
        "state": state,
        "intent": intent,
        "builder": {"address": builder[0], "fee_tenths_bps": builder[1]} if builder else None,
        "summary": {
            "filled_size": report.get("filled_size"),
            "avg_px": report.get("avg_px"),
            "arrival_mid": report.get("arrival_mid"),
            "shortfall_bps": report.get("shortfall_bps"),
            "exchange_fee_usd": sum(f["fee_usd"] for f in fills),
            "builder_fee_usd": sum(f["builder_fee_usd"] for f in fills),
            "notional_usd": sum(f["size"] * f["px"] for f in fills),
            "children_sent": report.get("children_sent"),
            "children_blocked": report.get("children_blocked"),
            "block_reasons": list(report.get("block_reasons") or []),
            "message": report.get("message", ""),
        },
        "tca": tca_report,
        "children": children,
        "fills": fills,
        "checks": {
            "fills_match": abs(filled - (report.get("filled_size") or 0.0)) <= 1e-9 * max(1.0, filled),
            "builder_fee_within_rate": not over,
            "builder_fee_over_rate": over,
            "children_builder_as_configured": not wrong,
            "children_builder_mismatch": wrong,
        },
    }
    rec["receipt_hash"] = receipt_hash(rec)
    rec["audit_anchor"] = None
    rec["post_trade"] = {"markouts": markouts,
                         "note": "fills in as horizons pass after the end; not part of receipt_hash"}
    return rec
