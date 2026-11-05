"""OMX configuration: one TOML file, validated strictly.

The config holds only public data and references. The signer key is named by a
file PATH or an environment variable NAME; the key itself never appears here.
Loading the key is the venue adapter's job at runtime. Any string that looks
like a private key (0x + 64 hex, or a bare 64-hex run) makes the whole file
invalid, comments included.
"""
from __future__ import annotations

import dataclasses
import math
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .policy import PolicyConfig

KEY_LIKE = re.compile(r"(?:0x)?(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
COIN = re.compile(r"^[A-Za-z0-9]{1,15}$")
NETWORKS = ("mainnet", "testnet")
MAX_BUILDER_FEE_TENTHS_BPS = 100       # 10 bps = 0.1 %, Hyperliquid's perp builder-fee ceiling
MAX_ELIGIBILITY_TTL_DAYS = 30           # attestations are renewed at least monthly; shorter is allowed
FORBIDDEN_NAMES = {"private_key", "secret", "secret_key", "mnemonic", "seed", "seed_phrase", "api_secret", "key"}
# The OMX maintainers' builder code, used by `omx init` when no --builder-address / --no-builder is given.
# It only takes effect after the user approves it from their own wallet (`omx approve-builder`); `omx fees`
# shows it in plain words. None = no default on that network.
DEFAULT_BUILDER = {"mainnet": "0x34C752D1092faE5C01c5CF00798F16534065E1f1", "testnet": None}
DEFAULT_BUILDER_FEE_TENTHS_BPS = 10     # 1 bps


class ConfigError(ValueError):
    pass


def contains_key_like(text: str) -> bool:
    return bool(KEY_LIKE.search(text or ""))


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    approval_above_usd: float = 1_000.0     # per-intent notional above this needs a human
    daily_budget_usd: float = 5_000.0       # agent notional per UTC day before every intent needs a human
    allowed_coins: frozenset = frozenset()  # empty -> same as policy.allowed_coins
    approval_ttl_s: int = 600


@dataclass(frozen=True)
class OMXConfig:
    network: str
    account_address: str
    signer_key_path: Optional[str]
    signer_key_env: Optional[str]
    builder_address: Optional[str]
    builder_fee_tenths_bps: int
    policy: PolicyConfig
    state_dir: Path
    audit_path: Path
    agent: AgentSettings
    source_path: Optional[Path] = None
    eligibility_ttl_days: int = MAX_ELIGIBILITY_TTL_DAYS
    order_expiry_ms: int = 15_000         # expiresAfter of every signed action (R3-4)
    lock_dir: Optional[Path] = None       # per-account lock dir (R3-5); None = $OMX_LOCK_DIR or the user default

    @property
    def eligibility_ttl_s(self) -> int:
        return self.eligibility_ttl_days * 86_400

    @property
    def agent_coins(self) -> frozenset:
        return self.agent.allowed_coins or self.policy.allowed_coins

    @property
    def builder_fee_bps(self) -> float:
        return self.builder_fee_tenths_bps / 10.0


# ---------------------------------------------------------------- helpers

def _num(v: Any, name: str, lo: float = 0.0, hi: float = math.inf, lo_open: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ConfigError(f"{name}: must be a finite number")
    if v < lo or (lo_open and v == lo) or v > hi:
        raise ConfigError(f"{name}: {v} outside allowed range")
    return float(v)


def _int(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ConfigError(f"{name}: must be an integer in [{lo}, {hi}]")
    return v


def _bool(v: Any, name: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(f"{name}: must be true or false")
    return v


def _coins(v: Any, name: str) -> frozenset:
    if not isinstance(v, list) or not all(isinstance(c, str) and COIN.match(c) for c in v):
        raise ConfigError(f"{name}: must be a list of coin symbols")
    return frozenset(v)


def _table(raw: dict, name: str, allowed: set) -> dict:
    t = raw.get(name, {})
    if not isinstance(t, dict):
        raise ConfigError(f"[{name}] must be a table")
    extra = set(t) - allowed
    if extra:
        raise ConfigError(f"[{name}] unknown key(s): {sorted(extra)}")
    return t


def _walk_names(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in FORBIDDEN_NAMES:
                raise ConfigError(f"{path}{k}: secret-bearing keys are not allowed in the config")
            _walk_names(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for v in obj:
            _walk_names(v, path)


_POLICY_FLOATS = {
    "min_day_ntl_usd": (0.0, False), "min_depth_usd": (0.0, False), "depth_band_bps": (0.0, True),
    "price_band_bps": (0.0, True), "default_coin_notional_usd": (0.0, True),
    "max_account_notional_usd": (0.0, True), "max_leverage": (0.0, True),
    "max_daily_loss_usd": (0.0, True), "max_child_notional_usd": (0.0, True),
}


def _policy(t: dict) -> PolicyConfig:
    kw: dict = {}
    for k, (lo, open_) in _POLICY_FLOATS.items():
        if k in t:
            kw[k] = _num(t[k], f"policy.{k}", lo, lo_open=open_)
    if "max_leverage" in kw and kw["max_leverage"] > 50:
        raise ConfigError("policy.max_leverage: above 50 is not allowed")
    if "price_band_bps" in kw and kw["price_band_bps"] > 1_000:
        raise ConfigError("policy.price_band_bps: above 1000 is not allowed")
    if "max_data_age_ms" in t:
        kw["max_data_age_ms"] = _int(t["max_data_age_ms"], "policy.max_data_age_ms", 100, 60_000)
    if "allowed_coins" in t:
        kw["allowed_coins"] = _coins(t["allowed_coins"], "policy.allowed_coins")
        if not kw["allowed_coins"]:
            raise ConfigError("policy.allowed_coins: must not be empty")
    if "allow_agent_sources" in t:
        kw["allow_agent_sources"] = _bool(t["allow_agent_sources"], "policy.allow_agent_sources")
    if "max_coin_notional_usd" in t:
        caps = t["max_coin_notional_usd"]
        if not isinstance(caps, dict):
            raise ConfigError("policy.max_coin_notional_usd must be a table")
        for c in caps:
            if not COIN.match(c):
                raise ConfigError(f"policy.max_coin_notional_usd: bad coin symbol {c!r}")
        kw["max_coin_notional_usd"] = {c: _num(v, f"policy.max_coin_notional_usd.{c}", 0.0, lo_open=True)
                                       for c, v in caps.items()}
    p = PolicyConfig(**kw)
    if p.max_child_notional_usd > p.max_account_notional_usd:
        raise ConfigError("policy.max_child_notional_usd exceeds max_account_notional_usd")
    return p


# ---------------------------------------------------------------- loading

def loads(text: str, base_dir: Path = Path(".")) -> OMXConfig:
    if contains_key_like(text):
        raise ConfigError("config contains a string that looks like a private key; "
                          "remove it and reference the key by file path or env var name only")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"not valid TOML: {e}") from None
    _walk_names(raw)
    top = {"network", "account_address", "state_dir", "audit_path", "eligibility_ttl_days", "signer", "builder",
           "policy", "agent", "order_expiry_ms", "lock_dir"}
    extra = set(raw) - top
    if extra:
        raise ConfigError(f"unknown top-level key(s): {sorted(extra)}")

    network = raw.get("network")
    if network not in NETWORKS:
        raise ConfigError(f"network: must be one of {NETWORKS}")
    addr = raw.get("account_address")
    if not isinstance(addr, str) or not ADDRESS.match(addr):
        raise ConfigError("account_address: must be a public 0x address (40 hex)")

    s = _table(raw, "signer", {"key_path", "key_env"})
    kp, ke = s.get("key_path"), s.get("key_env")
    if (kp is None) == (ke is None):
        raise ConfigError("[signer]: set exactly one of key_path or key_env")
    if kp is not None and (not isinstance(kp, str) or not kp.strip() or "\n" in kp):
        raise ConfigError("signer.key_path: must be a file path")
    if ke is not None and (not isinstance(ke, str) or not ENV_NAME.match(ke)):
        raise ConfigError("signer.key_env: must be an environment variable NAME like OMX_AGENT_KEY")

    b = _table(raw, "builder", {"address", "fee_tenths_bps"})
    fee = _int(b.get("fee_tenths_bps", 0), "builder.fee_tenths_bps", 0, MAX_BUILDER_FEE_TENTHS_BPS)
    baddr = b.get("address") or None
    if baddr is not None and (not isinstance(baddr, str) or not ADDRESS.match(baddr)):
        raise ConfigError("builder.address: must be a public 0x address (40 hex)")
    if fee > 0 and baddr is None:
        raise ConfigError("builder.fee_tenths_bps > 0 requires builder.address")
    if baddr is not None and baddr.lower() == addr.lower():
        raise ConfigError("builder.address must differ from account_address (and from the agent wallet)")

    policy = _policy(_table(raw, "policy", set(_POLICY_FLOATS) | {
        "max_data_age_ms", "allowed_coins", "allow_agent_sources", "max_coin_notional_usd"}))

    a = _table(raw, "agent", {"enabled", "approval_above_usd", "daily_budget_usd", "allowed_coins", "approval_ttl_s"})
    agent = AgentSettings(
        enabled=_bool(a.get("enabled", False), "agent.enabled"),
        approval_above_usd=_num(a.get("approval_above_usd", 1_000.0), "agent.approval_above_usd"),
        daily_budget_usd=_num(a.get("daily_budget_usd", 5_000.0), "agent.daily_budget_usd"),
        allowed_coins=_coins(a.get("allowed_coins", []), "agent.allowed_coins"),
        approval_ttl_s=_int(a.get("approval_ttl_s", 600), "agent.approval_ttl_s", 30, 86_400),
    )
    if not agent.allowed_coins <= policy.allowed_coins:
        raise ConfigError(f"agent.allowed_coins must be a subset of policy.allowed_coins: "
                          f"{sorted(agent.allowed_coins - policy.allowed_coins)}")

    def _path(v: Any, name: str, default: Path) -> Path:
        if v is None:
            return default
        if not isinstance(v, str) or not v.strip():
            raise ConfigError(f"{name}: must be a path string")
        p = Path(v)
        return p if p.is_absolute() else (base_dir / p)

    state_dir = _path(raw.get("state_dir"), "state_dir", base_dir / "state")
    audit_path = _path(raw.get("audit_path"), "audit_path", state_dir / "audit.jsonl")
    ttl_days = _int(raw.get("eligibility_ttl_days", MAX_ELIGIBILITY_TTL_DAYS), "eligibility_ttl_days",
                    1, MAX_ELIGIBILITY_TTL_DAYS)
    expiry = _int(raw.get("order_expiry_ms", 15_000), "order_expiry_ms", 2_000, 60_000)
    lock_dir = _path(raw.get("lock_dir"), "lock_dir", None)
    return OMXConfig(network, addr, kp, ke, baddr, fee, policy, state_dir, audit_path, agent,
                     eligibility_ttl_days=ttl_days, order_expiry_ms=expiry, lock_dir=lock_dir)


def default_lock_dir() -> Path:
    """User-level dir for per-account locks: $OMX_LOCK_DIR, else %LOCALAPPDATA%/omx/locks (Windows) or
    $XDG_STATE_HOME/omx/locks (default ~/.local/state/omx/locks)."""
    env = os.environ.get("OMX_LOCK_DIR")
    if env:
        return Path(env)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "omx" / "locks"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "omx" / "locks"


def account_lock_path(cfg: OMXConfig) -> Path:
    """One lock file per (network, account): two OMX instances never trade one account at once."""
    return (cfg.lock_dir or default_lock_dir()) / f"{cfg.network}-{cfg.account_address.lower()}.lock"


def eligibility_gate(cfg: OMXConfig, now_s: Optional[float] = None) -> Optional[str]:
    """None when a passing attestation younger than both its own expiry and the configured
    eligibility_ttl_days is on file; otherwise the first reason code."""
    from .eligibility import load_history, problems
    why = problems(load_history(cfg.state_dir), time.time() if now_s is None else now_s,
                   max_age_s=cfg.eligibility_ttl_s)
    return why[0] if why else None


def load(path: str | Path) -> OMXConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    cfg = loads(p.read_text(encoding="utf-8"), base_dir=p.resolve().parent)
    return dataclasses.replace(cfg, source_path=p)


# ---------------------------------------------------------------- writing

def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(account_address: str, network: str = "testnet", key_path: Optional[str] = None,
           key_env: Optional[str] = None, builder_address: Optional[str] = None,
           fee_tenths_bps: int = 0, agent_enabled: bool = False, *, approval_above_usd: float = 1000.0,
           daily_budget_usd: float = 5000.0) -> str:
    """Config text for `omx init` / `omx setup`. Validated by loads() before it is returned."""
    signer = f"key_path = {_q(key_path)}" if key_path is not None else f"key_env = {_q(key_env or '')}"
    builder = (f"address = {_q(builder_address)}\n" if builder_address else "# address = \"0x...\"\n")
    text = f"""# OMX config. Public data and references only: never paste a private key here.
network = {_q(network)}                 # "testnet" first, "mainnet" later
account_address = {_q(account_address)}  # your MAIN account's public address
state_dir = "state"
# audit_path = "state/audit.jsonl"
eligibility_ttl_days = 30            # re-answer the eligibility questions at least this often (1..30)
order_expiry_ms = 15000              # every signed order/cancel expires this long after signing (2000..60000)
# lock_dir = "..."                   # per-account lock dir (default %LOCALAPPDATA%/omx/locks)

[signer]
# The API/agent wallet key is referenced, never embedded. Set exactly one.
{signer}

[builder]
{builder}fee_tenths_bps = {fee_tenths_bps}             # 0 = no builder fee; 10 = 1 bps; max 100 (10 bps)

[policy]
allowed_coins = ["BTC", "ETH", "SOL"]
min_day_ntl_usd = 50000000.0
min_depth_usd = 250000.0
depth_band_bps = 25.0
price_band_bps = 100.0
default_coin_notional_usd = 10000.0
max_account_notional_usd = 25000.0
max_leverage = 3.0
max_daily_loss_usd = 500.0
max_data_age_ms = 3000
max_child_notional_usd = 5000.0
allow_agent_sources = true

[policy.max_coin_notional_usd]
# BTC = 15000.0

[agent]
enabled = {"true" if agent_enabled else "false"}
approval_above_usd = {float(approval_above_usd)!r}       # per-intent notional above this waits for a human
daily_budget_usd = {float(daily_budget_usd)!r}         # agent notional per UTC day; beyond it every intent waits for a human
allowed_coins = ["BTC", "ETH"]    # must be a subset of policy.allowed_coins
approval_ttl_s = 600
"""
    loads(text)
    return text
