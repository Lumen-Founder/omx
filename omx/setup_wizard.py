"""`omx setup`: one guided walk from nothing to a checked config, reusing the existing pieces.

Steps: what it does / does not do -> network -> MAIN account public address -> API (agent) wallet key
(an existing key file, or the key typed into a hidden prompt and saved to a user-only file) ->
eligibility (omx.onboarding.run_eligibility, as `omx eligibility`) -> AI agent limits -> builder fee
(the `omx init` choices and disclosure) -> write the config (omx.config.render) -> read-only checks
(omx.doctor) -> optional builder approval in the browser (the `omx approve-builder` flow) -> next steps
(`omx daemon`, then `omx mcp-config`).

Key handling: the MAIN wallet key or seed phrase is never asked for. A key typed into the hidden prompt
is held only as an eth_account object (inside _PendingKey, which hides it from repr and refuses pickling)
until the final confirmation, then written once by omx.keyfile.write_new_key_file (user-only, never over
an existing file). It is never printed, logged, put in the config or the audit log, or put in an error
message; only the address derived from it is shown. A key whose address equals the MAIN account
address is refused before anything is written. Any answer to a normal (echoed) prompt that looks like
a private key is discarded.

Nothing is written before the final confirmation, except what `omx eligibility` itself records (each
eligibility attempt is kept, blocked ones included). Ctrl+C or end of input stops the walk with nothing
else written.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, TextIO

from . import config as config_mod
from . import ipc, onboarding
from .audit import AuditLog
from .config import ADDRESS, ConfigError, contains_key_like, load, loads, render
from .keyfile import KeyFileExists, restrict_to_user, write_new_key_file

InputFn = Callable[[str], str]
API_PAGE = {"testnet": "https://app.hyperliquid-testnet.xyz/API", "mainnet": "https://app.hyperliquid.xyz/API"}
TRIES = 3

INTRO = """\
OMX setup
=========
This walks you through: network, your MAIN account's public address, an API (agent) wallet key for this
machine, the eligibility questions, AI agent limits, the builder fee, the config file, then read-only
checks (`omx doctor`).

What it does NOT do:
  - It never asks for your MAIN wallet's private key or seed phrase. Never type them here, in a config,
    in a terminal command, or to an AI agent.
  - The API wallet key stays on this machine, in a file only your user can read. It is never printed,
    logged or put in the config (the config holds only the file's path).
  - OMX has no withdrawal or transfer code. An API wallet can trade but cannot withdraw.
  - Nothing is written until you confirm at the end (the eligibility answers are recorded when given,
    as `omx eligibility` does). Ctrl+C stops at any prompt.
"""


class SetupAborted(RuntimeError):
    pass


class _PendingKey:
    """An API wallet key typed in this session, not yet written. Hidden from repr; cannot be pickled."""
    __slots__ = ("_account",)

    def __init__(self, account: Any):
        self._account = account

    @property
    def address(self) -> str:
        return self._account.address

    def write(self, path: Path, restrict) -> Optional[str]:
        return write_new_key_file(path, self._account, restrict)

    def __repr__(self) -> str:
        return f"_PendingKey(address={self.address})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("_PendingKey cannot be pickled")


class Wizard:
    def __init__(self, cfg_path: Path, input_fn: InputFn, getpass_fn: InputFn, out: TextIO,
                 transport_factory=None, serve_page=None, restrict=restrict_to_user, shell: Optional[str] = None):
        self.cfg_path = Path(os.path.abspath(cfg_path))
        self.raw_input, self.getpass_fn, self.out = input_fn, getpass_fn, out
        self.transport_factory, self.serve_page, self.restrict, self.shell = transport_factory, serve_page, \
            restrict, shell

    # ------------------------------------------------------------ prompts

    def w(self, s: str = "") -> None:
        self.out.write(s + "\n")

    def input(self, prompt: str) -> str:
        """input_fn, but an answer that looks like a private key is discarded (never echoed back)."""
        v = self.raw_input(prompt)
        if contains_key_like(v):
            self.w("  That looks like a private key. It was discarded and not stored. Only the hidden key "
                   "prompt in the API wallet step takes a key, and never your MAIN wallet's key.")
            return ""
        return v

    def ask(self, prompt: str, parse: Callable[[str], Any], default: Optional[str] = None) -> Any:
        """parse(answer) returns the value or raises ValueError(message). TRIES attempts."""
        for _ in range(TRIES):
            v = self.input(prompt).strip()
            if v == "" and default is not None:
                v = default
            try:
                return parse(v)
            except ValueError as e:
                self.w(f"  {e}")
        raise SetupAborted("no valid answer")

    def yes(self, prompt: str, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"

        def parse(v: str) -> bool:
            v = v.lower()
            if v == "":
                return default
            if v in ("y", "yes"):
                return True
            if v in ("n", "no"):
                return False
            raise ValueError("please answer y or n")
        return self.ask(f"{prompt} {hint} ", parse)

    # ------------------------------------------------------------ steps

    def run(self) -> int:
        self.w(INTRO)
        existing = None
        if self.cfg_path.exists():
            self.w(f"Config file: {self.cfg_path} (exists; you will be asked before it is replaced)")
            try:
                existing = load(self.cfg_path)
            except ConfigError:
                existing = None
            if existing is not None and ipc.daemon_alive(existing.state_dir):
                self.w("`omx daemon` is running with this config. Stop it first, then run `omx setup` again.")
                return 2
        else:
            self.w(f"Config file: {self.cfg_path} (use `omx --config <path> setup` for another place)")
        self.w()

        network = self.step_network()
        account = self.step_account()
        key_path, agent_addr, pending = self.step_agent_key(network, account)
        base = loads(render(account, network, str(key_path)), base_dir=self.cfg_path.parent)
        if ipc.daemon_alive(base.state_dir):
            self.w(f"`omx daemon` is running on {base.state_dir}. Stop it first, then run `omx setup` again.")
            return 2
        if not self.step_eligibility(base):
            return 2
        agent_on, above, budget = self.step_agent()
        builder, fee = self.step_builder(network, account, agent_addr, str(key_path))
        text = render(account, network, str(key_path), None, builder, fee, agent_on,
                      approval_above_usd=above, daily_budget_usd=budget)
        loads(text, base_dir=self.cfg_path.parent)
        if not self.step_write(text, network, account, key_path, agent_addr, pending, agent_on, above, budget,
                               builder, fee):
            return 1
        doctor_res = self.step_doctor()
        if builder:
            self.step_approve_builder(doctor_res)
        self.step_next(agent_on)
        return 0

    def step_network(self) -> str:
        self.w("Step 1/9  Network")

        def parse(v: str) -> str:
            v = v.lower()
            if v in config_mod.NETWORKS:
                return v
            raise ValueError("type testnet or mainnet")
        net = self.ask("  Network, testnet or mainnet (Enter = testnet): ", parse, default="testnet")
        if net == "mainnet":
            self.w("  MAINNET trades real funds. Start on testnet unless you have already run OMX there.")
            if not self.yes("  Use MAINNET?", default=False):
                self.w("  Using testnet.")
                net = "testnet"
        self.w()
        return net

    def step_account(self) -> str:
        self.w("Step 2/9  MAIN account public address")
        self.w("  The address you log in to Hyperliquid with (it holds the funds). Public address only.")

        def parse(v: str) -> str:
            if ADDRESS.match(v):
                return v
            raise ValueError("not a public address: 0x followed by 40 hex characters")
        acct = self.ask("  MAIN account address (0x + 40 hex): ", parse)
        self.w()
        return acct

    def step_agent_key(self, network: str, account: str) -> tuple[Path, str, Optional[_PendingKey]]:
        self.w("Step 3/9  API (agent) wallet")
        self.w(f"  Create one at {API_PAGE[network]} : connect your MAIN wallet, generate an API wallet, "
               "authorize it, and copy its private key. It can trade but cannot withdraw.")
        self.w("  1) I saved the API wallet key in a file: enter the file's path")
        self.w("  2) Enter the API wallet key now (hidden input); OMX saves it in a file only your user can read")

        def choice(v: str) -> str:
            if v in ("1", "2"):
                return v
            raise ValueError("type 1 or 2")
        if self.ask("  Choose 1 or 2: ", choice) == "1":
            path, addr = self._existing_key_file(network, account)
            pending = None
        else:
            path = self._new_key_path(network)
            pending = self._hidden_key(network, account)
            addr = pending.address
        self.w(f"  API wallet address: {addr} (derived from the key; the key itself is not shown)")
        self.w("  It should match the address listed on the Hyperliquid API page. `omx doctor` checks it.")
        self.w()
        return path, addr, pending

    def _existing_key_file(self, network: str, account: str) -> tuple[Path, str]:
        from .venue.hyperliquid import LocalKeySigner

        def parse(v: str) -> tuple[Path, str]:
            if not v:
                raise ValueError("enter the path of the key file")
            p = Path(os.path.abspath(os.path.expanduser(v.strip('"').strip("'"))))
            if not p.is_file():
                raise ValueError(f"no file at {p}")
            try:
                addr = LocalKeySigner.from_key_file(p).address
            except (ValueError, OSError, UnicodeDecodeError):
                raise ValueError("that file does not hold one hex private key (its content is not shown)") from None
            if addr.lower() == account.lower():
                raise ValueError(f"that key belongs to your MAIN account {account}: OMX never uses the main key. "
                                 f"Create an API wallet at {API_PAGE[network]} and use ITS key.")
            return p, addr
        return self.ask("  Path of the API wallet key file: ", parse)

    def _new_key_path(self, network: str) -> Path:
        default = self.cfg_path.parent / "secrets" / f"hl_agent_{network}.key"

        def parse(v: str) -> Path:
            p = Path(os.path.abspath(os.path.expanduser(v.strip('"').strip("'"))))
            if p.exists():
                raise ValueError(f"{p} already exists; OMX never overwrites a key file. Choose another path, "
                                 "or option 1 to use that file.")
            return p
        return self.ask(f"  Key file to create (Enter = {default}): ", parse, default=str(default))

    def _hidden_key(self, network: str, account: str) -> _PendingKey:
        from eth_account import Account
        self.w("  Paste the API wallet's private key. Input is hidden; nothing is written until the final step.")
        for _ in range(TRIES):
            text = self.getpass_fn("  API wallet private key (hidden): ")
            try:
                acct = Account.from_key((text or "").strip())
            except Exception:
                acct = None
            finally:
                text = None
            if acct is None:
                self.w("  That is not one hex private key (0x + 64 hex). The input was discarded.")
                continue
            if acct.address.lower() == account.lower():
                self.w(f"  REFUSED: this key belongs to your MAIN account {account}. OMX never uses the main "
                       f"wallet key; it was discarded and nothing was saved. Create an API wallet at "
                       f"{API_PAGE[network]} and enter ITS key.")
                acct = None
                continue
            return _PendingKey(acct)
        raise SetupAborted("no valid API wallet key")

    def step_eligibility(self, cfg) -> bool:
        self.w("Step 4/9  Eligibility")
        if onboarding.eligibility_problem(cfg.state_dir) is None:
            self.w("  A valid eligibility answer is already on file for this state directory.")
            if not self.yes("  Answer the questions again?", default=False):
                self.w()
                return True
        ok, _ = onboarding.run_eligibility(cfg.state_dir, self.input, self.out, audit=AuditLog(cfg.audit_path),
                                           ttl_s=cfg.eligibility_ttl_s)
        self.w()
        return ok

    def step_agent(self) -> tuple[bool, float, float]:
        self.w("Step 5/9  AI agent access (MCP)")
        on = self.yes("  Let an AI agent preview and submit orders within your limits?", default=True)
        above, budget = 1000.0, 5000.0
        if on:
            def usd(v: str) -> float:
                try:
                    x = float(v.replace(",", "").replace("_", ""))
                except ValueError:
                    raise ValueError("type a number of USD, or press Enter") from None
                if not math.isfinite(x) or x < 0:
                    raise ValueError("must be a number >= 0")
                return x
            above = self.ask("  Agent intents above this notional wait for your approval, USD (Enter = 1000): ",
                             usd, default="1000")
            budget = self.ask("  Agent notional per UTC day before every intent waits for you, USD "
                              "(Enter = 5000): ", usd, default="5000")
        self.w()
        return on, above, budget

    def step_builder(self, network: str, account: str, agent_addr: str, key_path: str) -> tuple[Optional[str], int]:
        from .cli import _init_builder
        self.w("Step 6/9  Builder fee")
        default = config_mod.DEFAULT_BUILDER.get(network)
        usable = default is not None and default.lower() not in (account.lower(), agent_addr.lower())
        if not usable:
            builder, fee = _init_builder(SimpleNamespace(no_builder=True, builder_address=None, fee_tenths_bps=None,
                                                         network=network, account=account))
            why = "testnet has no OMX builder" if default is None else "the OMX builder is this account"
            self.w(f"  ({why})")
            self.w("  " + onboarding.fee_disclosure(self._cfg(account, network, key_path, builder, fee))
                   .replace("\n", "\n  "))
            self.w()
            return builder, fee
        d_builder, d_fee = _init_builder(SimpleNamespace(no_builder=False, builder_address=None, fee_tenths_bps=None,
                                                         network=network, account=account))
        self.w("  " + onboarding.fee_disclosure(self._cfg(account, network, key_path, d_builder, d_fee))
               .replace("\n", "\n  "))
        self.w(f"  1) OMX builder fee {d_fee / 10:g} bp ({onboarding.max_fee_rate_str(d_fee)}), the `omx init` "
               "default")
        self.w("  2) No builder fee (same as `omx init --no-builder`): OMX charges nothing on top of Hyperliquid")
        self.w("  3) A lower OMX builder fee: 0.1 to 0.9 bp")

        def choice(v: str) -> str:
            if v in ("1", "2", "3"):
                return v
            raise ValueError("type 1, 2 or 3 (there is no Enter default here: choose explicitly)")
        c = self.ask("  Choose 1, 2 or 3: ", choice)
        tenths = None
        if c == "3":
            def rate(v: str) -> int:
                try:
                    x = float(v)
                except ValueError:
                    raise ValueError("type a rate in bp, e.g. 0.5") from None
                t = round(x * 10)
                if not math.isfinite(x) or abs(x * 10 - t) > 1e-9 or not 1 <= t < d_fee:
                    raise ValueError(f"must be 0.1 to {(d_fee - 1) / 10:g} bp in steps of 0.1")
                return t
            tenths = self.ask("  Builder fee in bp (0.1 to 0.9): ", rate)
        builder, fee = _init_builder(SimpleNamespace(no_builder=(c == "2"), builder_address=None,
                                                     fee_tenths_bps=tenths, network=network, account=account))
        if c != "1":
            self.w("  " + onboarding.fee_disclosure(self._cfg(account, network, key_path, builder, fee))
                   .replace("\n", "\n  "))
        self.w()
        return builder, fee

    def _cfg(self, account, network, key_path, builder, fee):
        return loads(render(account, network, key_path, None, builder, fee), base_dir=self.cfg_path.parent)

    def step_write(self, text, network, account, key_path, agent_addr, pending, agent_on, above, budget,
                   builder, fee) -> bool:
        self.w("Step 7/9  Write the files")
        self.w(f"  network           {network}{'  (MAINNET: real funds)' if network == 'mainnet' else ''}")
        self.w(f"  main account      {account}")
        self.w(f"  API wallet        {agent_addr}")
        self.w(f"  key file          {key_path}  ({'NEW, user-only' if pending else 'existing, not changed'})")
        self.w(f"  AI agent          {'on' if agent_on else 'off'}"
               + (f", approval above {above:,.2f} USD, daily budget {budget:,.2f} USD" if agent_on else ""))
        self.w(f"  builder fee       {f'{fee / 10:g} bp to {builder}' if builder and fee else 'none'}")
        self.w(f"  config            {self.cfg_path}")
        if self.cfg_path.exists() and not self.yes(f"  {self.cfg_path} exists. Replace it?", default=False):
            self.w("  Stopped. Nothing was written.")
            return False
        if not self.yes("  Write these files now?", default=False):
            self.w("  Stopped. Nothing was written.")
            return False
        if pending is not None:
            try:
                warn = pending.write(key_path, self.restrict)
            except KeyFileExists as e:
                self.w(f"  {e}. Nothing was written.")
                return False
            self.w(f"  wrote key file {key_path} (user-only)")
            if warn:
                self.w(f"  WARNING: could not restrict it to your user ({warn}); do that yourself now.")
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg_path.with_name(f".{self.cfg_path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.cfg_path)
        finally:
            if tmp.exists():
                tmp.unlink()
        self.w(f"  wrote config {self.cfg_path}")
        self.w()
        return True

    def step_doctor(self) -> Optional[list]:
        from . import doctor
        self.w("Step 8/9  Checks (`omx doctor`)")
        if not self.yes("  Run the read-only checks now? They read public account data from Hyperliquid /info; "
                        "nothing is signed or sent.", default=True):
            self.w("  Skipped. Run `omx --config ... doctor` later.")
            self.w()
            return None
        res = doctor.checks(self.cfg_path, self.transport_factory)
        doctor.report(res, self.out)
        self.w("  (`daemon running` fails until you start the daemon, see the next steps.)")
        self.w()
        return res

    def step_approve_builder(self, doctor_res: Optional[list]) -> None:
        from .cli import _approve_builder
        st = None if doctor_res is None else next((c.status for c in doctor_res if c.name == "builder approval"), None)
        if st == "OK":
            return
        self.w("Builder fee approval")
        self.w("  " + ("Not approved yet." if st == "FAIL" else "Approval state unknown (checks not run)."))
        self.w("  OMX refuses new orders until your MAIN wallet approves the builder fee. You sign it yourself "
               "in your browser wallet; OMX never sees a key or the signature.")
        if self.yes("  Open the approval page in your browser now?", default=False):
            from .cli import CLIError
            try:
                _approve_builder(load(self.cfg_path), self.out, self.serve_page)
            except CLIError as e:
                self.w(f"  Could not open the approval page: {e}. Later: omx --config {self.cfg_path} "
                       "approve-builder")
        else:
            self.w(f"  Later: omx --config {self.cfg_path} approve-builder")
        self.w()

    def step_next(self, agent_on: bool) -> None:
        from .mcp_config import command_line, default_shell, render_clients
        shell = self.shell or default_shell()
        self.w("Step 9/9  Next steps")
        self.w("  1. In a SEPARATE terminal, start the daemon and keep it running while you trade:")
        self.w("       " + command_line(["omx", "--config", str(self.cfg_path), "daemon"], shell))
        if not agent_on:
            self.w("  2. AI agent access is off, so no MCP client is needed. Use `omx preview` / `omx submit`.")
            return
        self.w("  2. Register the MCP server with your AI client (`omx mcp-config` prints this again):")
        self.w()
        self.out.write(render_clients(str(self.cfg_path), "all", shell))


def run_setup(cfg_path: Path, input_fn: InputFn, getpass_fn: InputFn, out: TextIO, transport_factory=None,
              serve_page=None, restrict=restrict_to_user, shell: Optional[str] = None) -> int:
    """0 done; 1 stopped (Ctrl+C, end of input, declined, no valid answer); 2 not eligible / refused."""
    wiz = Wizard(cfg_path, input_fn, getpass_fn, out, transport_factory, serve_page, restrict, shell)
    try:
        return wiz.run()
    except (KeyboardInterrupt, EOFError):
        out.write("\nSetup stopped. Nothing more was written.\n")
        return 1
    except (SetupAborted, onboarding.NotEligible) as e:
        out.write(f"\nSetup stopped ({e}). Nothing more was written.\n")
        return 1
