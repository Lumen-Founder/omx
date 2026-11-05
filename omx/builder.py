"""Builder-fee disclosure and approval helpers.

A builder fee is charged by Hyperliquid on each fill of an order that carries
`"builder": {"b": address, "f": tenths_bps}`, and only up to the maximum the
user approved for that builder. Approval is a user-signed action
(ApproveBuilderFee) that only the user's MAIN wallet can sign; agent (API)
wallets cannot approve builder fees. The engine never signs it: this module
builds the unsigned payload for the user's own wallet and reads the approved
maximum back from the venue.

Units: the order field `f` is in tenths of a basis point (10 = 1 bps = 0.01 %).
The approval field `maxFeeRate` is a percent string such as "0.1%".
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Callable, Optional

PERP_MAX_TENTHS_BPS = 100            # 0.1 % venue cap for perps
SPOT_MAX_TENTHS_BPS = 1000           # 1 % venue cap for spot
SIGNATURE_CHAIN_ID = "0x66eee"       # value the SDK uses (hyperliquid/utils/signing.py)
APPROVE_BUILDER_FEE_TYPES = [        # matches sign_approve_builder_fee in the SDK
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "maxFeeRate", "type": "string"},
    {"name": "builder", "type": "address"},
    {"name": "nonce", "type": "uint64"},
]
APPROVE_BUILDER_FEE_PRIMARY = "HyperliquidTransaction:ApproveBuilderFee"
_EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PCT = re.compile(r"^\d+(\.\d+)?%$")

Transport = Callable[[str, dict], dict]


def is_address(addr) -> bool:
    return isinstance(addr, str) and bool(_ADDR.match(addr))


def _fmt(d: Decimal) -> str:
    s = f"{d.normalize():f}"
    return "0" if s in ("-0", "") else s


# --- unit conversions -------------------------------------------------------

def tenths_bps_to_bps(tenths: int) -> float:
    return tenths / 10.0


def bps_to_tenths_bps(bps: float) -> int:
    t = round(bps * 10)
    if abs(t - bps * 10) > 1e-9:
        raise ValueError(f"builder fee must be a whole number of tenths of a bps: {bps}")
    return int(t)


def bps_to_percent(bps: float) -> float:
    return bps / 100.0


def percent_to_bps(pct: float) -> float:
    return pct * 100.0


def tenths_bps_to_percent_str(tenths: int) -> str:
    """10 -> "0.01%", the format ApproveBuilderFee expects for maxFeeRate."""
    return _fmt(Decimal(int(tenths)) / Decimal(1000)) + "%"


def percent_str_to_tenths_bps(pct: str) -> int:
    if not isinstance(pct, str) or not _PCT.match(pct):
        raise ValueError(f"fee rate must look like '0.1%': {pct!r}")
    t = Decimal(pct[:-1]) * 1000
    if t != t.to_integral_value():
        raise ValueError(f"fee rate finer than 0.1 bps: {pct}")
    return int(t)


def estimate_builder_fee_usd(notional_usd: float, tenths_bps: int) -> float:
    """Builder fee for one fill of the given notional."""
    return abs(notional_usd) * tenths_bps / 100_000.0


def disclosure(tenths_bps: int) -> str:
    """Plain-language text the UI shows before the user approves."""
    bps = tenths_bps_to_bps(tenths_bps)
    pct = tenths_bps_to_percent_str(tenths_bps)
    return (f"This app charges {bps:g} bps ({pct}) of filled notional on each fill, "
            f"approved by you, revocable at any time. "
            f"Example: a 10,000 USD fill pays {estimate_builder_fee_usd(10_000, tenths_bps):.2f} USD.")


# --- approval payload (signed by the user's main wallet, never by the engine) ---

def build_approve_builder_fee_action(builder_address: str, max_fee_rate_percent_str: str,
                                     nonce_ms: int, is_mainnet: bool,
                                     max_tenths_bps: int = PERP_MAX_TENTHS_BPS) -> dict:
    """Unsigned ApproveBuilderFee for the user's main wallet.

    Returns {"action": what is posted to /exchange with the signature,
             "typed_data": the EIP-712 message the wallet signs (eth_signTypedData_v4),
             "nonce": nonce_ms}.
    Revoking is the same action with maxFeeRate "0%".
    """
    if not is_address(builder_address):
        raise ValueError(f"malformed builder address: {builder_address!r}")
    tenths = percent_str_to_tenths_bps(max_fee_rate_percent_str)
    if tenths > max_tenths_bps:
        raise ValueError(f"max fee {max_fee_rate_percent_str} above cap "
                         f"{tenths_bps_to_percent_str(max_tenths_bps)}")
    if not isinstance(nonce_ms, int) or nonce_ms <= 0:
        raise ValueError("nonce_ms must be a positive int (current ms timestamp)")
    action = {
        "maxFeeRate": max_fee_rate_percent_str,
        "builder": builder_address.lower(),
        "nonce": nonce_ms,
        "type": "approveBuilderFee",
        "signatureChainId": SIGNATURE_CHAIN_ID,
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
    }
    typed = {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": int(SIGNATURE_CHAIN_ID, 16),
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {APPROVE_BUILDER_FEE_PRIMARY: APPROVE_BUILDER_FEE_TYPES,
                  "EIP712Domain": _EIP712_DOMAIN_TYPES},
        "primaryType": APPROVE_BUILDER_FEE_PRIMARY,
        "message": action,
    }
    return {"action": action, "typed_data": typed, "nonce": nonce_ms}


def approval_request_body(approval: dict, signature: dict) -> dict:
    """/exchange body once the user's wallet has returned {r, s, v}."""
    return {"action": approval["action"], "nonce": approval["nonce"], "signature": signature,
            "vaultAddress": None, "expiresAfter": None}


def parse_max_builder_fee(res) -> int:
    """The /info maxBuilderFee answer is a bare integer: the approved maximum in tenths of a bps
    (HL docs: "1 means 0.001%"; 0 = not approved). Anything else raises (fail closed)."""
    if isinstance(res, bool):
        raise ValueError(f"unexpected maxBuilderFee response: {res!r}")
    if isinstance(res, int):
        v = res
    elif isinstance(res, str) and res.strip().isdigit():
        v = int(res.strip())
    else:
        raise ValueError(f"unexpected maxBuilderFee response: {str(res)[:100]!r}")
    if v < 0:
        raise ValueError(f"unexpected maxBuilderFee response: {res!r}")
    return v


def info_max_builder_fee(transport: Transport, user: str, builder: str) -> int:
    """Approved maximum for (user, builder) in tenths of a bps; 0 = not approved."""
    if not is_address(user) or not is_address(builder):
        raise ValueError("user and builder must be 0x addresses")
    res = transport("/info", {"type": "maxBuilderFee", "user": user.lower(), "builder": builder.lower()})
    return parse_max_builder_fee(res)


# --- enforcement --------------------------------------------------------------

BUILDER_NOT_APPROVED = "BUILDER_NOT_APPROVED"
APPROVE_HINT = ("run `omx approve-builder` (your MAIN wallet signs the approval in your browser; OMX never "
                "sees a key), then wait for the daemon's next check (at most 60 s) or restart it")


def builder_of(address: Optional[str], fee_tenths_bps: int) -> Optional[tuple]:
    """(lowercase address, fee) when a builder fee is configured, else None. This tuple is what the
    engine attaches to every child order (ChildOrder.builder) and what venues send as {"b", "f"}."""
    fee = int(fee_tenths_bps or 0)
    if fee <= 0 or not address:
        return None
    if not is_address(address):
        raise ValueError(f"malformed builder address: {address!r}")
    if fee > PERP_MAX_TENTHS_BPS:
        raise ValueError(f"builder fee {fee} tenths bps exceeds the perps cap {PERP_MAX_TENTHS_BPS} (0.1 %)")
    return (address.lower(), fee)


def not_approved_message(builder: tuple, approved: Optional[int], detail: str = "") -> str:
    addr, fee = builder
    have = "unknown" if approved is None else f"{approved} ({tenths_bps_to_percent_str(approved)})"
    why = f"; {detail}" if detail else ""
    return (f"{BUILDER_NOT_APPROVED}: approved max builder fee {have} is below the configured "
            f"{fee} tenths of a bps ({tenths_bps_to_percent_str(fee)}) for builder {addr}{why}. "
            f"New orders are refused until it is approved: {APPROVE_HINT}")
