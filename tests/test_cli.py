import io
import json

import pytest

from omx.audit import AuditLog
from omx.cli import main
from omx.config import load, render
from omx.onboarding import load_eligibility, max_fee_rate_str

from test_mcp import ACCOUNT, FakeEngine, confirming_console, daemon_running, make_eligible

FAKE_KEY = "0x" + "1f" * 32
BUILDER = "0x" + "cd" * 20
T0 = 1_800_000_000.0

from answer_flow import flow

ELIGIBLE_ANSWERS = ["no", "KR", "yes", "", "no", "yes"]   # entity, residence, here?, citizenship(=KR), vpn, terms


class Inputs:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def run(argv, answers=(), factory=None, clock=lambda: T0):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, engine_factory=factory or (lambda c: None), input_fn=Inputs(answers), out=out, err=err,
                clock=clock)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text(render(ACCOUNT, "testnet", key_path="secrets/agent.key", builder_address=BUILDER,
                        fee_tenths_bps=10, agent_enabled=True), encoding="utf-8")
    make_eligible(tmp_path / "state")
    return p


def test_init_runs_onboarding_and_writes_config(tmp_path):
    p = tmp_path / "omx.toml"
    code, out, err = run(["--config", str(p), "init", "--account", ACCOUNT, "--key-path", "k/agent.key"],
                         ELIGIBLE_ANSWERS)
    assert code == 0, err
    assert p.exists() and "never asks for your MAIN wallet private key" in out
    rec = load_eligibility(tmp_path / "state")
    assert rec["eligible"] and rec["residence_country"] == "KR"
    code, _, err = run(["--config", str(p), "init", "--account", ACCOUNT, "--key-path", "x"])
    assert code == 1 and "exists" in err


def test_init_default_builder_is_used_only_when_set_and_can_be_declined(tmp_path, monkeypatch):
    from omx import config as config_mod
    from omx.config import load
    base = ["init", "--account", ACCOUNT, "--network", "mainnet", "--key-path", "k/agent.key"]

    def cfg_after(extra, name):
        p = tmp_path / name
        code, _, err = run(["--config", str(p)] + base + extra, ELIGIBLE_ANSWERS)
        assert code == 0, err
        return load(p)

    c = cfg_after([], "none.toml")                                   # no default shipped: no builder code
    assert c.builder_address is None and c.builder_fee_tenths_bps == 0
    monkeypatch.setitem(config_mod.DEFAULT_BUILDER, "mainnet", BUILDER)
    c = cfg_after([], "default.toml")
    assert c.builder_address.lower() == BUILDER.lower() and c.builder_fee_tenths_bps == 10
    c = cfg_after(["--no-builder"], "declined.toml")
    assert c.builder_address is None and c.builder_fee_tenths_bps == 0
    c = cfg_after(["--fee-tenths-bps", "5"], "lower.toml")
    assert c.builder_address.lower() == BUILDER.lower() and c.builder_fee_tenths_bps == 5
    other = "0x" + "12" * 20
    c = cfg_after(["--builder-address", other], "own.toml")           # an explicit builder keeps the old fee default
    assert c.builder_address.lower() == other and c.builder_fee_tenths_bps == 0


def test_init_blocked_resident_writes_nothing(tmp_path):
    p = tmp_path / "omx.toml"
    code, out, _ = run(["--config", str(p), "init", "--account", ACCOUNT, "--key-env", "OMX_KEY"],
                       flow(["no", "US", "", "US", "", "US", "no", "yes"]))
    assert code == 2 and not p.exists()
    assert "RESTRICTED_LOCATION:US" in out
    assert load_eligibility(tmp_path / "state")["eligible"] is False


@pytest.mark.parametrize("answers,reason", [
    (flow(["no", "CA", "CA-ON", "CA", "CA-ON", "CA", "no", "yes"]), "RESTRICTED_REGION:CA-ON"),
    (flow(["no", "KR", "", "KR", "", "KR", "yes", "yes"]), "VPN_OR_PROXY_DECLARED"),
    (flow(["no", "DE", "", "DE", "", "DE, IR", "no", "yes"]), "SANCTIONED_CITIZENSHIP:IR"),
    (flow(["no", "KR", "", "KR", "", "KR", "no", "no"]), "TERMS_NOT_ACCEPTED"),
])
def test_eligibility_blocks(cfgfile, answers, reason):
    code, out, _ = run(["--config", str(cfgfile), "eligibility"], answers)
    assert code == 2 and reason in out
    code, _, err = run(["--config", str(cfgfile), "preview", "ETH", "buy", "1"])
    assert code == 1 and "ELIGIBILITY_BLOCKED" in err
    code, _, err = run(["--config", str(cfgfile), "daemon"], factory=lambda c: FakeEngine())
    assert code == 1 and "ELIGIBILITY_BLOCKED" in err


def test_key_on_command_line_refused(cfgfile):
    code, out, err = run(["--config", str(cfgfile), "init", "--account", ACCOUNT, "--key-path", FAKE_KEY])
    assert code == 2 and "private key" in err
    assert FAKE_KEY not in out + err


def test_engine_commands_need_a_live_daemon(cfgfile):
    for argv in (["submit", "ETH", "buy", "1", "--yes"], ["preview", "ETH", "buy", "1"], ["status", "hu-x"],
                 ["cancel", "hu-x"], ["approvals", "approve", "ap-000000000000", "--yes"]):
        code, _, err = run(["--config", str(cfgfile)] + argv)
        assert code == 1 and "omx daemon" in err, argv


def test_preview_and_submit(cfgfile):
    eng = FakeEngine()
    with daemon_running(load(cfgfile), lambda c: eng):
        code, out, _ = run(["--config", str(cfgfile), "preview", "ETH", "buy", "2", "--algo", "twap",
                            "--duration-s", "60", "--slices", "2"])
        assert code == 0 and '"source": "human"' in out and "0.01%" in out and eng.submitted == []
        code, out, _ = run(["--config", str(cfgfile), "submit", "ETH", "buy", "2"], ["n"])
        assert code == 1 and eng.submitted == []
        code, out, _ = run(["--config", str(cfgfile), "submit", "ETH", "buy", "2"], ["y"])
        assert code == 0 and eng.submitted[0].source == "human" and eng.submitted[0].intent_id.startswith("hu-")
        assert json.loads(out[: out.index("\nBuilder fee")])["intent"]["intent_id"] == eng.submitted[0].intent_id
        code, _, err = run(["--config", str(cfgfile), "preview", "DOGE", "buy", "1"])
        assert code == 1 and "COIN_NOT_ALLOWED" in err


def test_status_cancel_kill_unkill_through_daemon(cfgfile):
    eng = FakeEngine()
    with daemon_running(load(cfgfile), lambda c: eng, console=confirming_console()):
        run(["--config", str(cfgfile), "submit", "ETH", "buy", "1", "--yes"])
        iid = eng.submitted[0].intent_id
        code, out, _ = run(["--config", str(cfgfile), "status", iid])
        assert code == 0 and '"working"' in out
        assert run(["--config", str(cfgfile), "cancel", iid])[0] == 0 and eng.cancelled == [iid]
        code, out, _ = run(["--config", str(cfgfile), "kill", "--reason", "test", "--all"])
        assert code == 0 and eng.killed and eng.scopes == ["all"] and '"daemon"' in out
        code, out, _ = run(["--config", str(cfgfile), "unkill"], ["i understand"])
        assert code == 1 and eng.killed and eng.unkills == []        # wrong phrase never reaches the engine
        code, _, _ = run(["--config", str(cfgfile), "unkill"], ["I UNDERSTAND"])
        assert code == 0 and not eng.killed and eng.unkills == ["I UNDERSTAND"]


def queue_agent_intent(cfgfile, size=20.0):
    from omx.approvals import AgentBudget, ApprovalQueue
    from omx.mcp_server import AgentGateway
    cfg = load(cfgfile)
    audit = AuditLog(cfg.audit_path)
    gw = AgentGateway(FakeEngine(), cfg, ApprovalQueue(cfg.state_dir, audit, clock=lambda: T0),
                      AgentBudget(cfg.state_dir, cfg.agent.daily_budget_usd, lambda: T0), audit,
                      clock=lambda: T0)
    res = gw.submit_order("agent:tester", {"coin": "ETH", "side": "buy", "size": size})
    assert res["status"] == "pending_approval"
    return res["approval_id"], cfg


def kinds(cfg):
    return [json.loads(l)["kind"] for l in cfg.audit_path.read_text().splitlines()]


def test_approvals_approve_executed_by_daemon_once(cfgfile):
    aid, cfg = queue_agent_intent(cfgfile)
    code, out, _ = run(["--config", str(cfgfile), "approvals", "list"])           # no daemon: local
    assert code == 0 and aid in out
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng, clock=lambda: T0, console=confirming_console()):
        code, out, _ = run(["--config", str(cfgfile), "approvals", "list"])       # daemon: via IPC
        assert code == 0 and aid in out
        code, _, _ = run(["--config", str(cfgfile), "approvals", "approve", aid], ["y"])
        assert code == 0 and len(eng.submitted) == 1 and eng.submitted[0].source == "agent:tester"
        code, _, err = run(["--config", str(cfgfile), "approvals", "approve", aid, "--yes"])
        assert code == 1 and "executed" in err and len(eng.submitted) == 1
    assert json.loads((cfg.state_dir / "agent_budget.json").read_text())["used_usd"] > 2000
    code, out, _ = run(["--config", str(cfgfile), "audit", "verify"])
    assert code == 0 and out.startswith("OK"), out
    # the second approve stops at the CLI (approvals.get says "executed"); nothing reaches the console
    assert kinds(cfg) == ["approval_queued", "daemon_start", "human_op_pending", "human_op_confirmed",
                          "approval_approved", "approval_executed", "daemon_stop"]


def test_approvals_reject_and_expiry(cfgfile):
    aid, cfg = queue_agent_intent(cfgfile)
    assert run(["--config", str(cfgfile), "approvals", "reject", aid, "--note", "no"])[0] == 0
    aid2, _ = queue_agent_intent(cfgfile)
    eng, clock = FakeEngine(), Clock()
    with daemon_running(cfg, lambda c: eng, clock=clock, console=confirming_console()):
        assert run(["--config", str(cfgfile), "approvals", "approve", aid, "--yes"])[0] == 1
        clock.t = T0 + 601
        code, _, err = run(["--config", str(cfgfile), "approvals", "approve", aid2, "--yes"])
        assert code == 1 and "expired" in err and eng.submitted == []


def test_audit_verify_detects_tampering(cfgfile, tmp_path):
    queue_agent_intent(cfgfile)
    queue_agent_intent(cfgfile)
    path = tmp_path / "state" / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0].replace("20.0", "2.0")] + lines[1:]) + "\n")
    code, out, _ = run(["--config", str(cfgfile), "audit", "verify"])
    assert code == 3 and out.startswith("BROKEN")


def test_fees_disclosure(cfgfile, tmp_path):
    code, out, _ = run(["--config", str(cfgfile), "fees"])
    assert code == 0 and "1 bps" in out and "exactly: 0.01%" in out and BUILDER in out and "1.00 USD" in out
    p = tmp_path / "zero.toml"
    p.write_text(render(ACCOUNT, "testnet", key_env="OMX_KEY"), encoding="utf-8")
    code, out, _ = run(["--config", str(p), "fees"])
    assert code == 0 and "Builder fee: none" in out
    assert [max_fee_rate_str(f) for f in (0, 1, 10, 20, 100)] == ["0%", "0.001%", "0.01%", "0.02%", "0.1%"]


def test_bad_config_reported(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text(render(ACCOUNT, "testnet", key_env="OMX_KEY") + f'\n# {FAKE_KEY}\n', encoding="utf-8")
    code, out, err = run(["--config", str(p), "fees"])
    assert code == 2 and "looks like a private key" in err and FAKE_KEY not in err


def test_kill_without_daemon_uses_one_shot_engine(cfgfile, tmp_path):
    from omx.killswitch import is_killed
    eng = FakeEngine()
    code, out, _ = run(["--config", str(cfgfile), "kill", "--reason", "stop now", "--all"], factory=lambda c: eng)
    assert code == 0 and is_killed(tmp_path / "state") and eng.scopes == ["all"]
    assert eng.kills == ["human:cli: stop now"] and '"one_shot_engine"' in out


def test_kill_without_daemon_or_engine_still_sets_file(cfgfile, tmp_path):
    from omx.killswitch import is_killed
    state = tmp_path / "state"

    def broken(c):
        raise RuntimeError("venue unreachable")
    code, out, _ = run(["--config", str(cfgfile), "kill", "--reason", "stop now"], factory=broken)
    assert code == 3 and is_killed(state) and "NOT cancelled" in out and "venue unreachable" in out
    assert run(["--config", str(cfgfile), "unkill"], ["no"])[0] == 1 and is_killed(state)
    code, out, _ = run(["--config", str(cfgfile), "unkill"], ["I UNDERSTAND"])
    assert code == 0 and not is_killed(state)
    assert kinds(load(cfgfile)) == ["kill_external", "unkill_external"]


def test_eligibility_through_daemon_uses_its_audit_writer(cfgfile):
    from omx.eligibility import load_history
    cfg = load(cfgfile)
    with daemon_running(cfg, lambda c: FakeEngine(), console=confirming_console()):
        code, out, _ = run(["--config", str(cfgfile), "eligibility"], ELIGIBLE_ANSWERS)
        assert code == 0 and "confirmed until" in out
        code, out, _ = run(["--config", str(cfgfile), "eligibility"], flow(["no", "CA", "CA-ON", "CA", "CA-ON", "CA", "no", "yes"]))
        assert code == 2 and "RESTRICTED_REGION:CA-ON" in out
    hist = load_history(cfg.state_dir)
    assert hist[-1]["eligible"] is False and hist[-1]["expires_s"] - hist[-1]["ts_s"] == 30 * 86400
    assert kinds(cfg).count("eligibility_attempt") == 2
    assert run(["--config", str(cfgfile), "audit", "verify"])[0] == 0
    code, _, err = run(["--config", str(cfgfile), "submit", "ETH", "buy", "1", "--yes"])
    assert code == 1 and "ELIGIBILITY_BLOCKED" in err


def test_eligibility_without_daemon_audited_with_config_ttl(tmp_path):
    from omx.eligibility import load_history
    p = tmp_path / "omx.toml"
    p.write_text(render(ACCOUNT, "testnet", key_env="OMX_KEY").replace("eligibility_ttl_days = 30",
                                                                      "eligibility_ttl_days = 7"), encoding="utf-8")
    code, out, _ = run(["--config", str(p), "eligibility"], ELIGIBLE_ANSWERS)
    assert code == 0
    rec = load_history(tmp_path / "state")[-1]
    assert rec["eligible"] and rec["expires_s"] - rec["ts_s"] == 7 * 86400
    assert kinds(load(p)) == ["eligibility_attempt"]


def test_audit_verify_reports_torn_tail_and_repair_needs_phrase(cfgfile, tmp_path):
    queue_agent_intent(cfgfile)
    path = tmp_path / "state" / "audit.jsonl"
    with path.open("ab") as fh:
        fh.write(b'{"data":{"x":2},"kind":"child_dec')          # crash mid-write
    code, out, _ = run(["--config", str(cfgfile), "audit", "verify"])
    assert code == 4 and out.startswith("TRUNCATED_TAIL") and "omx audit repair" in out and "BROKEN" not in out
    assert run(["--config", str(cfgfile), "audit", "repair"], ["yes"])[0] == 1           # wrong phrase
    assert run(["--config", str(cfgfile), "audit", "verify"])[0] == 4
    code, out, _ = run(["--config", str(cfgfile), "audit", "repair"], ["REPAIR AUDIT"])
    assert code == 0 and "now OK" in out and list(path.parent.glob("audit.jsonl.tail-*"))
    assert run(["--config", str(cfgfile), "audit", "verify"])[0] == 0


def test_audit_repair_refuses_tampered_log(cfgfile, tmp_path):
    queue_agent_intent(cfgfile)
    queue_agent_intent(cfgfile)
    path = tmp_path / "state" / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0].replace("20.0", "2.0")] + lines[1:]) + "\n")
    code, out, _ = run(["--config", str(cfgfile), "audit", "repair"], ["REPAIR AUDIT"])
    assert code == 3 and "repair refused" in out
