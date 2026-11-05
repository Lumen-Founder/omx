"""Onboarding: eligibility attestation, builder-fee disclosure, user checklist.

OMX never asks for, reads, or stores the MAIN wallet private key. The only key
OMX uses is an API/agent wallet key that the user creates in their own
Hyperliquid account; the config names its file path or env var, and the venue
adapter reads it at runtime. The eligibility history keeps country/region codes
and yes/no answers only; every attempt, passing or blocked, is a new line.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Optional, TextIO

from .builder import estimate_builder_fee_usd, tenths_bps_to_percent_str
from .audit import AuditLog
from .eligibility import (BLOCK_COOLDOWN_S, DEFAULT_TTL_S, LEGACY_FILE, REGION_REQUIRED_COUNTRIES, TERMS_SOURCE,
                          Attestation, check, cooldown_until, history_path, latest_blocked_before, load_history,
                          problems, record_attempt)

InputFn = Callable[[str], str]
ISO2 = re.compile(r"^[A-Z]{2}$")
REGION = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")
ELIGIBILITY_FILE = LEGACY_FILE          # pre-R1 single record, read only; history is eligibility_history.jsonl

NOT_AVAILABLE = ("OMX is not available to persons or entities residing, located, incorporated or with a "
                 "registered office in the United States, Ontario (Canada) or sanctioned territories, nor to "
                 "citizens of sanctioned territories (per the Hyperliquid Terms of Use). Using a VPN or proxy "
                 "or a false residency statement to get around this is a breach; OMX offers no way around a "
                 "block. OMX does not offer outcome markets.")


class NotEligible(RuntimeError):
    pass


# ---------------------------------------------------------------- eligibility

def _ask(input_fn: InputFn, out: TextIO, prompt: str, pattern: Optional[re.Pattern] = None,
         allow_empty: bool = False, tries: int = 3) -> str:
    for _ in range(tries):
        v = input_fn(prompt).strip().upper()
        if (allow_empty and v == "") or pattern is None or pattern.match(v):
            return v
        out.write("  not a valid code, try again\n")
    raise NotEligible("no valid answer")


def _yes(input_fn: InputFn, out: TextIO, prompt: str) -> bool:
    for _ in range(3):
        v = input_fn(prompt + " [yes/no] ").strip().lower()
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        out.write("  please answer yes or no\n")
    raise NotEligible("no valid answer")


def ask_attestation(input_fn: InputFn, out: TextIO) -> Attestation:
    out.write(NOT_AVAILABLE + "\n\n")
    is_entity = _yes(input_fn, out, "Are you using OMX on behalf of a company or other entity?")
    inc = _ask(input_fn, out, "Entity country of incorporation / registered office (ISO code, e.g. DE): ", ISO2) \
        if is_entity else ""
    country = _ask(input_fn, out, "Country where you RESIDE (live) (ISO code, e.g. KR): ", ISO2)
    # sub-national blocks only exist for CA / UA, so the region is asked only there
    # (blank is accepted and then blocked by check() as REGION_REQUIRED, so the attempt is recorded)
    region = _ask(input_fn, out, "Province/region where you reside (e.g. CA-BC, UA-30): ", REGION,
                  allow_empty=True) if country in REGION_REQUIRED_COUNTRIES else ""
    if _yes(input_fn, out, f"Are you physically in {country} right now?"):
        here, here_region = country, region
    else:
        here = _ask(input_fn, out, "Country where you are PHYSICALLY LOCATED right now (ISO code, e.g. JP): ", ISO2)
        here_region = _ask(input_fn, out, "Province/region where you are now (e.g. CA-BC, UA-30): ", REGION,
                           allow_empty=True) if here in REGION_REQUIRED_COUNTRIES else ""
    cits = _ask(input_fn, out, f"Citizenship(s), comma separated ISO codes [Enter = {country}]: ",
                re.compile(r"^[A-Z]{2}(\s*,\s*[A-Z]{2})*$"), allow_empty=True) or country
    vpn = _yes(input_fn, out, "Are you connecting through a VPN or proxy that changes your location?")
    out.write(f"Terms: {TERMS_SOURCE}\n")
    terms = _yes(input_fn, out, "Have you read and do you accept the Hyperliquid Terms of Use and the OMX terms?")
    return Attestation(residence_country=country, residence_region=region,
                       citizenships=tuple(c.strip() for c in cits.split(",")), entity_incorporation=inc,
                       is_entity=is_entity, uses_vpn_or_proxy=vpn, accepts_terms=terms,
                       current_country=here, current_region=here_region)


def save_eligibility(state_dir: str | Path, att: Attestation, ok: bool, reasons: list[str],
                     clock: Callable[[], float] = time.time, audit: Optional[AuditLog] = None,
                     ttl_s: int = DEFAULT_TTL_S) -> Path:
    """Append one attempt to the history (never overwrites). Returns the history path."""
    record_attempt(state_dir, att, ok, reasons, now_s=clock(), ttl_s=ttl_s, audit=audit)
    return history_path(state_dir)


_LEGACY_KEYS = ("eligible", "reasons", "terms_source", "ts_s", "residence_country", "residence_region",
                "citizenships", "is_entity", "entity_incorporation", "uses_vpn_or_proxy", "accepts_terms")


def load_eligibility(state_dir: str | Path) -> Optional[dict]:
    """The newest attempt in the pre-R1 shape. load_history() has every attempt and all fields."""
    hist = load_history(state_dir)
    return {k: hist[-1][k] for k in _LEGACY_KEYS if k in hist[-1]} if hist else None


def eligibility_problem(state_dir: str | Path, now_s: Optional[float] = None) -> Optional[str]:
    """None when a current, passing attestation is on file; otherwise the first reason code
    (ELIGIBILITY_NOT_CONFIRMED / _BLOCKED / _EXPIRED, TERMS_CHANGED_REATTEST, REATTEST_REQUIRED, ...)."""
    why = problems(load_history(state_dir), time.time() if now_s is None else now_s)
    return why[0] if why else None


def run_eligibility(state_dir: str | Path, input_fn: InputFn, out: TextIO, audit: Optional[AuditLog] = None,
                    clock: Callable[[], float] = time.time, ttl_s: int = DEFAULT_TTL_S) -> tuple[bool, list[str]]:
    att = ask_attestation(input_fn, out)
    ok, reasons = check(att)
    now = clock()
    rec = record_attempt(state_dir, att, ok, reasons, now_s=now, ttl_s=ttl_s, audit=audit)
    if not rec["eligible"]:
        out.write("OMX cannot be used: " + ", ".join(rec["reasons"]) + "\n" + NOT_AVAILABLE + "\n")
        out.write(f"This blocked attempt is kept on record. A new passing answer counts only if it is given "
                  f"after {_utc(rec['ts_s'] + BLOCK_COOLDOWN_S)} ({BLOCK_COOLDOWN_S // 3600} h cooldown).\n")
        return False, rec["reasons"]
    hist = load_history(state_dir)
    prev = latest_blocked_before(hist)
    if prev is not None:
        out.write(f"Note: an earlier attempt at {_utc(prev.get('ts_s', 0))} was blocked "
                  f"({', '.join(prev.get('reasons', []))}). It stays on record; this answer is recorded "
                  "as a separate entry. A false statement is a breach of the Terms.\n")
    why = problems(hist, now)
    if why:
        until = cooldown_until(hist)
        if "ELIGIBILITY_COOLDOWN" in why and until is not None:
            out.write(f"OMX stays locked: this answer came within {BLOCK_COOLDOWN_S // 3600} h of a blocked "
                      f"attempt, so it does not count. Answer again after {_utc(until)}.\n")
        else:
            out.write("OMX cannot be used yet: " + ", ".join(why) + "\n")
        return False, why
    out.write(f"Eligibility confirmed until {_utc(rec['expires_s'])}; you will be asked again after that.\n")
    return True, rec["reasons"]


def _utc(ts_s: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts_s))


# ---------------------------------------------------------------- fees

def max_fee_rate_str(tenths_bps: int) -> str:
    """Builder fee as the percent string Hyperliquid shows when you approve it: 10 -> '0.01%'."""
    return tenths_bps_to_percent_str(tenths_bps)


def builder_fee_usd(notional_usd: float, tenths_bps: int) -> float:
    return estimate_builder_fee_usd(notional_usd, tenths_bps)


def fee_disclosure(cfg) -> str:
    f = cfg.builder_fee_tenths_bps
    if f == 0 or not cfg.builder_address:
        return ("Builder fee: none. This config sends no builder code, so OMX charges nothing on top of "
                "Hyperliquid's own trading fees. You do not need to approve any builder fee.")
    return "\n".join([
        f"Builder fee: {f / 10:g} bps ({max_fee_rate_str(f)}) of the notional of every fill OMX sends.",
        "It is charged by Hyperliquid on each fill, on top of Hyperliquid's own exchange fee, "
        "and paid to this builder address:",
        f"  {cfg.builder_address}",
        f"Example: a fill of 10,000 USD notional pays {builder_fee_usd(10_000, f):.2f} USD builder fee.",
        f"The max fee rate you would approve from your MAIN wallet is exactly: {max_fee_rate_str(f)}",
        "OMX cannot charge more than the rate you approve. You can revoke the approval at any time "
        "from your main wallet; after that OMX orders carrying this builder code are rejected by "
        "Hyperliquid.",
    ])


# ---------------------------------------------------------------- checklist

def checklist(cfg) -> str:
    net = "app.hyperliquid-testnet.xyz" if cfg.network == "testnet" else "app.hyperliquid.xyz"
    where = f"file {cfg.signer_key_path}" if cfg.signer_key_path else f"environment variable {cfg.signer_key_env}"
    steps = [
        "OMX never asks for your MAIN wallet private key or seed phrase. Never type or paste it into OMX, "
        "its config, a terminal command, or an AI agent.",
        f"1. In your own Hyperliquid account ({net}), create an API / agent wallet (API page). "
        "An agent wallet can trade but cannot withdraw.",
        "2. Save the agent wallet key in a file only your user account can read (or an environment "
        "variable), outside any synced or shared folder.",
        f"3. Put only the PATH or variable NAME into the config. This config points to: {where}.",
        f"4. Check account_address is your MAIN account's public address: {cfg.account_address}.",
    ]
    if cfg.builder_fee_tenths_bps > 0 and cfg.builder_address:
        steps.append(f"5. From your MAIN wallet, approve the builder fee for {cfg.builder_address} with max "
                     f"rate {max_fee_rate_str(cfg.builder_fee_tenths_bps)}. You sign this yourself in "
                     "your wallet; OMX does not do it for you: `omx approve-builder` opens a local page "
                     "where your browser wallet signs it. Until then OMX refuses new orders "
                     "(BUILDER_NOT_APPROVED). `omx doctor` checks the whole setup.")
    else:
        steps.append("5. No builder fee is configured, so there is nothing to approve.")
    steps.append("6. Start on testnet. Run `python -m omx.cli preview ...` before any `submit`.")
    return "\n".join(steps)


def run_onboarding(cfg, input_fn: InputFn, out: TextIO) -> bool:
    ok, _ = run_eligibility(cfg.state_dir, input_fn, out)
    if not ok:
        return False
    out.write("\n" + fee_disclosure(cfg) + "\n\n" + checklist(cfg) + "\n")
    return True
