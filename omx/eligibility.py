"""Access eligibility, mirroring Hyperliquid's Terms of Use (app.hyperliquid.xyz/terms,
last updated 2026-06-15), sections 1.6-1.9 and 3.1.5.

Restricted Persons there: persons or entities residing, located, incorporated or with a
registered office in the United States or Ontario (Canada) or in sanctioned territories,
and citizens of sanctioned territories wherever they are. Outcome markets are excluded
for further designated persons; this product does not offer outcome markets at all.

Residence and current physical location are separate facts and are both checked. For
countries whose block is sub-national (CA: Ontario; UA: occupied regions) a region code
is required, otherwise the attestation is blocked.

Where readings could differ we take the narrower, safer one: the sanctioned list below is
broader than the comprehensive-embargo minimum. The operator may only ADD entries.
Circumvention (VPN/proxy, false residency statements) is a breach; the software never
offers ways around a block.

Attestations are kept as an append-only history (`state_dir/eligibility_history.jsonl`).
A blocked attempt is never overwritten: a later passing attempt is a new entry, and
require_valid() accepts only an unexpired pass that is newer than every blocked attempt and
was given at least BLOCK_COOLDOWN_S (24 h) after the latest one. Schema 2 records must state
the current physical location; save paths store an attempt without it as blocked.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .audit import file_lock

TERMS_SOURCE = "https://app.hyperliquid.xyz/terms (Last updated on June 15, 2026)"

# ISO 3166-1 alpha-2, plus region codes for sub-national blocks.
RESTRICTED_COUNTRIES = frozenset({"US"})
RESTRICTED_REGIONS = frozenset({"CA-ON"})
SANCTIONED_TERRITORIES = frozenset({
    "CU", "IR", "KP", "SY",            # comprehensive embargoes
    "RU", "BY",                        # broad sanctions programmes (conservative choice)
    "UA-43", "UA-14", "UA-09",         # Crimea, Donetsk, Luhansk regions
    "UA-40", "UA-23", "UA-65",         # Sevastopol, Zaporizhzhia, Kherson regions
    "VE", "MM", "AF", "SD", "SS", "LY", "SO", "YE", "CF", "CD", "ML", "NI", "ZW", "IQ", "LB",
})
# Countries where the block depends on the region, so the region must be stated.
REGION_REQUIRED_COUNTRIES = frozenset({"CA", "UA"})
EXCLUDED_PRODUCTS = frozenset({"outcome"})

DEFAULT_TTL_S = 30 * 86400             # an attestation must be renewed after 30 days
MAX_FUTURE_SKEW_S = 600                # a record dated after `now` by more than this is refused
BLOCK_COOLDOWN_S = 86400               # a pass made within this long after a blocked attempt never counts
HISTORY_FILE = "eligibility_history.jsonl"
LEGACY_FILE = "eligibility.json"       # pre-R1 single record; read only, never written
SCHEMA = 2                             # 2 = residence and current location asked separately


class EligibilityError(RuntimeError):
    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("not eligible: " + ", ".join(self.reasons))


@dataclass(frozen=True)
class Attestation:
    residence_country: str            # ISO alpha-2
    residence_region: str = ""        # e.g. "CA-ON", "UA-43"
    citizenships: tuple[str, ...] = ()
    entity_incorporation: str = ""    # for entities: country of incorporation / registered office
    is_entity: bool = False
    uses_vpn_or_proxy: bool = False
    accepts_terms: bool = False
    current_country: str = ""         # where the person is physically now; "" = not stated
    current_region: str = ""


def check(att: Attestation, observed_country: str = "", observed_region: str = "",
          extra_blocked: frozenset = frozenset()) -> tuple[bool, list[str]]:
    """observed_* come from the network edge (e.g. a CDN country header) when available."""
    blocked = RESTRICTED_COUNTRIES | SANCTIONED_TERRITORIES | set(extra_blocked)
    reasons: list[str] = []
    if not att.accepts_terms:
        reasons.append("TERMS_NOT_ACCEPTED")
    if att.uses_vpn_or_proxy:
        reasons.append("VPN_OR_PROXY_DECLARED")
    res, cur = att.residence_country.upper(), att.current_country.upper()
    places = {res, cur} - {""}
    if att.is_entity and att.entity_incorporation:
        places.add(att.entity_incorporation.upper())
    if observed_country:
        places.add(observed_country.upper())
    for p in sorted(places):
        if p in blocked:
            reasons.append(f"RESTRICTED_LOCATION:{p}")
    for what, country, region in (("RESIDENCE", res, att.residence_region.upper()),
                                  ("LOCATION", cur, att.current_region.upper())):
        if country in REGION_REQUIRED_COUNTRIES and not region:
            reasons.append(f"REGION_REQUIRED:{what}:{country}")
        if region and country and not region.startswith(country + "-"):
            reasons.append(f"REGION_COUNTRY_MISMATCH:{what}:{region}")
    regions = {att.residence_region.upper(), att.current_region.upper(), observed_region.upper()} - {""}
    for r in sorted(regions):
        if r in RESTRICTED_REGIONS or r in SANCTIONED_TERRITORIES or r in extra_blocked:
            reasons.append(f"RESTRICTED_REGION:{r}")
    for c in att.citizenships:
        if c.upper() in SANCTIONED_TERRITORIES or c.upper() in extra_blocked:
            reasons.append(f"SANCTIONED_CITIZENSHIP:{c.upper()}")
    here = cur or res                  # the observed edge country is compared with the stated location
    if observed_country and here and observed_country.upper() != here:
        reasons.append("LOCATION_MISMATCH")
    return (not reasons), reasons


# ---------------------------------------------------------------- history

_ATT_FIELDS = ("residence_country", "residence_region", "current_country", "current_region", "citizenships",
               "is_entity", "entity_incorporation", "uses_vpn_or_proxy", "accepts_terms")


def history_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / HISTORY_FILE


def load_history(state_dir: str | Path) -> list[dict]:
    """All attempts, oldest first. Falls back to the legacy single record (schema 1)."""
    hp = history_path(state_dir)
    if hp.exists():
        out = []
        for line in hp.read_bytes().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:   # torn partial line from a crash: unusable, never a pass
                    out.append({"schema": SCHEMA, "eligible": False, "corrupt": True,
                                "reasons": ["HISTORY_RECORD_CORRUPT"]})
        return out
    lp = Path(state_dir) / LEGACY_FILE
    if lp.exists():
        rec = json.loads(lp.read_text(encoding="utf-8"))
        rec.setdefault("schema", 1)
        return [rec]
    return []


def attestation_of(rec: dict) -> Attestation:
    return Attestation(**{k: (tuple(rec[k]) if k == "citizenships" else rec[k]) for k in _ATT_FIELDS if k in rec})


def record_attempt(state_dir: str | Path, att: Attestation, ok: bool, reasons: list[str],
                   now_s: float, ttl_s: int = DEFAULT_TTL_S, audit=None) -> dict:
    """Append one attempt. The rule check is re-run here, so a caller cannot store a
    pass for an attestation the rules block. Every attempt is audited when `audit` is given.
    Schema 2 requires the current physical location; without it the attempt is stored blocked."""
    passed, own = check(att)
    if not att.current_country.strip():
        own.append("CURRENT_LOCATION_REQUIRED")
    reasons = list(dict.fromkeys(list(reasons) + own))
    ok = bool(ok) and passed and not reasons
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    hp = history_path(d)
    with file_lock(hp):
        prior = load_history(d)
        lines = []
        if not hp.exists() and prior:        # carry a legacy record into the history first
            lines.append(dict(prior[0], imported_from=LEGACY_FILE))
        ts = int(now_s)
        rec = {"schema": SCHEMA, "seq": len(prior), "ts_s": ts, "expires_s": ts + int(ttl_s), "eligible": ok,
               "reasons": reasons, "terms_source": TERMS_SOURCE,
               **{k: (list(getattr(att, k)) if k == "citizenships" else getattr(att, k)) for k in _ATT_FIELDS}}
        lines.append(rec)
        with hp.open("a+b") as fh:
            end = fh.seek(0, os.SEEK_END)
            if end:
                fh.seek(end - 1)
                if fh.read(1) != b"\n":       # torn line from a crash stays as a corrupt (blocked) entry
                    fh.write(b"\n")
            for r in lines:
                fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
    if audit is not None:
        audit.append("eligibility_attempt", {k: rec[k] for k in rec if k != "terms_source"}, durable=True)
    return rec


def problems(history: list[dict], now_s: float, max_age_s: Optional[float] = None) -> list[str]:
    """[] when the newest attempt is an unexpired pass under the current rules; else reasons."""
    if not history:
        return ["ELIGIBILITY_NOT_CONFIRMED"]
    latest = history[-1]                 # file order: a pass must be newer than every blocked attempt
    if not latest.get("eligible"):
        return ["ELIGIBILITY_BLOCKED"] + list(latest.get("reasons", []))
    out = []
    if latest.get("schema", 1) < SCHEMA:
        out.append("REATTEST_REQUIRED")      # old record: current location was not asked separately
    elif not str(latest.get("current_country", "")).strip():
        out.append("CURRENT_LOCATION_REQUIRED")
    if latest.get("terms_source") != TERMS_SOURCE:
        out.append("TERMS_CHANGED_REATTEST")
    ts = float(latest.get("ts_s", 0))
    until = cooldown_until(history)
    if until is not None and ts < until:
        out.append("ELIGIBILITY_COOLDOWN")   # answered again too soon after a block: re-attest after `until`
    exp = float(latest.get("expires_s", ts + DEFAULT_TTL_S))
    if max_age_s is not None:
        exp = min(exp, ts + max_age_s)
    if now_s >= exp:
        out.append("ELIGIBILITY_EXPIRED")
    if ts > now_s + MAX_FUTURE_SKEW_S:
        out.append("ATTESTATION_IN_FUTURE")
    try:
        ok, why = check(attestation_of(latest))
    except TypeError:
        ok, why = False, ["RECORD_UNREADABLE"]
    if not ok:
        out += ["RULES_CHANGED_REATTEST"] + why
    return out


def require_valid(state_dir: str | Path, now_s: float, max_age_s: Optional[float] = None) -> None:
    """Raise EligibilityError unless the newest attestation in `state_dir` is an unexpired
    pass. `now_s` is wall-clock unix seconds (the same clock the attestation was saved with)."""
    why = problems(load_history(state_dir), now_s, max_age_s)
    if why:
        raise EligibilityError(why)


def cooldown_until(history: list[dict]) -> Optional[float]:
    """End of the cooldown set by blocked attempts before the newest one (latest block time,
    max over records so a rolled-back clock cannot shorten it), or None. Corrupt crash lines
    carry no time and set no cooldown; they still never count as a pass."""
    ts = [float(r["ts_s"]) for r in history[:-1] if not r.get("eligible") and not r.get("corrupt") and "ts_s" in r]
    return max(ts) + BLOCK_COOLDOWN_S if ts else None


def latest_blocked_before(history: list[dict]) -> Optional[dict]:
    """The most recent blocked attempt that a newer pass follows, if any (for notices)."""
    for rec in reversed(history[:-1]):
        if not rec.get("eligible"):
            return rec
    return None

