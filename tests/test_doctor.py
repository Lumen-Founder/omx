"""`omx doctor` (offline: a fake /info transport, throwaway keys in tmp dirs)."""
import io
import time

import pytest
from eth_account import Account

from omx import ipc
from omx.cli import main
from omx.config import render
from omx.doctor import checks, run_doctor

from test_mcp import make_eligible

BUILDER = "0x" + "be" * 20


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class FakeInfo:
    def __init__(self, account, agent, over=None):
        self.calls, self.urls = [], []
        now_ms = int(time.time() * 1000)
        self.r = {
            ("userRole", account.lower()): {"role": "user"},
            ("userRole", agent.lower()): {"role": "agent", "data": {"user": account.lower()}},
            ("clearinghouseState", account.lower()): {"marginSummary": {"accountValue": "743.877"}},
            ("clearinghouseState", BUILDER): {"marginSummary": {"accountValue": "150.0"}},
            ("extraAgents", account.lower()): [{"name": "omx", "address": agent, "validUntil": now_ms + 86_400_000}],
            ("maxBuilderFee", account.lower()): 10,
            ("userAbstraction", account.lower()): "disabled",
            ("userAbstraction", BUILDER): "default",
        }
        self.r.update(over or {})

    def factory(self, url):
        self.urls.append(url)
        return self

    def __call__(self, path, payload):
        assert path == "/info"
        self.calls.append(payload)
        r = self.r[(payload["type"], payload["user"])]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def setup(tmp_path):
    agent = Account.create()
    key_hex = "0x" + bytes(agent.key).hex()
    kp = tmp_path / "agent.key"
    kp.write_text(key_hex, encoding="utf-8")
    account = Account.create().address
    p = tmp_path / "omx.toml"
    p.write_text(render(account, "testnet", key_path=str(kp), builder_address=BUILDER, fee_tenths_bps=10),
                 encoding="utf-8")
    make_eligible(tmp_path / "state")
    ipc.write_heartbeat(tmp_path / "state", {"pid": 1, "loop_progress_ms": time.time() * 1000})
    return p, account, agent.address, key_hex, kp


def by_name(res):
    return {c.name: c for c in res}


def test_all_ok_and_the_key_is_never_printed(setup):
    p, account, agent, key_hex, _ = setup
    fake = FakeInfo(account, agent)
    out = io.StringIO()
    assert run_doctor(p, out, fake.factory) == 0
    text = out.getvalue()
    assert "all OK" in text and "FAIL" not in text
    assert key_hex[2:].lower() not in text.lower() and key_hex[2:].upper() not in text
    assert agent.lower() in text and "743.88 USDC" in text
    assert fake.urls == ["https://api.hyperliquid-testnet.xyz"]
    assert {c["type"] for c in fake.calls} == {"userRole", "clearinghouseState", "extraAgents", "maxBuilderFee",
                                                "userAbstraction"}
    assert {"type": "maxBuilderFee", "user": account.lower(), "builder": BUILDER} in fake.calls


def test_failures_have_fixes(setup):
    p, account, agent, key_hex, _ = setup
    fake = FakeInfo(account, agent, {
        ("userRole", account.lower()): {"role": "missing"},
        ("clearinghouseState", account.lower()): {"marginSummary": {"accountValue": "0.0"}},
        ("extraAgents", account.lower()): [],
        ("userRole", agent.lower()): {"role": "missing"},
        ("maxBuilderFee", account.lower()): 5,
        ("clearinghouseState", BUILDER): {"marginSummary": {"accountValue": "99.99"}},
    })
    (p.parent / "state" / ipc.HEARTBEAT).unlink()
    res = by_name(checks(p, fake.factory))
    for name in ("account exists", "account value", "agent approved", "builder approval", "builder account value",
                 "daemon running"):
        assert res[name].status == "FAIL" and res[name].fix, name
    assert "omx approve-builder" in res["builder approval"].fix and "100 USDC" in res["builder account value"].fix
    out = io.StringIO()
    assert run_doctor(p, out, fake.factory) == 1 and "6 failed" in out.getvalue()
    assert key_hex[2:].lower() not in out.getvalue().lower()


def test_agent_key_is_the_main_key(tmp_path):
    main_wallet = Account.create()
    kp = tmp_path / "main.key"
    kp.write_text("0x" + bytes(main_wallet.key).hex(), encoding="utf-8")
    p = tmp_path / "omx.toml"
    p.write_text(render(main_wallet.address, "testnet", key_path=str(kp)), encoding="utf-8")
    res = by_name(checks(p, FakeInfo(main_wallet.address, main_wallet.address).factory))
    assert res["agent address"].status == "FAIL" and "MAIN wallet key" in res["agent address"].detail
    assert res["builder approval"].status == "SKIP" and res["builder account value"].status == "SKIP"
    assert res["eligibility"].status == "FAIL" and "omx eligibility" in res["eligibility"].fix


def test_missing_key_file_bad_config_and_read_errors(setup, tmp_path):
    p, account, agent, _, kp = setup
    kp.unlink()
    fake = FakeInfo(account, agent, {("maxBuilderFee", account.lower()): RuntimeError("timeout")})
    res = by_name(checks(p, fake.factory))
    assert res["agent key file"].status == "FAIL" and "agent address" not in res
    assert res["agent approved"].status == "SKIP"
    assert res["builder approval"].status == "FAIL" and "timeout" in res["builder approval"].detail
    assert res["builder account value"].status == "OK"             # one failed read does not stop the rest
    bad = tmp_path / "bad.toml"
    bad.write_text("network = 'moon'\n", encoding="utf-8")
    only = checks(bad, fake.factory)
    assert len(only) == 1 and only[0].status == "FAIL" and only[0].name == "config loads"


def test_expired_agent_and_role_fallback(setup):
    p, account, agent, _, _ = setup
    old = FakeInfo(account, agent, {("extraAgents", account.lower()):
                                      [{"name": "old", "address": agent, "validUntil": 1_000}]})
    assert by_name(checks(p, old.factory))["agent approved"].status == "FAIL"
    role = FakeInfo(account, agent, {("extraAgents", account.lower()): []})
    c = by_name(checks(p, role.factory))["agent approved"]
    assert c.status == "OK" and "userRole" in c.detail


def test_cli_doctor(setup):
    p, account, agent, key_hex, _ = setup
    out, err = io.StringIO(), io.StringIO()
    code = main(["--config", str(p), "doctor"], out=out, err=err,
                transport_factory=FakeInfo(account, agent).factory)
    assert code == 0 and "all OK" in out.getvalue() and key_hex[2:].lower() not in out.getvalue().lower()


def test_portfolio_margin_account_and_non_standard_builder_fail_with_fixes(setup):
    p, account, agent, _, _ = setup
    fake = FakeInfo(account, agent, {("userAbstraction", account.lower()): "portfolioMargin",
                                     ("userAbstraction", BUILDER): "portfolioMargin"})
    res = by_name(checks(p, fake.factory))
    assert res["account mode"].status == "FAIL" and "Manual / Standard" in res["account mode"].fix
    assert "ACCOUNT_MODE_UNSUPPORTED" in res["account mode"].detail
    assert res["builder account mode"].status == "FAIL" and "standard" in res["builder account mode"].detail
