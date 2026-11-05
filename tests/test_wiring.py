"""Wiring: config -> live engine (offline, throwaway key, fake transport)."""
import os
from dataclasses import replace

import pytest
from eth_account import Account

from omx import config as C
from omx.audit import AuditLog
from omx.engine import Engine, from_config


def _cfg(tmp_path, **kw):
    agent = Account.create()
    keyfile = tmp_path / "agent.key"
    keyfile.write_text(agent.key.hex(), encoding="utf-8")
    main = Account.create().address
    text = C.render(account_address=main, network="testnet", key_path=str(keyfile), **kw)
    return C.loads(text, base_dir=tmp_path), agent, main


class NoNet:
    def __call__(self, path, payload):
        raise AssertionError("no network in tests")


@pytest.fixture(autouse=True)
def eligible_stub(monkeypatch):
    """from_config calls omx.eligibility.require_valid (lane Z); a passing stub here."""
    from omx import eligibility
    calls = []
    monkeypatch.setattr(eligibility, "require_valid", lambda state_dir, now_s: calls.append((state_dir, now_s)),
                        raising=False)
    return calls


def test_from_config_requires_valid_eligibility(tmp_path, monkeypatch, eligible_stub):
    from omx import eligibility
    cfg, agent, main = _cfg(tmp_path)
    from_config(cfg, transport=NoNet())
    assert eligible_stub and eligible_stub[0][0] == cfg.state_dir

    def refuse(state_dir, now_s):
        raise RuntimeError("ELIGIBILITY_EXPIRED")
    monkeypatch.setattr(eligibility, "require_valid", refuse)
    with pytest.raises(RuntimeError, match="ELIGIBILITY_EXPIRED"):
        from_config(cfg, transport=NoNet())
    monkeypatch.delattr(eligibility, "require_valid")
    with pytest.raises(RuntimeError, match="ELIGIBILITY_GATE_MISSING"):
        from_config(cfg, transport=NoNet())


def test_from_config_builds_engine_with_agent_key(tmp_path):
    cfg, agent, main = _cfg(tmp_path)
    eng = from_config(cfg, transport=NoNet())
    assert isinstance(eng, Engine)
    assert eng.venue.signer.address == agent.address
    assert agent.key.hex() not in repr(eng.venue.signer)
    assert eng.venue.is_mainnet is False


def test_from_env_signer(tmp_path, monkeypatch):
    cfg, agent, main = _cfg(tmp_path)
    monkeypatch.setenv("OMX_TEST_AGENT_KEY", agent.key.hex())
    cfg = replace(cfg, signer_key_path=None, signer_key_env="OMX_TEST_AGENT_KEY")
    eng = from_config(cfg, transport=NoNet())
    assert eng.venue.signer.address == agent.address


def test_main_wallet_key_refused(tmp_path):
    cfg, agent, main = _cfg(tmp_path)
    cfg = replace(cfg, account_address=agent.address)   # signer == main account -> refused
    with pytest.raises(Exception, match="main wallet"):
        from_config(cfg, transport=NoNet())


def test_audit_fsync_only_for_durable_kinds(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
    log = AuditLog(tmp_path / "a.jsonl")
    log.append("child_decision", {"x": 1})
    log.append("preview", {"x": 2})
    assert calls == []
    log.append("child_sent", {"x": 3})
    log.append("kill", {"x": 4})
    assert len(calls) == 2
    assert AuditLog.verify(tmp_path / "a.jsonl")[0]
