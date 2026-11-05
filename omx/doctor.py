"""`omx doctor`: read-only setup checks, printed as OK / FAIL (with the fix) / SKIP.

Checks, in order: the config loads; the agent key file exists (or the env var is set); the agent
address derived from it differs from the account and the builder (the key itself is never printed);
the network endpoint; /info reads: the account exists (userRole), its account mode (userAbstraction:
standard and unified are supported, unified shown with its equity basis), its account value (standard:
perp clearinghouseState; unified: spot USDC balance and positions, see omx.account_mode), the agent is approved for the account (extraAgents, else userRole of the agent),
the builder approval (maxBuilderFee) covers the configured fee, the builder's own perp account value is
at least 100 USDC (Hyperliquid's rule for collecting builder fees); the eligibility attestation is
valid; the daemon is running.

Nothing is written and nothing is signed. The only network access is these /info reads, made when a
user runs the command (tests inject a fake transport).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from . import account_mode as AM
from . import ipc
from .builder import builder_of, parse_max_builder_fee, tenths_bps_to_percent_str
from .config import ConfigError, OMXConfig, eligibility_gate, load

BUILDER_MIN_ACCOUNT_VALUE_USD = 100.0
# userAbstraction values with separate perp/spot balances. "disabled" = Manual / Standard;
# "default" is what testnet reports for an untouched account whose perp and spot balances move
# separately; a builder in "default" accrued builderRewards on testnet fills (2026-11-05 E2E).
STANDARD_MODES = AM.STANDARD_MODES
OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _app(cfg: OMXConfig) -> str:
    return "app.hyperliquid-testnet.xyz" if cfg.network == "testnet" else "app.hyperliquid.xyz"


def _account_value(info: Callable[[dict], Any], user: str) -> float:
    cs = info({"type": "clearinghouseState", "user": user})
    return float(cs["marginSummary"]["accountValue"])


def default_derive(cfg: OMXConfig) -> str:
    """Agent address from the configured key (read in this process only; the key is not kept or shown)."""
    from .venue.hyperliquid import LocalKeySigner
    signer = LocalKeySigner.from_key_file(cfg.signer_key_path) if cfg.signer_key_path \
        else LocalKeySigner.from_env(cfg.signer_key_env)
    return signer.address


def checks(cfg_path: Path, transport_factory: Optional[Callable[[str], Callable]] = None,
           derive: Callable[[OMXConfig], str] = default_derive, now_s: Optional[float] = None) -> list[Check]:
    now_s = time.time() if now_s is None else now_s
    out: list[Check] = []
    try:
        cfg = load(cfg_path)
    except ConfigError as e:
        return [Check("config loads", FAIL, str(e), f"fix the config at {cfg_path} (`omx init` writes one)")]
    out.append(Check("config loads", OK, f"{cfg_path} ({cfg.network}, account {cfg.account_address})"))

    # key reference and the agent address it gives
    agent: Optional[str] = None
    if cfg.signer_key_path:
        kp = Path(cfg.signer_key_path)
        if kp.is_file():
            out.append(Check("agent key file", OK, f"exists: {kp}"))
        else:
            out.append(Check("agent key file", FAIL, f"not found: {kp}",
                             f"save the API wallet key created on {_app(cfg)} (API page) in that file"))
    elif os.environ.get(cfg.signer_key_env or ""):
        out.append(Check("agent key env", OK, f"{cfg.signer_key_env} is set"))
    else:
        out.append(Check("agent key env", FAIL, f"{cfg.signer_key_env} is empty or unset",
                         "set it in the shell that runs `omx daemon`"))
    if out[-1].status == OK:
        try:
            agent = derive(cfg).lower()
        except Exception as e:  # the message never contains the key (LocalKeySigner hides it)
            out.append(Check("agent address", FAIL, f"could not derive an address: {type(e).__name__}",
                             "the file / variable must hold one hex private key of an API wallet"))
        else:
            if agent == cfg.account_address.lower():
                out.append(Check("agent address", FAIL, f"{agent} is the account itself (the MAIN wallet key)",
                                 f"create an API wallet on {_app(cfg)} (API page) and use ITS key; never the main key"))
            elif cfg.builder_address and agent == cfg.builder_address.lower():
                out.append(Check("agent address", FAIL, f"{agent} is the builder address",
                                 "the agent, the account and the builder must be three different addresses"))
            else:
                out.append(Check("agent address", OK, f"{agent} (differs from the account"
                                 + (" and the builder)" if cfg.builder_address else ")")))

    from .venue.hyperliquid import MAINNET_API_URL, TESTNET_API_URL, RequestsTransport
    url = MAINNET_API_URL if cfg.network == "mainnet" else TESTNET_API_URL
    out.append(Check("network endpoint", OK, f"{cfg.network}: {url}"
                     + ("  (MAINNET: real funds)" if cfg.network == "mainnet" else "")))
    transport = (transport_factory or RequestsTransport)(url)
    info = lambda payload: transport("/info", payload)      # noqa: E731
    acct = cfg.account_address.lower()

    def guarded(name: str, fn: Callable[[], Check]) -> None:
        try:
            out.append(fn())
        except Exception as e:  # one failed read does not stop the others
            out.append(Check(name, FAIL, f"read failed: {type(e).__name__}: {str(e)[:200]}",
                             f"check the network connection to {url}, then run `omx doctor` again"))

    def account_exists() -> Check:
        role = (info({"type": "userRole", "user": acct}) or {}).get("role")
        if role == "missing":
            return Check("account exists", FAIL, f"{acct} has no Hyperliquid {cfg.network} account",
                         f"deposit USDC to it on {_app(cfg)} (testnet: use the faucet)")
        if role == "agent":
            return Check("account exists", FAIL, f"{acct} is an API (agent) wallet, not a main account",
                         "account_address must be your MAIN account's public address")
        return Check("account exists", OK, f"role {role}")

    def account_value() -> Check:
        if mode_seen.get("mode") in AM.UNIFIED_MODES:
            u = unified()
            flag = f" ({AM.EQUITY_BASIS_UNVERIFIED})" if AM.EQUITY_BASIS_UNVERIFIED in u["flags"] else ""
            if u["equity_usd"] > 0:
                return Check("account value", OK, f"unified equity {u['equity_usd']:,.2f} USDC{flag}")
            return Check("account value", FAIL, f"unified equity is {u['equity_usd']:,.2f} USDC{flag}",
                         f"deposit USDC on {_app(cfg)} (testnet: use the faucet)")
        v = _account_value(info, acct)
        if v > 0:
            return Check("account value", OK, f"perp account value {v:,.2f} USDC")
        return Check("account value", FAIL, "perp account value is 0",
                     f"deposit USDC (or move it from spot to perps) on {_app(cfg)}")

    def agent_approved() -> Check:
        if agent is None:
            return Check("agent approved", SKIP, "no agent address (see above)")
        rows = info({"type": "extraAgents", "user": acct}) or []
        for r in rows:
            if str(r.get("address", "")).lower() == agent:
                until = int(r.get("validUntil") or 0)
                if until and until / 1000 < now_s:
                    return Check("agent approved", FAIL, f"API wallet {r.get('name')!r} expired at "
                                 f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(until / 1000))}",
                                 f"create or renew an API wallet on {_app(cfg)} (API page)")
                when = (" until " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(until / 1000))) if until else ""
                return Check("agent approved", OK, f"API wallet {r.get('name')!r}{when}")
        role = info({"type": "userRole", "user": agent}) or {}
        if role.get("role") == "agent" and str((role.get("data") or {}).get("user", "")).lower() == acct:
            return Check("agent approved", OK, "agent of this account (userRole)")
        return Check("agent approved", FAIL, f"{agent} is not an approved API wallet of {acct}",
                     f"approve it on {_app(cfg)} (API page) from your MAIN wallet")

    mode_seen: dict = {}

    def unified() -> dict:
        return AM.unified_equity(info({"type": "spotClearinghouseState", "user": acct}),
                                 info({"type": "clearinghouseState", "user": acct}))

    def account_mode() -> Check:
        mode = info({"type": "userAbstraction", "user": acct})
        mode_seen["mode"] = mode
        if mode in STANDARD_MODES:
            return Check("account mode", OK, f"{mode} (standard: separate perp and spot balances; "
                         f"equity = perp account value)")
        if mode in AM.UNIFIED_MODES:
            u = unified()
            c = u["candidates"]
            txt = (f"{mode} (one USDC balance for spot and perps; equity basis {u['basis']} = "
                   f"{u['equity_usd']:,.2f} USDC: spot USDC total {c[AM.BASIS_SPOT_TOTAL]:,.2f}, "
                   f"total + unrealized P&L {c[AM.BASIS_SPOT_PLUS_UPNL]:,.2f})")
            if AM.EQUITY_BASIS_UNVERIFIED in u["flags"]:
                txt += (f"; {AM.EQUITY_BASIS_UNVERIFIED}: the lower candidate is used until "
                        "tools/measure_account_mode.py settles the basis")
            return Check("account mode", OK, txt)
        why = ("portfolio margin" if mode == "portfolioMargin" else
               "DEX abstraction (discontinued)" if mode == "dexAbstraction" else f"unknown mode {mode!r}")
        return Check("account mode", FAIL, f"{mode}: {why} is not supported by OMX v1; new orders are refused "
                     f"({AM.ACCOUNT_MODE_UNSUPPORTED})",
                     f"switch the account type to Unified or Manual / Standard on {_app(cfg)} (portfolio: Account type)")

    for name, fn in (("account exists", account_exists), ("account mode", account_mode),
                     ("account value", account_value), ("agent approved", agent_approved)):
        guarded(name, fn)

    b = builder_of(cfg.builder_address, cfg.builder_fee_tenths_bps)
    if b is None:
        out.append(Check("builder approval", SKIP, "no builder fee configured"))
        out.append(Check("builder account value", SKIP, "no builder fee configured"))
    else:
        def approval() -> Check:
            got = parse_max_builder_fee(info({"type": "maxBuilderFee", "user": acct, "builder": b[0]}))
            txt = f"approved {got} ({tenths_bps_to_percent_str(got)}), configured {b[1]} ({tenths_bps_to_percent_str(b[1])})"
            if got >= b[1]:
                return Check("builder approval", OK, txt)
            return Check("builder approval", FAIL, txt + ": OMX refuses new orders (BUILDER_NOT_APPROVED)",
                         "run `omx approve-builder` and sign with your MAIN wallet in the browser")

        def builder_value() -> Check:
            v = _account_value(info, b[0])
            if v >= BUILDER_MIN_ACCOUNT_VALUE_USD:
                return Check("builder account value", OK, f"{b[0]}: {v:,.2f} USDC")
            return Check("builder account value", FAIL,
                         f"{b[0]}: {v:,.2f} USDC < {BUILDER_MIN_ACCOUNT_VALUE_USD:g}",
                         "Hyperliquid requires a builder to hold at least 100 USDC of perp account value; "
                         "fund the builder address")
        def builder_mode() -> Check:
            # docs (Account abstraction modes): builder addresses must be in standard mode to accrue fees
            mode = info({"type": "userAbstraction", "user": b[0]})
            if mode in STANDARD_MODES:
                return Check("builder account mode", OK, f"{b[0]}: {mode}")
            return Check("builder account mode", FAIL, f"{b[0]}: {mode}; builder fees accrue only in standard mode",
                         "switch the builder account type to Manual / Standard")
        guarded("builder approval", approval)
        guarded("builder account value", builder_value)
        guarded("builder account mode", builder_mode)

    prob = eligibility_gate(cfg, now_s)
    out.append(Check("eligibility", OK, "valid attestation on file") if prob is None else
               Check("eligibility", FAIL, prob, "run `omx eligibility`"))

    state, detail = ipc.daemon_state(cfg.state_dir)
    if state == "not_running":
        out.append(Check("daemon running", FAIL, detail, "start `omx daemon` in its own terminal"))
    else:
        hb = ipc.read_heartbeat(cfg.state_dir) or {}
        extra = "; BUILDER_NOT_APPROVED (new intents refused)" if hb.get("builder_not_approved") else ""
        out.append(Check("daemon running", OK, f"{state}: {detail}{extra}"))
    return out


def run_doctor(cfg_path: Path, out: TextIO, transport_factory: Optional[Callable[[str], Callable]] = None,
               derive: Callable[[OMXConfig], str] = default_derive, now_s: Optional[float] = None) -> int:
    """Print the checks; exit code 0 when nothing FAILed, else 1."""
    return report(checks(cfg_path, transport_factory, derive, now_s), out)


def report(res: list[Check], out: TextIO) -> int:
    """Print checks as `omx doctor` does; 0 when nothing FAILed, else 1 (also used by `omx setup`)."""
    for c in res:
        out.write(f"{c.status:<4}  {c.name}: {c.detail}\n")
        if c.status == FAIL and c.fix:
            out.write(f"      fix: {c.fix}\n")
    bad = sum(c.status == FAIL for c in res)
    out.write(f"\n{len(res)} checks, {bad} failed\n" if bad else f"\n{len(res)} checks, all OK\n")
    return 1 if bad else 0
