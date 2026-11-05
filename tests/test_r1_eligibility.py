"""Fix round R1, contract 10: regions, separate location, expiry, append-only history (ports F9, F9b)."""
import io
import json

import pytest

from omx import eligibility as el
from omx.audit import AuditLog
from omx.eligibility import (DEFAULT_TTL_S, EligibilityError, Attestation, check, load_history, problems,
                             require_valid)
from omx.onboarding import (NOT_AVAILABLE, eligibility_problem, load_eligibility, run_eligibility,
                            save_eligibility)

T0 = 1_800_000_000.0
DAY = 86400


from answer_flow import flow


def answers(res="KR", res_region="", here=None, here_region=None, cits=None, vpn="no", terms="yes"):
    """Order: entity?, residence country, residence region, current country, current region,
    citizenships, VPN?, terms? Current location defaults to the residence answers."""
    if here_region is None:
        here_region = res_region if here is None else ""
    return flow(["no", res, res_region, here or res, here_region, cits or res, vpn, terms])


class Inputs:
    def __init__(self, a):
        self.a, self.prompts = list(a), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.a.pop(0)


def attempt(state, a, t=T0, audit=None, **kw):
    out = io.StringIO()
    inp = Inputs(a)
    ok, reasons = run_eligibility(state, inp, out, audit=audit, clock=lambda: t, **kw)
    assert inp.a == [], "not every answer was consumed"
    return ok, reasons, out.getvalue(), inp.prompts


# ---------------------------------------------------------------- regions and location

def test_canada_without_province_is_blocked(tmp_path):           # F9
    ok, reasons, _, _ = attempt(tmp_path, answers("CA"))
    assert not ok and "REGION_REQUIRED:RESIDENCE:CA" in reasons
    assert eligibility_problem(tmp_path, T0) == "ELIGIBILITY_BLOCKED"


@pytest.mark.parametrize("kw,ok,reason", [
    (dict(res="CA", res_region="CA-BC"), True, None),
    (dict(res="CA", res_region="CA-ON"), False, "RESTRICTED_REGION:CA-ON"),
    (dict(res="UA"), False, "REGION_REQUIRED:RESIDENCE:UA"),
    (dict(res="UA", res_region="UA-30"), True, None),
    (dict(res="UA", res_region="UA-43"), False, "RESTRICTED_REGION:UA-43"),
    (dict(res="KR", here="CA", cits="KR"), False, "REGION_REQUIRED:LOCATION:CA"),
    (dict(res="KR", here="CA", here_region="CA-ON", cits="KR"), False, "RESTRICTED_REGION:CA-ON"),
    (dict(res="CA", res_region="CA-BC", here="CA", here_region="CA-ON"), False, "RESTRICTED_REGION:CA-ON"),
    (dict(res="KR", here="US", cits="KR"), False, "RESTRICTED_LOCATION:US"),
    (dict(res="KR", here="JP", cits="KR"), True, None),
])
def test_region_and_current_location_rules(tmp_path, kw, ok, reason):
    got, reasons, _, _ = attempt(tmp_path, answers(**kw))
    assert got is ok, reasons
    if reason:
        assert reason in reasons


def test_residence_and_location_are_asked_separately(tmp_path):
    _, _, out, prompts = attempt(tmp_path, answers())
    assert any("RESIDE" in p for p in prompts) and any("physically in KR right now" in p for p in prompts)
    assert not any("Province/region" in p for p in prompts)       # asked only for CA / UA
    _, _, _, prompts = attempt(tmp_path / "jp", answers(here="JP", cits="KR"))
    assert any("PHYSICALLY LOCATED" in p for p in prompts)
    assert any("VPN or proxy" in p for p in prompts)
    assert NOT_AVAILABLE in out and "offers no way around" in out and "VPN or proxy" in out


def test_observed_country_is_compared_with_current_location():
    att = Attestation("KR", citizenships=("KR",), accepts_terms=True, current_country="JP")
    assert check(att, observed_country="JP")[0]
    ok, why = check(att, observed_country="KR")
    assert not ok and "LOCATION_MISMATCH" in why


def test_check_and_attestation_backward_compatible():
    assert check(Attestation("KR", accepts_terms=True), observed_country="KR") == (True, [])
    assert check(Attestation("CA", "CA-BC", accepts_terms=True), observed_country="CA")[0]
    assert not check(Attestation("CA", accepts_terms=True))[0]         # province now required


# ---------------------------------------------------------------- history

def test_blocked_attempt_is_kept_and_audited(tmp_path):          # F9b
    audit = AuditLog(tmp_path / "audit.jsonl")
    ok, _, _, _ = attempt(tmp_path, answers("US"), audit=audit)
    assert not ok and eligibility_problem(tmp_path, T0) == "ELIGIBILITY_BLOCKED"
    ok, reasons, out, _ = attempt(tmp_path, answers("KR"), t=T0 + 60, audit=audit)
    assert not ok and reasons[0] == "ELIGIBILITY_COOLDOWN"            # re-answer within 24 h does not count
    assert "was blocked" in out and "RESTRICTED_LOCATION:US" in out and "stays locked" in out
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0 + 2 * DAY)                          # never, not just until the cooldown ends
    assert e.value.reasons == ["ELIGIBILITY_COOLDOWN"]
    ok, _, out, _ = attempt(tmp_path, answers("KR"), t=T0 + DAY, audit=audit)
    assert ok and "was blocked" in out
    hist = load_history(tmp_path)
    assert [h["eligible"] for h in hist] == [False, True, True] and [h["seq"] for h in hist] == [0, 1, 2]
    assert hist[0]["residence_country"] == "US" and "RESTRICTED_LOCATION:US" in hist[0]["reasons"]
    recs = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_bytes().splitlines()]
    att = [r["data"] for r in recs if r["kind"] == "eligibility_attempt"]
    assert [a["eligible"] for a in att] == [False, True, True]
    assert AuditLog.verify(tmp_path / "audit.jsonl")[0]
    require_valid(tmp_path, T0 + DAY + 60)                             # a pass after the cooldown counts


def test_cooldown_boundary_and_constant(tmp_path, monkeypatch):
    assert el.BLOCK_COOLDOWN_S == 86400
    _, _, out, _ = attempt(tmp_path, answers("US"))
    assert "24 h cooldown" in out
    attempt(tmp_path, answers("KR"), t=T0 + DAY - 1)
    assert eligibility_problem(tmp_path, T0 + DAY) == "ELIGIBILITY_COOLDOWN"
    attempt(tmp_path, answers("KR"), t=T0 + DAY)
    require_valid(tmp_path, T0 + DAY + 1)
    monkeypatch.setattr(el, "BLOCK_COOLDOWN_S", 2 * DAY)              # configurable
    assert eligibility_problem(tmp_path, T0 + DAY + 1) == "ELIGIBILITY_COOLDOWN"


def test_cooldown_counts_from_latest_block_even_if_clock_rolls_back(tmp_path):
    attempt(tmp_path, answers("US"), t=T0 + 5 * DAY)
    attempt(tmp_path, answers("KR"), t=T0 + 2 * DAY)                  # dated before the block
    assert eligibility_problem(tmp_path, T0 + 10 * DAY) == "ELIGIBILITY_COOLDOWN"


def test_cooldown_restarts_on_each_block(tmp_path):
    attempt(tmp_path, answers("US"))
    attempt(tmp_path, answers("KR", vpn="yes"), t=T0 + DAY - 10)
    attempt(tmp_path, answers("KR"), t=T0 + DAY + 10)
    assert eligibility_problem(tmp_path, T0 + DAY + 20) == "ELIGIBILITY_COOLDOWN"
    attempt(tmp_path, answers("KR"), t=T0 + 2 * DAY)
    assert eligibility_problem(tmp_path, T0 + 2 * DAY + 1) is None


# ---------------------------------------------------------------- current location is required to store a pass

def test_save_without_current_location_is_stored_blocked(tmp_path):
    att = Attestation("KR", citizenships=("KR",), accepts_terms=True)
    assert check(att) == (True, [])                                   # check() unchanged for compatibility
    save_eligibility(tmp_path, att, True, [], clock=lambda: T0)
    hist = load_history(tmp_path)
    assert hist[-1]["eligible"] is False and hist[-1]["reasons"] == ["CURRENT_LOCATION_REQUIRED"]
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0 + 1)
    assert e.value.reasons == ["ELIGIBILITY_BLOCKED", "CURRENT_LOCATION_REQUIRED"]
    full = Attestation("KR", citizenships=("KR",), accepts_terms=True, current_country="KR")
    save_eligibility(tmp_path, full, True, [], clock=lambda: T0 + DAY)
    require_valid(tmp_path, T0 + DAY + 1)


def test_require_valid_refuses_schema2_pass_without_location(tmp_path):
    rec = {"schema": 2, "seq": 0, "ts_s": int(T0), "expires_s": int(T0) + DEFAULT_TTL_S, "eligible": True,
           "reasons": [], "terms_source": el.TERMS_SOURCE, "residence_country": "KR", "residence_region": "",
           "current_country": "", "current_region": "", "citizenships": ["KR"], "is_entity": False,
           "entity_incorporation": "", "uses_vpn_or_proxy": False, "accepts_terms": True}
    (tmp_path / el.HISTORY_FILE).write_text(json.dumps(rec) + "\n", encoding="utf-8")   # written by other code
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0 + 1)
    assert e.value.reasons == ["CURRENT_LOCATION_REQUIRED"]


def test_blocked_newer_than_pass_refuses(tmp_path):
    attempt(tmp_path, answers("KR"))
    attempt(tmp_path, answers("KR", vpn="yes"), t=T0 + 10)
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0 + 20)
    assert e.value.reasons[0] == "ELIGIBILITY_BLOCKED" and "VPN_OR_PROXY_DECLARED" in e.value.reasons
    assert len(load_history(tmp_path)) == 2


def test_no_attestation_refuses(tmp_path):
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0)
    assert e.value.reasons == ["ELIGIBILITY_NOT_CONFIRMED"]


def test_history_file_is_append_only(tmp_path):
    attempt(tmp_path, answers("US"))
    first = (tmp_path / el.HISTORY_FILE).read_bytes()
    attempt(tmp_path, answers("KR"), t=T0 + 1)
    assert (tmp_path / el.HISTORY_FILE).read_bytes().startswith(first)
    assert not (tmp_path / el.LEGACY_FILE).exists()                    # the single overwritable file is gone


def test_save_cannot_store_a_pass_the_rules_block(tmp_path):
    save_eligibility(tmp_path, Attestation("US", accepts_terms=True, current_country="US"), True, [],
                     clock=lambda: T0)
    assert eligibility_problem(tmp_path, T0) == "ELIGIBILITY_BLOCKED"
    assert load_history(tmp_path)[-1]["reasons"] == ["RESTRICTED_LOCATION:US"]


def test_torn_history_line_is_never_a_pass(tmp_path):
    attempt(tmp_path, answers("KR"))
    with (tmp_path / el.HISTORY_FILE).open("ab") as fh:
        fh.write(b'{"eligible":tr')
    assert eligibility_problem(tmp_path, T0 + 1) == "ELIGIBILITY_BLOCKED"
    attempt(tmp_path, answers("KR"), t=T0 + 2)
    assert eligibility_problem(tmp_path, T0 + 3) is None and len(load_history(tmp_path)) == 3


# ---------------------------------------------------------------- expiry

def test_attestation_expires_after_30_days_by_default(tmp_path):
    attempt(tmp_path, answers())
    assert DEFAULT_TTL_S == 30 * DAY
    require_valid(tmp_path, T0 + 30 * DAY - 1)
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0 + 30 * DAY)
    assert e.value.reasons == ["ELIGIBILITY_EXPIRED"]
    assert eligibility_problem(tmp_path, T0 + 31 * DAY) == "ELIGIBILITY_EXPIRED"


def test_expiry_is_configurable(tmp_path):
    attempt(tmp_path, answers(), ttl_s=3600)
    require_valid(tmp_path, T0 + 3599)
    with pytest.raises(EligibilityError):
        require_valid(tmp_path, T0 + 3600)
    attempt(tmp_path, answers(), t=T0 + 4000)
    with pytest.raises(EligibilityError):
        require_valid(tmp_path, T0 + 4000 + 2 * DAY, max_age_s=DAY)   # caller may demand a fresher one


def test_future_dated_attestation_refused(tmp_path):
    attempt(tmp_path, answers(), t=T0 + DAY)
    with pytest.raises(EligibilityError) as e:
        require_valid(tmp_path, T0)
    assert "ATTESTATION_IN_FUTURE" in e.value.reasons


def test_terms_change_forces_reattest(tmp_path, monkeypatch):
    attempt(tmp_path, answers())
    monkeypatch.setattr(el, "TERMS_SOURCE", "https://app.hyperliquid.xyz/terms (Last updated on 2027)")
    assert eligibility_problem(tmp_path, T0 + 1) == "TERMS_CHANGED_REATTEST"


def test_rules_tightened_after_attestation_refuse(tmp_path, monkeypatch):
    attempt(tmp_path, answers("KR", here="JP", cits="KR"))
    monkeypatch.setattr(el, "RESTRICTED_COUNTRIES", frozenset({"US", "JP"}))
    assert problems(load_history(tmp_path), T0 + 1)[:2] == ["RULES_CHANGED_REATTEST", "RESTRICTED_LOCATION:JP"]


# ---------------------------------------------------------------- legacy single-record file

def legacy(tmp_path, eligible, country="KR"):
    rec = {"eligible": eligible, "reasons": [] if eligible else ["RESTRICTED_LOCATION:US"],
           "terms_source": el.TERMS_SOURCE, "ts_s": int(T0), "residence_country": country,
           "residence_region": "", "citizenships": [country], "is_entity": False, "entity_incorporation": "",
           "uses_vpn_or_proxy": False, "accepts_terms": True}
    (tmp_path / el.LEGACY_FILE).write_text(json.dumps(rec), encoding="utf-8")


def test_legacy_pass_must_reattest(tmp_path):
    legacy(tmp_path, True)
    assert eligibility_problem(tmp_path, T0 + 1) == "REATTEST_REQUIRED"


def test_legacy_block_is_carried_into_history(tmp_path):
    legacy(tmp_path, False, "US")
    assert eligibility_problem(tmp_path, T0 + 1) == "ELIGIBILITY_BLOCKED"
    ok, reasons, _, _ = attempt(tmp_path, answers("KR"), t=T0 + 5)
    assert not ok and reasons[0] == "ELIGIBILITY_COOLDOWN"            # the legacy block starts a cooldown too
    ok, _, out, _ = attempt(tmp_path, answers("KR"), t=T0 + DAY + 5)
    hist = load_history(tmp_path)
    assert ok and [h["eligible"] for h in hist] == [False, True, True]
    assert hist[0]["imported_from"] == el.LEGACY_FILE
    assert "was blocked" in out and (tmp_path / el.LEGACY_FILE).exists()


def test_load_eligibility_keeps_old_shape(tmp_path):
    attempt(tmp_path, answers("KR", here="JP", cits="KR"))
    rec = load_eligibility(tmp_path)
    assert rec["eligible"] and rec["residence_country"] == "KR"
    assert set(rec) <= {"eligible", "reasons", "terms_source", "ts_s", "residence_country", "residence_region",
                        "citizenships", "is_entity", "entity_incorporation", "uses_vpn_or_proxy", "accepts_terms"}
    assert load_history(tmp_path)[-1]["current_country"] == "JP"


def test_region_country_mismatch_still_blocked_by_check():
    ok, why = check(Attestation("KR", residence_region="CA-ON", citizenships=("KR",), accepts_terms=True,
                                current_country="KR"))
    assert not ok and "REGION_COUNTRY_MISMATCH:RESIDENCE:CA-ON" in why
