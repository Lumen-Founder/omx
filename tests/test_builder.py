"""Offline tests for builder-fee helpers. The approval payload is checked against
the SDK by signing it with a throwaway key both ways."""
import copy

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_hex
from hyperliquid.utils.signing import (recover_user_from_user_signed_action, sign_approve_builder_fee,
                                       user_signed_payload)

from omx import builder as b

BUILDER = "0x" + "Cd" * 20
USER = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any socket connect during these tests is a bug."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_unit_conversions():
    assert b.tenths_bps_to_bps(10) == 1.0 and b.bps_to_tenths_bps(2.5) == 25
    assert b.bps_to_percent(10) == 0.1 and b.percent_to_bps(0.01) == 1.0
    assert b.tenths_bps_to_percent_str(10) == "0.01%"
    assert b.tenths_bps_to_percent_str(100) == "0.1%"
    assert b.tenths_bps_to_percent_str(1) == "0.001%"
    assert b.tenths_bps_to_percent_str(0) == "0%"
    assert b.percent_str_to_tenths_bps("0.1%") == 100 and b.percent_str_to_tenths_bps("0.001%") == 1
    for bad in ("0.0001%", "0.1", "abc%", "-1%"):
        with pytest.raises(ValueError):
            b.percent_str_to_tenths_bps(bad)
    with pytest.raises(ValueError):
        b.bps_to_tenths_bps(0.05)


def test_fee_estimate_and_disclosure():
    assert b.estimate_builder_fee_usd(10_000, 10) == pytest.approx(1.0)      # 1 bps of 10k
    assert b.estimate_builder_fee_usd(-2_000, 100) == pytest.approx(2.0)     # 10 bps, sign-free
    text = b.disclosure(25)
    assert text.startswith("This app charges 2.5 bps (0.025%) of filled notional on each fill")
    assert "approved by you, revocable at any time" in text and "2.50 USD" in text


@pytest.mark.parametrize("mainnet", [True, False])
def test_approve_payload_matches_sdk(mainnet):
    nonce = 1_800_000_000_123
    ours = b.build_approve_builder_fee_action(BUILDER, "0.01%", nonce, mainnet)
    act = ours["action"]
    assert act["type"] == "approveBuilderFee" and act["builder"] == BUILDER.lower()
    assert act["signatureChainId"] == "0x66eee" and act["hyperliquidChain"] == ("Mainnet" if mainnet else "Testnet")
    assert ours["typed_data"]["primaryType"] == "HyperliquidTransaction:ApproveBuilderFee"
    assert [f["name"] for f in ours["typed_data"]["types"][ours["typed_data"]["primaryType"]]] == \
        ["hyperliquidChain", "maxFeeRate", "builder", "nonce"]
    # SDK's own construction (Exchange.approve_builder_fee + sign_user_signed_action)
    sdk_act = {"maxFeeRate": "0.01%", "builder": BUILDER.lower(), "nonce": nonce, "type": "approveBuilderFee"}
    wallet = Account.create()                         # throwaway, exists only in this test
    sdk_sig = sign_approve_builder_fee(wallet, sdk_act, mainnet)    # mutates sdk_act like the SDK does
    assert act == sdk_act
    assert ours["typed_data"] == user_signed_payload(ours["typed_data"]["primaryType"],
                                                     b.APPROVE_BUILDER_FEE_TYPES, sdk_act)
    signed = wallet.sign_message(encode_typed_data(full_message=ours["typed_data"]))
    our_sig = {"r": to_hex(signed["r"]), "s": to_hex(signed["s"]), "v": signed["v"]}
    assert our_sig == sdk_sig
    who = recover_user_from_user_signed_action(copy.deepcopy(act), our_sig, b.APPROVE_BUILDER_FEE_TYPES,
                                               b.APPROVE_BUILDER_FEE_PRIMARY, mainnet)
    assert who == wallet.address
    body = b.approval_request_body(ours, our_sig)
    assert body["action"] is act and body["nonce"] == nonce and body["vaultAddress"] is None


def test_approve_refusals():
    with pytest.raises(ValueError, match="builder address"):
        b.build_approve_builder_fee_action("0x123", "0.01%", 1, True)
    with pytest.raises(ValueError, match="above cap"):
        b.build_approve_builder_fee_action(BUILDER, "0.2%", 1, True)
    with pytest.raises(ValueError):
        b.build_approve_builder_fee_action(BUILDER, "1 bps", 1, True)
    with pytest.raises(ValueError, match="nonce"):
        b.build_approve_builder_fee_action(BUILDER, "0.01%", 0, True)
    # revocation is the same action at 0 %
    assert b.build_approve_builder_fee_action(BUILDER, "0%", 5, True)["action"]["maxFeeRate"] == "0%"


def test_info_max_builder_fee():
    calls = []

    def transport(path, payload):
        calls.append((path, payload))
        return 10

    assert b.info_max_builder_fee(transport, USER, BUILDER) == 10
    assert calls == [("/info", {"type": "maxBuilderFee", "user": USER, "builder": BUILDER.lower()})]
    assert b.info_max_builder_fee(lambda p, q: 0, USER, BUILDER) == 0
    with pytest.raises(ValueError):
        b.info_max_builder_fee(transport, "nope", BUILDER)
