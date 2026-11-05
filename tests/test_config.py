from pathlib import Path

import pytest

from omx.config import ConfigError, contains_key_like, load, loads, render

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "0x" + "ab" * 20
BUILDER = "0x" + "cd" * 20
FAKE_KEY = "0x" + "1f" * 32          # 64 hex: synthetic, shaped like a key


def base(**kw):
    return render(ACCOUNT, "testnet", key_path="secrets/agent.key", **kw)


def test_example_config_loads():
    cfg = load(ROOT / "config.example.toml")
    assert cfg.network == "testnet" and cfg.builder_fee_tenths_bps == 0 and cfg.builder_address is None
    assert cfg.signer_key_path == "secrets/agent.key" or cfg.signer_key_path.endswith(".key")
    assert cfg.agent.enabled is False
    assert cfg.agent_coins == frozenset({"BTC", "ETH"})
    assert cfg.audit_path == cfg.state_dir / "audit.jsonl"


def test_render_round_trip_and_policy_fields(tmp_path):
    cfg = loads(base(builder_address=BUILDER, fee_tenths_bps=10), base_dir=tmp_path)
    assert cfg.builder_fee_bps == 1.0
    assert cfg.policy.max_leverage == 3.0 and cfg.policy.max_data_age_ms == 3000
    assert cfg.policy.allowed_coins == frozenset({"BTC", "ETH", "SOL"})
    assert cfg.state_dir == tmp_path / "state"


@pytest.mark.parametrize("where", [
    lambda t: t.replace('key_path = "secrets/agent.key"', f'key_path = "{FAKE_KEY}"'),
    lambda t: t + f"\n# my key {FAKE_KEY}\n",                       # even in a comment
    lambda t: t.replace('state_dir = "state"', f'state_dir = "{FAKE_KEY[2:]}"'),   # bare 64 hex
    lambda t: t.replace("[agent]", f'[agent]\nnote = "{FAKE_KEY.upper()[2:]}"'),
])
def test_key_like_strings_rejected_anywhere(where):
    with pytest.raises(ConfigError, match="private key"):
        loads(where(base()))


def test_contains_key_like():
    assert contains_key_like(FAKE_KEY)
    assert not contains_key_like(ACCOUNT)        # 40-hex addresses are fine
    assert not contains_key_like("0x" + "a" * 63)


@pytest.mark.parametrize("edit,msg", [
    (lambda t: t.replace('network = "testnet"', 'network = "devnet"'), "network"),
    (lambda t: t.replace(ACCOUNT, "0x1234"), "account_address"),
    (lambda t: t.replace('key_path = "secrets/agent.key"', 'key_path = "a"\nkey_env = "OMX_KEY"'), "exactly one"),
    (lambda t: t.replace('key_path = "secrets/agent.key"', ''), "exactly one"),
    (lambda t: t.replace('key_path = "secrets/agent.key"', 'key_env = "lower-case"'), "key_env"),
    (lambda t: t.replace("fee_tenths_bps = 0", "fee_tenths_bps = 101"), "fee_tenths_bps"),
    (lambda t: t.replace("fee_tenths_bps = 0", "fee_tenths_bps = 10"), "requires builder.address"),
    (lambda t: t.replace("fee_tenths_bps = 0", "fee_tenths_bps = 1.5"), "fee_tenths_bps"),
    (lambda t: t.replace("max_leverage = 3.0", "max_leverage = -1.0"), "max_leverage"),
    (lambda t: t.replace("max_leverage = 3.0", "max_leverage = 80.0"), "max_leverage"),
    (lambda t: t.replace("max_leverage = 3.0", 'max_leverage = "3"'), "max_leverage"),
    (lambda t: t.replace("max_child_notional_usd = 5000.0", "max_child_notional_usd = 99999.0"), "max_child"),
    (lambda t: t.replace('allowed_coins = ["BTC", "ETH"]', 'allowed_coins = ["DOGE"]'), "subset"),
    (lambda t: t.replace("enabled = false", "enabled = 1"), "agent.enabled"),
    (lambda t: "surprise = 1\n" + t, "unknown top-level"),
    (lambda t: t.replace("[agent]", "[agent]\nbogus = 1"), "unknown key"),
    (lambda t: t.replace("[signer]", "[signer]\nprivate_key_hint = 1"), "unknown key"),
    (lambda t: t.replace("# BTC = 15000.0", "BTC = 0.0"), "max_coin_notional_usd"),
    (lambda t: t.replace("approval_ttl_s = 600", "approval_ttl_s = 5"), "approval_ttl_s"),
    (lambda t: t + "\n[extra\n", "TOML"),
])
def test_strict_validation(edit, msg):
    with pytest.raises(ConfigError, match=msg):
        loads(edit(base()))


def test_secret_named_fields_rejected():
    t = base().replace("[agent]", '[agent.x]\nmnemonic = "word word"\n[agent]')
    with pytest.raises(ConfigError, match="secret-bearing"):
        loads(t)


def test_env_signer_and_coin_caps(tmp_path):
    t = render(ACCOUNT, "mainnet", key_env="OMX_AGENT_KEY").replace("# BTC = 15000.0", "BTC = 15000.0")
    cfg = loads(t)
    assert cfg.signer_key_env == "OMX_AGENT_KEY" and cfg.signer_key_path is None
    assert cfg.policy.coin_cap("BTC") == 15000.0 and cfg.policy.coin_cap("ETH") == 10000.0


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml")


def test_eligibility_ttl_days(tmp_path):
    assert loads(base()).eligibility_ttl_days == 30 and loads(base()).eligibility_ttl_s == 30 * 86400
    assert loads(base().replace("eligibility_ttl_days = 30", "eligibility_ttl_days = 3")).eligibility_ttl_days == 3
    assert loads(base().replace("eligibility_ttl_days = 30\n", "")).eligibility_ttl_days == 30
    for bad in ("0", "31", "7.5", '"30"'):
        with pytest.raises(ConfigError, match="eligibility_ttl_days"):
            loads(base().replace("eligibility_ttl_days = 30", f"eligibility_ttl_days = {bad}"))
