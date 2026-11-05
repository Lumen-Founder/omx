"""Create a NEW throwaway secp256k1 wallet for TEST use (a testnet builder, a Base Sepolia payer).

    python tools/new_test_wallet.py --out secrets/testnet_builder.key

Writes the hex private key (0x + 64 hex) to --out, refuses to overwrite an existing file, restricts the
file to the current user (POSIX mode 0600; on Windows `icacls /inheritance:r /grant:r <user>:F`, best
effort) and prints ONLY the address. The key never goes to stdout, stderr or a log. Do not use the
result for real funds: it was generated on this machine for testing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

try:                                   # the one permission helper lives in omx.keyfile
    from omx.keyfile import restrict_to_user
except ImportError:                    # run from a source checkout without an install
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from omx.keyfile import restrict_to_user


def create(out_path: Path, restrict: Callable[[Path], Optional[str]] = restrict_to_user) -> tuple[str, Optional[str]]:
    """New key written to out_path (exclusive create). Returns (address, warning or None)."""
    from eth_account import Account
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    acct = Account.create()
    text = "0x" + bytes(acct.key).hex()
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, (text + "\n").encode("ascii"))
    finally:
        os.close(fd)
        text = None
    return acct.address, restrict(out_path)


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr,
         restrict: Callable[[Path], Optional[str]] = restrict_to_user) -> int:
    ap = argparse.ArgumentParser(prog="new_test_wallet", description="create a throwaway TEST wallet key file")
    ap.add_argument("--out", required=True, help="file to create (must not exist)")
    ns = ap.parse_args(argv)
    path = Path(ns.out)
    if path.exists():
        err.write(f"refused: {path} already exists; this tool never overwrites a key file\n")
        return 2
    try:
        address, warn = create(path, restrict)
    except FileExistsError:
        err.write(f"refused: {path} already exists; this tool never overwrites a key file\n")
        return 2
    if warn:
        err.write(f"warning: {warn}\n")
    out.write(address + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
