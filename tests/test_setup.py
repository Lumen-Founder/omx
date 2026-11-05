"""`omx setup` and `omx mcp-config` (offline: fake /info, fake approval page, throwaway keys in tmp dirs)."""
import base64
import io
import json
import os
import re
import shlex
import subprocess
import tomllib
import urllib.parse
from pathlib import Path

import pytest
from eth_account import Account

from omx import mcp_config
from omx.cli import main
from omx.config import DEFAULT_BUILDER, load, render
from omx.keyfile import KeyFileExists, write_new_key_file
from omx.onboarding import load_eligibility
from omx.setup_wizard import run_setup

ELIGIBLE = ["no", "KR", "yes", "", "no", "yes"]   # entity, residence, here?, citizenship(=KR), vpn, terms


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Inputs:
    """Answers in order; end of the list = end of input (EOFError), like a closed stdin."""
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


class FakeInfo:
    """Public /info answers by request type; maxBuilderFee is what the account approved."""
    def __init__(self, approved=0):
        self.calls, self.urls, self.approved = [], [], approved

    def factory(self, url):
        self.urls.append(url)
        return self

    def __call__(self, path, payload):
        assert path == "/info"
        self.calls.append(payload)
        return {"userRole": {"role": "user"}, "userAbstraction": "disabled",
                "clearinghouseState": {"marginSummary": {"accountValue": "743.877"}},
                "extraAgents": [], "maxBuilderFee": self.approved}[payload["type"]]


def new_key():
    a = Account.create()
    return a, "0x" + bytes(a.key).hex()


def wizard(tmp_path, answers, keys=(), fake=None, serve=None, cfg_name="omx.toml"):
    out, inp, gp, restricted = io.StringIO(), Inputs(answers), Inputs(keys), []
    code = run_setup(tmp_path / cfg_name, inp, gp, out, transport_factory=(fake or FakeInfo()).factory,
                     serve_page=serve, restrict=lambda p: restricted.append(Path(p)), shell="posix")
    return code, out.getvalue(), inp, gp, restricted


def all_text_under(root: Path, skip: Path) -> str:
    parts = []
    for p in root.rglob("*"):
        if p.is_file() and p.resolve() != skip.resolve():
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ---------------------------------------------------------------- the whole walk

def test_full_testnet_walk_hidden_key(tmp_path):
    main_acct = Account.create().address
    agent, key_hex = new_key()
    fake = FakeInfo()
    answers = ["", main_acct, "2", ""] + ELIGIBLE + ["", "", "2500", "y", ""]
    code, out, inp, gp, restricted = wizard(tmp_path, answers, [key_hex], fake)
    assert code == 0, out
    kp = tmp_path / "secrets" / "hl_agent_testnet.key"
    # key file: created, content = the key, restricted (before it got its name), the key never shown
    assert kp.read_text(encoding="ascii").strip() == key_hex
    assert len(restricted) == 1 and restricted[0].parent == kp.parent and restricted[0] != kp
    assert not restricted[0].exists()                  # the temporary name is gone
    assert key_hex not in out and key_hex[2:] not in out and key_hex[2:].upper() not in out
    assert agent.address in out
    assert len(gp.prompts) == 1 and "hidden" in gp.prompts[0]
    assert not any("private key" in p and "hidden" not in p for p in inp.prompts)
    # nothing else on disk carries the key: config, eligibility history, audit log
    assert key_hex[2:] not in all_text_under(tmp_path, kp).lower()
    cfg = load(tmp_path / "omx.toml")
    assert cfg.network == "testnet" and cfg.account_address == main_acct
    assert Path(cfg.signer_key_path) == kp and Path(cfg.signer_key_path).is_absolute()
    assert cfg.agent.enabled and cfg.agent.approval_above_usd == 1000.0 and cfg.agent.daily_budget_usd == 2500.0
    assert cfg.builder_address is None and cfg.builder_fee_tenths_bps == 0
    assert load_eligibility(tmp_path / "state")["eligible"]
    # doctor ran against the fake (read-only /info), next steps printed with the absolute path
    assert fake.urls and all(c["type"] != "order" for c in fake.calls)
    assert "agent address: " + agent.address.lower() in out
    assert "omx --config " + shlex.quote(str(tmp_path / "omx.toml")) + " daemon" in out
    assert "## Claude Code" in out and "OMX_CONFIG=" + str(tmp_path / "omx.toml") in out
    assert "Never type them here" in out and "no withdrawal or transfer code" in out


def test_setup_through_the_cli_with_the_real_permission_helper(tmp_path):
    main_acct = Account.create().address
    _, key_hex = new_key()
    p = tmp_path / "cfg dir" / "omx.toml"
    out, err = io.StringIO(), io.StringIO()
    answers = ["", main_acct, "2", ""] + ELIGIBLE + ["n", "y", "n"]
    code = main(["--config", str(p), "setup", "--shell", "powershell"], input_fn=Inputs(answers),
                getpass_fn=Inputs([key_hex]), out=out, err=err, transport_factory=FakeInfo().factory)
    assert code == 0, out.getvalue() + err.getvalue()
    kp = p.parent / "secrets" / "hl_agent_testnet.key"
    assert kp.read_text(encoding="ascii").strip() == key_hex
    if os.name == "nt":
        acl = subprocess.run(["icacls", str(kp)], capture_output=True, text=True).stdout
        assert "(I)" not in acl                         # no inherited entries left
        assert os.environ["USERNAME"].lower() in acl.lower()
    else:
        assert (kp.stat().st_mode & 0o777) == 0o600
    assert key_hex[2:] not in out.getvalue() + err.getvalue()
    assert "AI agent access is off" in out.getvalue()
    assert "omx --config '" + str(p) + "' daemon" in out.getvalue()       # PowerShell quoting of a path with a space


def test_main_wallet_key_is_refused_before_anything_is_written(tmp_path):
    main_wallet, main_hex = new_key()
    agent, agent_hex = new_key()
    answers = ["", main_wallet.address, "2", ""] + ELIGIBLE + ["n", "y", "n"]
    code, out, *_ = wizard(tmp_path, answers, [main_hex, agent_hex])
    assert code == 0
    assert "REFUSED: this key belongs to your MAIN account" in out
    kp = tmp_path / "secrets" / "hl_agent_testnet.key"
    assert kp.read_text(encoding="ascii").strip() == agent_hex      # only the API wallet key was saved
    assert main_hex[2:] not in out and main_hex[2:] not in all_text_under(tmp_path, kp).lower()


def test_only_main_wallet_keys_stop_the_walk_with_nothing_written(tmp_path):
    main_wallet, main_hex = new_key()
    code, out, *_ = wizard(tmp_path, ["", main_wallet.address, "2", ""], [main_hex] * 3)
    assert code == 1 and out.count("REFUSED") == 3
    assert not (tmp_path / "secrets").exists() and not (tmp_path / "omx.toml").exists()
    assert not (tmp_path / "state").exists()


def test_invalid_hidden_key_is_not_echoed(tmp_path):
    bad = "0x" + "zz" * 32
    code, out, *_ = wizard(tmp_path, ["", Account.create().address, "2", ""], [bad, "not a key", "  "])
    assert code == 1 and "zz" * 8 not in out and "not a key" not in out
    assert out.count("The input was discarded") == 3


def test_existing_main_key_file_is_refused(tmp_path):
    main_wallet, main_hex = new_key()
    f = tmp_path / "main.key"
    f.write_text(main_hex, encoding="utf-8")
    code, out, *_ = wizard(tmp_path, ["", main_wallet.address, "1", str(f)])     # then end of input
    assert code == 1 and "belongs to your MAIN account" in out and main_hex[2:] not in out
    assert not (tmp_path / "omx.toml").exists()


def test_existing_api_key_file_is_used_and_not_changed(tmp_path):
    agent, agent_hex = new_key()
    f = tmp_path / "keys" / "api.key"
    f.parent.mkdir()
    f.write_text(agent_hex + "\n", encoding="utf-8")
    before = f.stat().st_mtime_ns
    answers = ["", Account.create().address, "1", str(f)] + ELIGIBLE + ["n", "y", "n"]
    code, out, _, gp, restricted = wizard(tmp_path, answers)
    assert code == 0 and gp.prompts == [] and restricted == []
    assert load(tmp_path / "omx.toml").signer_key_path == str(f)
    assert f.stat().st_mtime_ns == before and agent.address in out and agent_hex[2:] not in out


def test_existing_key_file_is_never_overwritten(tmp_path):
    kp = tmp_path / "secrets" / "hl_agent_testnet.key"
    kp.parent.mkdir()
    kp.write_text("keep me", encoding="utf-8")
    other = tmp_path / "secrets" / "other.key"
    _, key_hex = new_key()
    answers = ["", Account.create().address, "2", "", str(other)] + ELIGIBLE + ["n", "y", "n"]
    code, out, *_ = wizard(tmp_path, answers, [key_hex])
    assert code == 0 and "already exists; OMX never overwrites a key file" in out
    assert kp.read_text(encoding="utf-8") == "keep me"
    assert other.read_text(encoding="ascii").strip() == key_hex
    # the writer itself refuses too (a file that appeared after the question)
    with pytest.raises(KeyFileExists):
        write_new_key_file(kp, Account.create(), lambda p: None)
    assert kp.read_text(encoding="utf-8") == "keep me"
    assert [p.name for p in kp.parent.iterdir() if ".tmp-" in p.name] == []


@pytest.mark.parametrize("stop", [EOFError(), KeyboardInterrupt()])
def test_stopping_mid_walk_writes_no_key_or_config(tmp_path, stop):
    _, key_hex = new_key()
    answers = ["", Account.create().address, "2", ""] + ELIGIBLE + ["", "", "", stop]   # stop at "write?"
    code, out, *_ = wizard(tmp_path, answers, [key_hex])
    assert code == 1 and "Setup stopped" in out
    assert not (tmp_path / "secrets").exists() and not (tmp_path / "omx.toml").exists()
    assert key_hex[2:] not in out


def test_end_of_input_inside_eligibility_writes_nothing(tmp_path):
    _, key_hex = new_key()
    code, out, *_ = wizard(tmp_path, ["", Account.create().address, "2", "", "no", "KR"], [key_hex])
    assert code == 1
    assert not (tmp_path / "secrets").exists() and not (tmp_path / "omx.toml").exists()
    assert load_eligibility(tmp_path / "state") is None


def test_declining_the_final_confirmation_writes_nothing(tmp_path):
    _, key_hex = new_key()
    code, out, *_ = wizard(tmp_path, ["", Account.create().address, "2", ""] + ELIGIBLE + ["n", "n"], [key_hex])
    assert code == 1 and "Nothing was written" in out
    assert not (tmp_path / "secrets").exists() and not (tmp_path / "omx.toml").exists()


def test_existing_config_needs_its_own_confirmation(tmp_path):
    acct = Account.create().address
    (tmp_path / "omx.toml").write_text(render(acct, "testnet", key_path="x.key"), encoding="utf-8")
    old = (tmp_path / "omx.toml").read_text(encoding="utf-8")
    _, key_hex = new_key()
    code, out, inp, *_ = wizard(tmp_path, ["", acct, "2", ""] + ELIGIBLE + ["n", "n"], [key_hex])
    assert code == 1 and "exists. Replace it?" in inp.prompts[-1] and "Nothing was written" in out
    assert (tmp_path / "omx.toml").read_text(encoding="utf-8") == old and not (tmp_path / "secrets").exists()


def test_key_pasted_into_a_normal_prompt_is_discarded(tmp_path):
    _, key_hex = new_key()
    acct = Account.create().address
    code, out, *_ = wizard(tmp_path, ["", key_hex, acct])      # pasted at the address prompt; then end of input
    assert code == 1 and "looks like a private key. It was discarded" in out and key_hex[2:] not in out


def test_ineligible_answer_stops_with_code_2(tmp_path):
    _, key_hex = new_key()
    answers = ["", Account.create().address, "2", ""] + ["no", "US", "yes", "", "no", "yes"]
    code, out, *_ = wizard(tmp_path, answers, [key_hex])
    assert code == 2 and not (tmp_path / "omx.toml").exists() and not (tmp_path / "secrets").exists()


def test_mainnet_builder_choice_and_browser_approval(tmp_path):
    _, key_hex = new_key()
    served = []

    def serve(cfg, **kw):
        served.append((cfg, kw))
        return 0
    fake = FakeInfo(approved=0)
    answers = (["mainnet", "y", Account.create().address, "2", ""] + ELIGIBLE + ["n"]
               + ["", "1"]                       # Enter is not an answer here: the user must choose
               + ["y", "", "y"])                  # write, doctor, open the approval page
    code, out, *_ = wizard(tmp_path, answers, [key_hex], fake, serve)
    assert code == 0, out
    assert "there is no Enter default here" in out and "Builder fee: 1 bps (0.01%)" in out
    cfg = load(tmp_path / "omx.toml")
    assert cfg.network == "mainnet" and cfg.builder_address == DEFAULT_BUILDER["mainnet"]
    assert cfg.builder_fee_tenths_bps == 10 and not cfg.agent.enabled
    assert fake.urls == ["https://api.hyperliquid.xyz"]
    assert "Not approved yet." in out and len(served) == 1
    assert served[0][0].builder_address == DEFAULT_BUILDER["mainnet"] and served[0][1]["revoke"] is False
    assert "secrets" + os.sep + "hl_agent_mainnet.key" in out


@pytest.mark.parametrize("choice,fee,builder", [("2", 0, None), ("3", 5, DEFAULT_BUILDER["mainnet"])])
def test_mainnet_no_builder_or_lower_rate(tmp_path, choice, fee, builder):
    _, key_hex = new_key()
    extra = ["0.5"] if choice == "3" else []
    answers = ["mainnet", "y", Account.create().address, "2", ""] + ELIGIBLE + ["n", choice] + extra + ["y", "n"] + (["n"] if builder else [])
    code, out, *_ = wizard(tmp_path, answers, [key_hex])
    assert code == 0, out
    cfg = load(tmp_path / "omx.toml")
    assert cfg.builder_fee_tenths_bps == fee and cfg.builder_address == builder
    assert ("Approval state unknown" in out) == bool(builder)


def test_declining_mainnet_falls_back_to_testnet(tmp_path):
    _, key_hex = new_key()
    answers = ["mainnet", "n", Account.create().address, "2", ""] + ELIGIBLE + ["n", "y", "n"]
    code, out, *_ = wizard(tmp_path, answers, [key_hex])
    assert code == 0 and load(tmp_path / "omx.toml").network == "testnet"


# ---------------------------------------------------------------- omx mcp-config

@pytest.fixture
def cfg_odd_path(tmp_path):
    """A config under a directory with a space and an apostrophe (Windows: backslashes too)."""
    d = tmp_path / "my dir's"
    d.mkdir()
    p = d / "omx.toml"
    p.write_text(render(Account.create().address, "testnet", key_path="k.key", agent_enabled=True),
                 encoding="utf-8")
    return p


def mc(args, cfg=None):
    out, err = io.StringIO(), io.StringIO()
    argv = (["--config", str(cfg)] if cfg else []) + ["mcp-config"] + args
    return main(argv, out=out, err=err), out.getvalue(), err.getvalue()


def ps_split(line):
    """Split a PowerShell command line made of bare words and '...' literals ('' = one quote)."""
    toks, i = [], 0
    while i < len(line):
        if line[i] == " ":
            i += 1
            continue
        if line[i] == "'":
            j, buf = i + 1, []
            while True:
                if line[j] == "'" and j + 1 < len(line) and line[j + 1] == "'":
                    buf.append("'")
                    j += 2
                elif line[j] == "'":
                    break
                else:
                    buf.append(line[j])
                    j += 1
            toks.append("".join(buf))
            i = j + 1
        else:
            j = line.find(" ", i)
            j = len(line) if j < 0 else j
            toks.append(line[i:j])
            i = j
    return toks


def body(text):
    return "\n".join(l for l in text.splitlines() if not l.startswith("#"))


@pytest.mark.parametrize("shell,split", [("posix", shlex.split), ("powershell", ps_split)])
def test_claude_codex_gemini_command_lines_round_trip(cfg_odd_path, shell, split):
    abs_ = os.path.abspath(cfg_odd_path)
    code, out, _ = mc(["--client", "claude-code", "--shell", shell], cfg_odd_path)
    assert code == 0
    assert split(out.splitlines()[0]) == ["claude", "mcp", "add", "--env", f"OMX_CONFIG={abs_}", "--transport",
                                          "stdio", "omx", "--", "uvx", "omx-hyperliquid"]
    if shell == "powershell":
        assert " '--' " in out.splitlines()[0]          # a bare -- would be dropped by a .ps1 shim
    code, out, _ = mc(["--client", "codex", "--shell", shell], cfg_odd_path)
    assert split(out.splitlines()[0]) == ["codex", "mcp", "add", "omx", "--env", f"OMX_CONFIG={abs_}", "--", "uvx",
                                          "omx-hyperliquid"]
    toml = tomllib.loads(body("\n".join(out.splitlines()[1:])))
    assert toml["mcp_servers"]["omx"] == {"command": "uvx", "args": ["omx-hyperliquid"], "startup_timeout_sec": 60,
                                          "env": {"OMX_CONFIG": abs_}}
    code, out, _ = mc(["--client", "gemini", "--shell", shell], cfg_odd_path)
    assert split(out.strip()) == ["gemini", "mcp", "add", "-e", f"OMX_CONFIG={abs_}", "omx", "uvx",
                                  "omx-hyperliquid"]


def test_json_snippets_escape_the_path(cfg_odd_path):
    abs_ = os.path.abspath(cfg_odd_path)
    entry = {"command": "uvx", "args": ["omx-hyperliquid"], "env": {"OMX_CONFIG": abs_}}
    code, out, _ = mc(["--client", "claude-desktop"], cfg_odd_path)
    assert code == 0 and json.loads(body(out)) == {"mcpServers": {"omx": entry}}
    if os.name == "nt":
        assert abs_.replace("\\", "\\\\") in out           # backslashes doubled inside JSON
    code, out, _ = mc(["--client", "json"], cfg_odd_path)
    assert json.loads(out) == {"mcpServers": {"omx": {"command": "uvx", "args": ["omx-hyperliquid", "--config",
                                                                                     abs_]}}}
    code, out, _ = mc(["--client", "vscode", "--shell", "posix"], cfg_odd_path)
    argv = shlex.split(out.splitlines()[0])
    assert argv[:2] == ["code", "--add-mcp"]
    assert json.loads(argv[2]) == {"name": "omx", "type": "stdio", **entry}
    assert json.loads(body("\n".join(out.splitlines()[1:]))) == {"servers": {"omx": {"type": "stdio", **entry}}}
    code, out, _ = mc(["--client", "vscode", "--shell", "powershell"], cfg_odd_path)
    assert "code --add-mcp" not in body(out)
    assert json.loads(body(out)) == {"servers": {"omx": {"type": "stdio", **entry}}}


def test_cursor_install_links(cfg_odd_path):
    abs_ = os.path.abspath(cfg_odd_path)
    code, out, _ = mc(["--client", "cursor"], cfg_odd_path)
    links = [l for l in out.splitlines() if not l.startswith("#")]
    assert code == 0 and len(links) == 2
    assert links[0].startswith("cursor://anysphere.cursor-deeplink/mcp/install?")
    assert links[1].startswith("https://cursor.com/en/install-mcp?")
    for link in links:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        assert q["name"] == ["omx"]
        cfg = json.loads(base64.b64decode(q["config"][0]))
        assert cfg == {"command": "uvx", "args": ["omx-hyperliquid"], "env": {"OMX_CONFIG": abs_}}
        assert re.fullmatch(r"[A-Za-z0-9%._~&=:/?-]+", link)    # '+', '/' and '=' of base64 are percent-encoded


def test_all_clients_and_relative_config_becomes_absolute(cfg_odd_path, monkeypatch):
    monkeypatch.chdir(cfg_odd_path.parent)
    code, out, err = mc(["--shell", "posix"], Path("omx.toml"))
    assert code == 0 and err == ""
    for title in mcp_config.TITLES.values():
        assert f"## {title}" in out
    assert str(cfg_odd_path.resolve()) in out or os.path.abspath(cfg_odd_path) in out


def test_env_var_config_is_used(cfg_odd_path, monkeypatch):
    monkeypatch.setenv("OMX_CONFIG", str(cfg_odd_path))
    code, out, _ = mc(["--client", "json"])
    assert code == 0 and json.loads(out)["mcpServers"]["omx"]["args"][-1] == os.path.abspath(cfg_odd_path)


def test_no_config_exits_2_and_points_to_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OMX_CONFIG", raising=False)
    code, out, err = mc([], tmp_path / "missing.toml")
    assert code == 2 and out == "" and "omx setup" in err


def test_broken_config_exits_2(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text("network = 'moon'\n", encoding="utf-8")
    code, out, err = mc([], p)
    assert code == 2 and out == "" and "config error" in err


def test_agent_disabled_is_noted(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text(render(Account.create().address, "testnet", key_path="k.key"), encoding="utf-8")
    code, out, err = mc(["--client", "json"], p)
    assert code == 0 and "enabled = false" in err and json.loads(out)
