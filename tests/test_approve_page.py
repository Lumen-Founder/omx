"""`omx approve-builder` page (offline). The typed data the page builds is taken from the rendered page
itself (embedded JSON + the page's own JS run in node with a fake wallet and a fake fetch) and from the
Python mirror, signed with a throwaway key, and compared with hyperliquid-python-sdk 0.20.0's own
sign_user_signed_action / user_signed_payload. No request leaves this machine."""
import copy
import io
import json
import re
import shutil
import subprocess

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from hyperliquid.utils.signing import (recover_user_from_user_signed_action, sign_approve_builder_fee,
                                       sign_user_signed_action, user_signed_payload)

from omx import approve_page as ap
from omx.builder import APPROVE_BUILDER_FEE_PRIMARY, APPROVE_BUILDER_FEE_TYPES
from omx.cli import main
from omx.config import contains_key_like, loads, render

BUILDER = "0x" + "Be" * 20
NONCE = 1_800_000_000_123
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any socket connect during these tests is a bug (binding a loopback port is not a connect)."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def wallet():
    return Account.create()                    # throwaway, exists only in this test


def cfg_for(account, network="testnet", fee=10, builder=BUILDER, tmp=None):
    text = render(account, network, key_path="k", builder_address=builder, fee_tenths_bps=fee)
    return loads(text) if tmp is None else loads(text, base_dir=tmp)


def page_json(page_html: str) -> dict:
    m = re.search(r'<script type="application/json" id="omx-data">(.*?)</script>', page_html, re.S)
    return json.loads(m.group(1))


def sign(wallet, typed):
    s = wallet.sign_message(encode_typed_data(full_message=typed))
    return "0x" + bytes(s.signature).hex()


def sdk_action(rate, builder, nonce):
    """Exactly what Exchange.approve_builder_fee builds before signing."""
    return {"maxFeeRate": rate, "builder": builder, "nonce": nonce, "type": "approveBuilderFee"}


@pytest.mark.parametrize("network", ["testnet", "mainnet"])
def test_page_typed_data_matches_sdk_on_its_chain(wallet, network):
    page = ap.ApprovalPage(cfg_for(wallet.address, network))
    d = page_json(page.html)
    assert d == page.data and d["builder"] == BUILDER.lower() and d["maxFeeRate"] == "0.01%"
    assert d["hyperliquidChain"] == ("Mainnet" if network == "mainnet" else "Testnet")
    action = ap.build_action(d, "0x66EEE", NONCE)                 # the SDK always signs on 0x66eee
    typed = ap.build_typed_data(d, action)
    ours = ap.split_signature(sign(wallet, typed))
    sdk_act = sdk_action("0.01%", BUILDER.lower(), NONCE)
    theirs = sign_approve_builder_fee(wallet, sdk_act, network == "mainnet")   # mutates sdk_act like the SDK
    assert ours == theirs
    assert action == sdk_act                                     # same six action fields and values
    again = sign_user_signed_action(wallet, sdk_action("0.01%", BUILDER.lower(), NONCE), APPROVE_BUILDER_FEE_TYPES,
                                    APPROVE_BUILDER_FEE_PRIMARY, network == "mainnet")
    assert again == ours
    who = recover_user_from_user_signed_action(copy.deepcopy(action), ours, APPROVE_BUILDER_FEE_TYPES,
                                               APPROVE_BUILDER_FEE_PRIMARY, network == "mainnet")
    assert who == wallet.address
    assert ap.exchange_body(action, ours) == {"action": action, "nonce": NONCE, "signature": ours}


@pytest.mark.parametrize("chain", ["0xa4b1", "0x1", "0x66eee", "0xaa36a7"])
def test_page_typed_data_on_the_wallets_chain(wallet, chain):
    d = ap.ApprovalPage(cfg_for(wallet.address)).data
    action = ap.build_action(d, chain, NONCE)
    assert action["signatureChainId"] == chain
    typed = ap.build_typed_data(d, action)
    assert typed["domain"]["chainId"] == int(chain, 16)
    sdk_payload = user_signed_payload(APPROVE_BUILDER_FEE_PRIMARY, APPROVE_BUILDER_FEE_TYPES, dict(action))
    assert encode_typed_data(full_message=typed) == encode_typed_data(full_message=sdk_payload)
    sig = ap.split_signature(sign(wallet, typed))
    who = Account.recover_message(encode_typed_data(full_message=sdk_payload), vrs=[sig["v"], sig["r"], sig["s"]])
    assert who == wallet.address
    assert set(typed["message"]) == {"hyperliquidChain", "maxFeeRate", "builder", "nonce"}


def test_revoke_is_zero_percent(wallet):
    page = ap.ApprovalPage(cfg_for(wallet.address), revoke=True)
    d = page_json(page.html)
    assert d["maxFeeRate"] == "0%" and d["revoke"] is True
    act = ap.build_action(d, "0x66eee", NONCE)
    assert ap.split_signature(sign(wallet, ap.build_typed_data(d, act))) == \
        sign_approve_builder_fee(wallet, sdk_action("0%", BUILDER.lower(), NONCE), False)
    assert "REVOKING" in page.html


def test_page_text_and_no_secrets(wallet):
    page = ap.ApprovalPage(cfg_for(wallet.address))
    h = page.html
    assert "1 bp (basis point) = 0.01 % of filled notional" in h
    assert "omx approve-builder --revoke" in h and "maxFeeRate 0%" in h
    assert wallet.address.lower() in h and BUILDER.lower() in h and "0.01%" in h
    assert "never sees your key" in h and "no field for a private key" in h
    assert "<input" not in h and "<form" not in h and "<textarea" not in h
    assert not contains_key_like(h)
    assert ap.PAGE_LIB_JS in h and ap.PAGE_UI_JS in h
    assert "MAINNET: real funds" in ap.ApprovalPage(cfg_for(wallet.address, "mainnet")).html


def test_approval_rate_rules(wallet):
    c = cfg_for(wallet.address, fee=20)
    assert ap.approval_rate(c, None, False) == ("0.02%", 20, [])
    assert ap.approval_rate(c, "0.1%", False)[:2] == ("0.1%", 100)
    low = ap.approval_rate(c, "0.01%", False)
    assert low[1] == 10 and "BUILDER_NOT_APPROVED" in low[2][0]
    assert ap.approval_rate(c, None, True) == ("0%", 0, [])
    for rate, revoke, msg in (("0.2%", False, "cap"), ("0%", False, "revoke"), ("0.01%", True, "--revoke"),
                              ("abc", False, "percent")):
        with pytest.raises(ValueError):
            ap.approval_rate(c, rate, revoke)
    with pytest.raises(ap.ApprovePageError, match="no builder"):
        ap.approval_rate(loads(render(wallet.address, "testnet", key_path="k")), None, False)
    with pytest.raises(ap.ApprovePageError, match="nothing to approve"):
        ap.approval_rate(cfg_for(wallet.address, fee=0), None, False)


def test_respond_rules_and_headers(wallet):
    page = ap.ApprovalPage(cfg_for(wallet.address))
    page.port = 54321
    host = "127.0.0.1:54321"
    assert page.respond("GET", page.path, host)[0] == 200
    assert page.respond("GET", page.path + "?x=1", "localhost:54321")[0] == 200
    assert page.respond("GET", "/", host)[0] == 404
    assert page.respond("GET", "/favicon.ico", host)[0] == 404
    assert page.respond("GET", page.path, "evil.example:54321")[0] == 403          # DNS rebinding
    assert page.respond("POST", page.path, host)[0] == 405
    st, headers, body = page.respond("GET", page.path, host)
    csp = headers["Content-Security-Policy"]
    assert "connect-src https://api.hyperliquid-testnet.xyz;" in csp and f"'nonce-{page.nonce}'" in csp
    assert "default-src 'none'" in csp and headers["Cache-Control"] == "no-store"
    assert body.decode() == page.html and len(page.token) >= 16


def test_server_binds_loopback_only(wallet):
    page = ap.ApprovalPage(cfg_for(wallet.address))
    srv = ap.make_server(page)
    try:
        assert srv.server_address[0] == "127.0.0.1" and page.port == srv.server_address[1] > 0
        assert page.url() == f"http://127.0.0.1:{page.port}/{page.token}"
    finally:
        srv.server_close()
    with pytest.raises(ap.ApprovePageError):
        ap.make_server(page, host="0.0.0.0")


def test_serve_prints_url_opens_browser_and_stops(wallet):
    opened, out = [], io.StringIO()
    code = ap.serve(cfg_for(wallet.address), out=out, timeout_s=0, opener=opened.append, sleep=lambda s: None)
    assert code == 0 and len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")
    assert opened[0] in out.getvalue() and "never sees a key" in out.getvalue()


def test_cli_approve_builder(tmp_path, wallet):
    p = tmp_path / "omx.toml"
    p.write_text(render(wallet.address, "testnet", key_path="k", builder_address=BUILDER, fee_tenths_bps=10),
                 encoding="utf-8")
    seen = []

    def fake_serve(cfg, **kw):
        seen.append(kw)
        return 0

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--config", str(p), *argv], out=out, err=err, serve_page=fake_serve)
        return code, out.getvalue(), err.getvalue()
    code, out, _ = run("approve-builder", "--no-browser")
    assert code == 0 and "0.01%" in out
    assert seen[-1] == {"max_fee_rate": None, "revoke": False, "out": seen[-1]["out"], "open_browser": False,
                        "timeout_s": 900.0}
    assert run("approve-builder", "--revoke")[0] == 0 and seen[-1]["revoke"] is True
    code, _, err = run("approve-builder", "--revoke", "--max-fee-rate", "0.01%")
    assert code == 1 and "--revoke" in err
    code, _, err = run("approve-builder", "--max-fee-rate", "0.5%")
    assert code == 1 and "cap" in err
    assert len(seen) == 2


# ------------------------------------------------------------ the page's own JS, run in node

HARNESS = r"""
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const els = {};
function el(id) { return els[id] || (els[id] = {id: id, textContent: "", disabled: false, listeners: {},
                                                  addEventListener(t, f) { this.listeners[t] = f; }}); }
el("omx-data").textContent = input.page_json;
global.document = {getElementById: el};
const requests = [], fetches = [];
global.window = {ethereum: {request: async function (req) {
  requests.push(req);
  if (req.method === "eth_requestAccounts") return [input.wallet_account];
  if (req.method === "eth_chainId") return input.chain;
  if (req.method === "eth_signTypedData_v4") return input.signature;
  throw new Error("unexpected wallet method " + req.method);
}}};
global.fetch = async function (url, opts) {
  fetches.push({url: url, method: opts.method, headers: opts.headers, body: JSON.parse(opts.body)});
  return {status: 200, text: async function () {
    return url.endsWith("/info") ? String(input.approved) : '{"status":"ok","response":{"type":"default"}}'; }};
};
Date.now = function () { return input.nonce; };
eval(fs.readFileSync(process.argv[3], "utf8"));
(async function () {
  await el("go").listeners.click();
  const lib = {action: omxBuildAction(JSON.parse(input.page_json), input.chain, input.nonce)};
  lib.typed = omxTypedData(JSON.parse(input.page_json), lib.action);
  lib.split = input.signature ? omxSplitSig(input.signature) : null;
  console.log(JSON.stringify({requests: requests, fetches: fetches, log: el("log").textContent,
                              typedShown: el("typed").textContent, lib: lib}));
})();
"""


def run_page_js(tmp_path, page, wallet_account, chain, signature, approved=10):
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>', page.html, re.S).group(1)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    m = re.search(r'<script type="application/json" id="omx-data">(.*?)</script>', page.html, re.S)
    (tmp_path / "in.json").write_text(json.dumps({"page_json": m.group(1), "wallet_account": wallet_account,
                                                   "chain": chain, "signature": signature, "nonce": NONCE,
                                                   "approved": approved}), encoding="utf-8")
    r = subprocess.run([NODE, str(tmp_path / "harness.js"), str(tmp_path / "in.json"), str(tmp_path / "page.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("network,chain", [("testnet", "0x66eee"), ("mainnet", "0xa4b1"), ("testnet", "0xAA36A7")])
def test_page_js_signs_checks_account_and_posts_from_the_browser(tmp_path, wallet, network, chain):
    page = ap.ApprovalPage(cfg_for(wallet.address, network))
    d = page.data
    action = ap.build_action(d, chain, NONCE)
    typed = ap.build_typed_data(d, action)
    sig_hex = sign(wallet, typed)
    res = run_page_js(tmp_path, page, wallet.address, chain, sig_hex)
    # the page's own functions agree with the Python mirror
    assert res["lib"]["action"] == action and res["lib"]["typed"] == typed
    assert res["lib"]["split"] == ap.split_signature(sig_hex)
    # wallet calls: accounts, chain, then the v4 signature request for the MAIN account
    assert [q["method"] for q in res["requests"]] == ["eth_requestAccounts", "eth_chainId", "eth_signTypedData_v4"]
    p = res["requests"][2]["params"]
    assert p[0] == wallet.address and json.loads(p[1]) == typed
    # the browser POSTs {action, nonce, signature} to the network's /exchange, then re-reads maxBuilderFee
    api = ap.MAINNET_API_URL if network == "mainnet" else ap.TESTNET_API_URL
    ex, info = res["fetches"]
    assert ex["url"] == api + "/exchange" and ex["method"] == "POST"
    sig = ap.split_signature(sig_hex)
    assert ex["body"] == {"action": action, "nonce": NONCE, "signature": sig}
    if chain.lower() == "0x66eee":            # the only chain the SDK itself signs on
        assert sig == sign_user_signed_action(wallet, sdk_action("0.01%", BUILDER.lower(), NONCE),
                                              APPROVE_BUILDER_FEE_TYPES, APPROVE_BUILDER_FEE_PRIMARY,
                                              network == "mainnet")
    who = recover_user_from_user_signed_action(copy.deepcopy(ex["body"]["action"]), ex["body"]["signature"],
                                               APPROVE_BUILDER_FEE_TYPES, APPROVE_BUILDER_FEE_PRIMARY,
                                               network == "mainnet")
    assert who == wallet.address
    assert info == {"url": api + "/info", "method": "POST", "headers": {"Content-Type": "application/json"},
                    "body": {"type": "maxBuilderFee", "user": wallet.address.lower(), "builder": BUILDER.lower()}}
    assert "enough, OMX can trade" in res["log"] and json.loads(res["typedShown"]) == typed


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_js_refuses_another_account(tmp_path, wallet):
    page = ap.ApprovalPage(cfg_for(wallet.address))
    other = Account.create().address
    res = run_page_js(tmp_path, page, other, "0x66eee", None)
    assert [q["method"] for q in res["requests"]] == ["eth_requestAccounts"]      # never asked to sign
    assert res["fetches"] == [] and "REFUSED" in res["log"] and wallet.address.lower() in res["log"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_js_reports_a_low_approval(tmp_path, wallet):
    page = ap.ApprovalPage(cfg_for(wallet.address, fee=20), max_fee_rate="0.02%")
    action = ap.build_action(page.data, "0x66eee", NONCE)
    res = run_page_js(tmp_path, page, wallet.address, "0x66eee", sign(wallet, ap.build_typed_data(page.data, action)),
                      approved=10)
    assert "below it, OMX refuses new orders" in res["log"]
