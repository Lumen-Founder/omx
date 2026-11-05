"""`omx approve-builder`: a one-page local site where the user's own browser wallet signs
ApproveBuilderFee and sends it to Hyperliquid itself.

The Python side only serves a static page on 127.0.0.1 (random port, random path) and never receives a
key or a signature: the page's JS asks the injected wallet (window.ethereum, EIP-1193) for the account
(eth_requestAccounts) and chain (eth_chainId), builds the EIP-712 typed data exactly as
hyperliquid-python-sdk 0.20.0 does for a user-signed action (hyperliquid/utils/signing.py
user_signed_payload + sign_approve_builder_fee), checks that the wallet account is cfg.account_address
(refuses otherwise), asks the wallet to sign (eth_signTypedData_v4), POSTs {action, nonce, signature}
from the browser to <api>/exchange, shows the answer and re-reads /info maxBuilderFee.

Typed data (SDK 0.20.0, verified from the installed source):
  domain      {name "HyperliquidSignTransaction", version "1", chainId int(signatureChainId, 16),
               verifyingContract 0x0000000000000000000000000000000000000000}
  primaryType "HyperliquidTransaction:ApproveBuilderFee"
  fields      hyperliquidChain string, maxFeeRate string, builder address, nonce uint64
  action      {type "approveBuilderFee", hyperliquidChain "Mainnet"|"Testnet", signatureChainId hex,
               maxFeeRate "0.01%", builder lowercase, nonce ms}; the SDK hard-codes signatureChainId
               "0x66eee", the page uses the wallet's current chain (the SDK comment: "can be any chain";
               a wallet signs v4 typed data only for its active chain id).
Revoke = the same action with maxFeeRate "0%".

build_action / build_typed_data / split_signature are the Python mirror of the page's PAGE_LIB_JS
functions (omxBuildAction / omxTypedData / omxSplitSig); the tests check both against the SDK.
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, TextIO

from .builder import (APPROVE_BUILDER_FEE_PRIMARY, APPROVE_BUILDER_FEE_TYPES, PERP_MAX_TENTHS_BPS,
                      _EIP712_DOMAIN_TYPES, builder_of, percent_str_to_tenths_bps, tenths_bps_to_percent_str)

from .venue.hyperliquid import MAINNET_API_URL, TESTNET_API_URL

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEFAULT_TIMEOUT_S = 900


class ApprovePageError(ValueError):
    pass


# ---------------------------------------------------------------- what the page signs

def approval_rate(cfg, max_fee_rate: Optional[str], revoke: bool) -> tuple[str, int, list[str]]:
    """(canonical percent string, tenths of a bps, warnings) for this config and these flags."""
    if not cfg.builder_address:
        raise ApprovePageError("no builder configured: set [builder] address (and fee_tenths_bps) in the config")
    if revoke:
        if max_fee_rate is not None:
            raise ApprovePageError("--revoke signs maxFeeRate 0%; do not pass --max-fee-rate with it")
        return "0%", 0, []
    if builder_of(cfg.builder_address, cfg.builder_fee_tenths_bps) is None:
        raise ApprovePageError("builder.fee_tenths_bps is 0: nothing to approve (use --revoke to revoke)")
    t = cfg.builder_fee_tenths_bps if max_fee_rate is None else percent_str_to_tenths_bps(max_fee_rate.strip())
    if t <= 0:
        raise ApprovePageError("a max fee rate of 0% is a revocation: use --revoke")
    if t > PERP_MAX_TENTHS_BPS:
        raise ApprovePageError(f"max fee rate above Hyperliquid's perps cap {tenths_bps_to_percent_str(PERP_MAX_TENTHS_BPS)}")
    warn = []
    if t < cfg.builder_fee_tenths_bps:
        warn.append(f"{tenths_bps_to_percent_str(t)} is below the configured fee "
                    f"{tenths_bps_to_percent_str(cfg.builder_fee_tenths_bps)}: the daemon will stay "
                    "BUILDER_NOT_APPROVED and Hyperliquid will reject OMX orders")
    return tenths_bps_to_percent_str(t), t, warn


def typed_template() -> dict:
    """The fixed part of the EIP-712 message (everything but domain.chainId and the message)."""
    return {"domain": {"name": "HyperliquidSignTransaction", "version": "1", "verifyingContract": ZERO_ADDRESS},
            "types": {APPROVE_BUILDER_FEE_PRIMARY: APPROVE_BUILDER_FEE_TYPES, "EIP712Domain": _EIP712_DOMAIN_TYPES},
            "primaryType": APPROVE_BUILDER_FEE_PRIMARY}


def page_data(cfg, max_fee_rate: str, max_tenths: int, revoke: bool) -> dict:
    """Everything the page needs; all public. Embedded in the page as JSON."""
    mainnet = cfg.network == "mainnet"
    f = cfg.builder_fee_tenths_bps
    return {"network": cfg.network, "hyperliquidChain": "Mainnet" if mainnet else "Testnet",
            "apiUrl": MAINNET_API_URL if mainnet else TESTNET_API_URL,
            "account": cfg.account_address.lower(), "builder": cfg.builder_address.lower(),
            "maxFeeRate": max_fee_rate, "maxFeeTenthsBps": max_tenths, "configuredTenthsBps": f,
            "configuredRate": tenths_bps_to_percent_str(f), "revoke": bool(revoke), "typed": typed_template()}


def build_action(d: dict, chain_id_hex: str, nonce_ms: int) -> dict:
    """Mirror of omxBuildAction in PAGE_LIB_JS."""
    return {"type": "approveBuilderFee", "hyperliquidChain": d["hyperliquidChain"],
            "signatureChainId": hex(int(chain_id_hex, 16)), "maxFeeRate": d["maxFeeRate"],
            "builder": d["builder"].lower(), "nonce": int(nonce_ms)}


def build_typed_data(d: dict, action: dict) -> dict:
    """Mirror of omxTypedData in PAGE_LIB_JS: the message holds exactly the typed fields."""
    t = d["typed"]
    msg = {f["name"]: action[f["name"]] for f in t["types"][t["primaryType"]]}
    return {"domain": {**t["domain"], "chainId": int(action["signatureChainId"], 16)}, "types": t["types"],
            "primaryType": t["primaryType"], "message": msg}


def split_signature(sig_hex: str) -> dict:
    """Mirror of omxSplitSig: 65-byte r||s||v hex -> {r, s, v} as the SDK's sign_inner returns it."""
    h = sig_hex[2:] if sig_hex.startswith("0x") else sig_hex
    if len(h) != 130:
        raise ValueError("unexpected signature length")
    v = int(h[128:130], 16)
    if v < 27:
        v += 27
    return {"r": hex(int(h[0:64], 16)), "s": hex(int(h[64:128], 16)), "v": v}


def exchange_body(action: dict, signature: dict) -> dict:
    """Mirror of omxExchangeBody: what the page POSTs to /exchange."""
    return {"action": action, "nonce": action["nonce"], "signature": signature}


# ---------------------------------------------------------------- the page

PAGE_LIB_JS = r"""
function omxBuildAction(d, chainIdHex, nonceMs) {
  return {type: "approveBuilderFee", hyperliquidChain: d.hyperliquidChain,
          signatureChainId: "0x" + BigInt(chainIdHex).toString(16), maxFeeRate: d.maxFeeRate,
          builder: d.builder.toLowerCase(), nonce: Number(nonceMs)};
}
function omxTypedData(d, action) {
  const t = d.typed, message = {};
  for (const f of t.types[t.primaryType]) message[f.name] = action[f.name];
  const domain = Object.assign({}, t.domain, {chainId: Number(BigInt(action.signatureChainId))});
  return {domain: domain, types: t.types, primaryType: t.primaryType, message: message};
}
function omxSplitSig(sig) {
  const h = sig.startsWith("0x") ? sig.slice(2) : sig;
  if (h.length !== 130) throw new Error("unexpected signature length");
  let v = parseInt(h.slice(128, 130), 16);
  if (v < 27) v += 27;
  return {r: "0x" + BigInt("0x" + h.slice(0, 64)).toString(16),
          s: "0x" + BigInt("0x" + h.slice(64, 128)).toString(16), v: v};
}
function omxExchangeBody(action, signature) {
  return {action: action, nonce: action.nonce, signature: signature};
}
"""

PAGE_UI_JS = r"""
(function () {
  const d = JSON.parse(document.getElementById("omx-data").textContent);
  const log = document.getElementById("log"), btn = document.getElementById("go");
  function say(text) { log.textContent += text + "\n"; }
  async function post(path, body) {
    const r = await fetch(d.apiUrl + path, {method: "POST", headers: {"Content-Type": "application/json"},
                                           body: JSON.stringify(body)});
    const text = await r.text();
    return {status: r.status, text: text};
  }
  async function reread() {
    const r = await post("/info", {type: "maxBuilderFee", user: d.account, builder: d.builder});
    say("maxBuilderFee now (HTTP " + r.status + "): " + r.text + " tenths of a basis point");
    const n = Number(r.text);
    if (Number.isInteger(n)) {
      say("= " + (n / 1000) + " % of filled notional. OMX is configured for " + d.configuredTenthsBps +
          " (" + d.configuredRate + "): " + (n >= d.configuredTenthsBps ? "enough, OMX can trade."
                                                                        : "below it, OMX refuses new orders."));
    }
  }
  async function run() {
    btn.disabled = true;
    log.textContent = "";
    try {
      if (!window.ethereum) { say("No browser wallet found (window.ethereum). Unlock or install one and reload."); return; }
      const accounts = await window.ethereum.request({method: "eth_requestAccounts"});
      const who = String(accounts[0] || "").toLowerCase();
      if (who !== d.account) {
        say("REFUSED: the wallet's selected account is " + (who || "none") + ", but this approval must be " +
            "signed by your MAIN account " + d.account + ". Switch accounts in the wallet and press the button again.");
        return;
      }
      const chainId = await window.ethereum.request({method: "eth_chainId"});
      const action = omxBuildAction(d, chainId, Date.now());
      const typed = omxTypedData(d, action);
      document.getElementById("typed").textContent = JSON.stringify(typed, null, 2);
      say("Asking the wallet to sign " + typed.primaryType + " (maxFeeRate " + action.maxFeeRate +
          ", builder " + action.builder + ", " + action.hyperliquidChain + ", wallet chain " + action.signatureChainId + ")...");
      const sig = await window.ethereum.request({method: "eth_signTypedData_v4",
                                                 params: [accounts[0], JSON.stringify(typed)]});
      const body = omxExchangeBody(action, omxSplitSig(sig));
      say("Signed. Sending it from this browser to " + d.apiUrl + "/exchange ...");
      const r = await post("/exchange", body);
      say("Hyperliquid answered (HTTP " + r.status + "): " + r.text);
      await reread();
    } catch (e) {
      say("Stopped: " + (e && e.message ? e.message : String(e)));
    } finally {
      btn.disabled = false;
    }
  }
  btn.addEventListener("click", run);
  document.getElementById("check").addEventListener("click", function () {
    reread().catch(function (e) { say("Stopped: " + (e && e.message ? e.message : String(e))); });
  });
})();
"""

PAGE_CSS = """
:root { --bg: #ffffff; --fg: #1b1f23; --muted: #57606a; --warn: #b42318; --box: #f6f8fa; --line: #d0d7de; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9da7b3; --warn: #ff7b72;
  --box: #161b22; --line: #30363d; } }
body { background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 16px; }
main { max-width: 760px; margin: 0 auto; }
h1 { font-size: 22px; margin: 8px 0 12px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; border: 1px solid var(--line); font-weight: 600; }
.mainnet { color: var(--warn); border-color: var(--warn); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; }
dt { color: var(--muted); } dd { margin: 0; word-break: break-all; font-family: ui-monospace, monospace; }
.box { background: var(--box); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; margin: 12px 0; }
button { font: inherit; padding: 8px 14px; margin: 4px 8px 4px 0; cursor: pointer; }
pre { white-space: pre-wrap; word-break: break-all; background: var(--box); padding: 10px; border-radius: 6px; }
code { font-family: ui-monospace, monospace; }
"""


def _plain_words(d: dict) -> list[str]:
    acct, b, api = d["account"], d["builder"], d["apiUrl"]
    if d["revoke"]:
        what = [f"You are REVOKING the builder fee approval for builder {b}: you sign maxFeeRate 0%.",
                "After this, Hyperliquid rejects orders that carry this builder code, and OMX refuses new "
                "orders until you approve again or set fee_tenths_bps = 0 in the config."]
    else:
        bps = d["maxFeeTenthsBps"] / 10
        what = [f"You approve builder {b} to charge at most {d['maxFeeRate']} ({bps:g} bp) of the notional of each "
                "fill of orders that carry its builder code.",
                "1 bp (basis point) = 0.01 % of filled notional. Example: on a 10,000 USD fill, 1 bp is 1.00 USD; "
                f"at {d['maxFeeRate']} it is {10_000 * d['maxFeeTenthsBps'] / 100_000:.2f} USD.",
                f"OMX is configured to charge {d['configuredTenthsBps'] / 10:g} bp ({d['configuredRate']}) on every fill, "
                "on top of Hyperliquid's own exchange fee. Unfilled or cancelled orders pay nothing. Hyperliquid never "
                "charges more than the maximum you approve here.",
                "To revoke at any time: run `omx approve-builder --revoke` (signs maxFeeRate 0%). After that "
                "Hyperliquid rejects orders with this builder code."]
    return what + [
        f"Sign only with your MAIN account {acct}. The page refuses any other account.",
        "Your wallet shows a typed message 'HyperliquidTransaction:ApproveBuilderFee'. It is a signature, not a "
        "transaction: it moves no funds and costs no gas.",
        f"OMX never sees your key. The Python process that served this page receives nothing back: this page "
        f"sends the signed approval directly from your browser to {api}/exchange.",
        "This page has no field for a private key or seed phrase. Never paste one anywhere."]


def render_page(d: dict, nonce: str) -> str:
    data = json.dumps(d, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = "Revoke builder fee" if d["revoke"] else "Approve builder fee"
    net = d["network"]
    badge = f'<span class="badge{" mainnet" if net == "mainnet" else ""}">{html.escape(net.upper())}' + \
            (": real funds" if net == "mainnet" else "") + "</span>"
    words = "".join(f"<p>{html.escape(p)}</p>" for p in _plain_words(d))
    e = html.escape
    rows = (f"<dt>Network</dt><dd>{e(d['hyperliquidChain'])} ({e(d['apiUrl'])})</dd>"
            f"<dt>Main account</dt><dd>{e(d['account'])}</dd><dt>Builder</dt><dd>{e(d['builder'])}</dd>"
            f"<dt>maxFeeRate</dt><dd>{e(d['maxFeeRate'])}</dd>")
    n = e(nonce)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OMX {e(title)}</title><style nonce="{n}">{PAGE_CSS}</style></head>
<body><main>
<h1>OMX: {e(title)} {badge}</h1>
<div class="box">{words}</div>
<dl>{rows}</dl>
<button id="go" type="button">{e("Connect wallet and sign revocation" if d["revoke"] else "Connect wallet and sign approval")}</button>
<button id="check" type="button">Re-read approval</button>
<pre id="log" aria-live="polite"></pre>
<details><summary>Typed data sent to the wallet</summary><pre id="typed"></pre></details>
</main>
<script type="application/json" id="omx-data">{data}</script>
<script nonce="{n}">{PAGE_LIB_JS}{PAGE_UI_JS}</script>
</body></html>
"""


class ApprovalPage:
    """The single page and the rules for answering a request to it (pure, so it is testable offline)."""

    def __init__(self, cfg, max_fee_rate: Optional[str] = None, revoke: bool = False):
        rate, tenths, self.warnings = approval_rate(cfg, max_fee_rate, revoke)
        self.data = page_data(cfg, rate, tenths, revoke)
        self.token = secrets.token_urlsafe(16)
        self.nonce = secrets.token_urlsafe(16)
        self.html = render_page(self.data, self.nonce)
        self.port: Optional[int] = None
        self.hits = 0

    @property
    def path(self) -> str:
        return "/" + self.token

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    def headers(self) -> dict:
        csp = (f"default-src 'none'; script-src 'nonce-{self.nonce}'; style-src 'nonce-{self.nonce}'; "
               f"connect-src {self.data['apiUrl']}; img-src 'none'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'")
        return {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                "Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}

    def respond(self, method: str, path: str, host: Optional[str]) -> tuple[int, dict, bytes]:
        """GET of the random path with a loopback Host header only (no DNS rebinding); nothing else."""
        if method != "GET":
            return 405, {"Allow": "GET"}, b"method not allowed\n"
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return 403, {}, b"forbidden\n"
        if path.split("?", 1)[0] != self.path:
            return 404, {}, b"not found\n"
        self.hits += 1
        return 200, self.headers(), self.html.encode("utf-8")


def make_server(page: ApprovalPage, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A loopback-only HTTP server for the page on a random free port."""
    if host != "127.0.0.1":
        raise ApprovePageError("the approval page is served on 127.0.0.1 only")

    class Handler(BaseHTTPRequestHandler):
        server_version = "omx"
        sys_version = ""

        def _answer(self, method: str) -> None:
            status, headers, body = page.respond(method, self.path, self.headers.get("Host"))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._answer("GET")

        def do_POST(self):  # noqa: N802
            self._answer("POST")

        def do_PUT(self):  # noqa: N802
            self._answer("PUT")

        def log_message(self, *a):          # quiet: nothing about requests on the console
            pass

    srv = ThreadingHTTPServer((host, 0), Handler)
    page.port = srv.server_address[1]
    return srv


def serve(cfg, max_fee_rate: Optional[str] = None, revoke: bool = False, out: Optional[TextIO] = None,
          open_browser: bool = True, timeout_s: float = DEFAULT_TIMEOUT_S,
          opener: Callable[[str], bool] = webbrowser.open, sleep: Callable[[float], None] = time.sleep) -> int:
    """Serve the page until Ctrl+C or timeout_s. The Python side never makes a network request."""
    page = ApprovalPage(cfg, max_fee_rate, revoke)
    srv = make_server(page)
    t = threading.Thread(target=srv.serve_forever, name="omx-approve-page", daemon=True)
    t.start()
    w = out.write if out is not None else (lambda s: None)
    try:
        d = page.data
        verb = "revocation" if revoke else f"approval of max fee {d['maxFeeRate']}"
        w(f"Builder fee {verb} for builder {d['builder']} on {d['hyperliquidChain']}.\n")
        for x in page.warnings:
            w(f"WARNING: {x}\n")
        w(f"Open this page in the browser that has your MAIN wallet ({d['account']}):\n  {page.url()}\n"
          "Your wallet signs there and the page sends it to Hyperliquid itself; OMX never sees a key or the "
          f"signature. Stop this command with Ctrl+C when done (it stops by itself after {timeout_s:g} s).\n"
          "Then check with `omx doctor` (the daemon re-checks within 60 s while orders are refused).\n")
        if out is not None:
            out.flush()
        if open_browser:
            try:
                opener(page.url())
            except Exception:                 # no browser: the URL is printed above
                pass
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    w("approval page closed\n")
    return 0
