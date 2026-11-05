"""Move USDC between spot and perps of a TESTNET account (e.g. the test builder wallet).

TESTNET ONLY: the endpoint is fixed to the Hyperliquid testnet. The key is read from a file
path and never printed. Run by the human, in their own PowerShell:

    .venv\\Scripts\\python.exe -B tools\\testnet_usd_class_transfer.py --key C:\\omx-testnet\\builder_test.key --amount 110 --to-perp
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import TESTNET_API_URL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", required=True, help="path of the key file (never printed)")
    ap.add_argument("--amount", type=float, required=True)
    side = ap.add_mutually_exclusive_group(required=True)
    side.add_argument("--to-perp", action="store_true")
    side.add_argument("--to-spot", action="store_true")
    ns = ap.parse_args(argv)
    if not 0 < ns.amount <= 10_000:
        print("amount must be in (0, 10000]", file=sys.stderr)
        return 2
    wallet = Account.from_key(Path(ns.key).read_text(encoding="utf-8").strip())
    addr = wallet.address
    print(f"testnet account {addr}: moving {ns.amount} USDC {'spot -> perps' if ns.to_perp else 'perps -> spot'}")
    # SDK 0.20.0 Info() parses the full spotMeta on start and breaks on testnet's token list
    # (IndexError); a class transfer needs no meta, so pass empty ones.
    empty = {"meta": {"universe": []}, "spot_meta": {"universe": [], "tokens": []}}
    res = Exchange(wallet, TESTNET_API_URL, **empty).usd_class_transfer(ns.amount, ns.to_perp)
    print(json.dumps(res))
    info = Info(TESTNET_API_URL, skip_ws=True, **empty)
    print("perps accountValue:", info.user_state(addr)["marginSummary"]["accountValue"])
    return 0 if isinstance(res, dict) and res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
