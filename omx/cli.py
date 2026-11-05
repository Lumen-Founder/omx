"""OMX command line (python -m omx.cli).

The CLI never prints a private key and refuses any argument that looks like
one. Keys are referenced by path or env var name in the config only.

Process model (fix contract R1, item 8): `omx daemon` owns the one engine.
preview / submit / status / cancel / approvals approve are requests to it over
the file IPC; without a live daemon (heartbeat older than 5 s) they fail and say
so. `omx kill` always writes the KILL file first (with its scope), then asks the
daemon to cancel, or, with no live daemon, builds a one-shot cancel-only engine
(purpose "kill": no eligibility gate) and calls kill(scope) itself.

Human-privileged requests to the daemon (approvals approve/reject, unkill,
eligibility, audit repair) must also be confirmed by typing the line the daemon
prints in its own console (R2/B2). Run locally (no daemon), the same commands
need an interactive terminal when the built-in input() is used.

`omx approve-builder` serves one page on 127.0.0.1 where the user's own browser
wallet signs ApproveBuilderFee and sends it to Hyperliquid (omx.approve_page);
this process never receives a key or a signature. `omx doctor` runs read-only
setup checks (omx.doctor); it is the only command that reads /info by itself.
"""
from __future__ import annotations

import argparse
import builtins
import dataclasses
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from . import ipc, onboarding
from .approvals import ApprovalError, ApprovalQueue, make_intent
from .audit import TRUNCATED_TAIL, AuditLog
from .config import ConfigError, OMXConfig, contains_key_like, eligibility_gate, load, loads, render
from .engine_api import build_engine, kill_engine
from .killswitch import CONFIRM_PHRASE, KillSwitch, kill_from_outside
from .tty import is_console
from .types import Algo

UNKILL_PHRASE = CONFIRM_PHRASE
REPAIR_PHRASE = "REPAIR AUDIT"
TIMEOUT_S = 15.0
CONFIRM_TIMEOUT_S = 180.0          # time to walk to the daemon console and type the confirmation
CONSOLE_NOTE = ("Waiting for confirmation in the `omx daemon` console: it shows this request and the exact "
                "line to type (up to {:g} s).\n")
ORDER_FIELDS = ("coin", "side", "size", "algo", "limit_px", "duration_s", "slices", "pov_rate", "post_only",
                "reduce_only", "max_slippage_bps", "note")


class CLIError(RuntimeError):
    pass


def default_engine_factory(cfg: OMXConfig, purpose: str = "trade"):
    """omx.engine.from_config(cfg, purpose=...). purpose "kill" builds a cancel-only engine without
    the eligibility gate (cancelling must work for an expired or blocked user)."""
    try:
        from . import engine as eng
    except ImportError as e:
        raise CLIError(f"omx.engine is not available: {e}") from None
    fn = getattr(eng, "from_config", None)
    if fn is None:
        raise CLIError("omx.engine has no from_config(cfg)")
    return fn(cfg, purpose=purpose)


def config_path(arg: Optional[str]) -> Path:
    return Path(arg or os.environ.get("OMX_CONFIG") or "omx.toml")


def load_config_or_exit(arg: Optional[str], err: TextIO) -> Optional[OMXConfig]:
    try:
        return load(config_path(arg))
    except ConfigError as e:
        err.write(f"config error: {e}\n")
        return None


def _dump(out: TextIO, obj: Any) -> None:
    if dataclasses.is_dataclass(obj):
        obj = dataclasses.asdict(obj)
    out.write(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="omx", description="OMX non-custodial Hyperliquid execution")
    ap.add_argument("--config", default=None, help="config file (default: $OMX_CONFIG or ./omx.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write a config and run onboarding")
    p.add_argument("--account", required=True, help="MAIN account public address (0x + 40 hex)")
    p.add_argument("--network", choices=["testnet", "mainnet"], default="testnet")
    k = p.add_mutually_exclusive_group(required=True)
    k.add_argument("--key-path", help="PATH of the agent wallet key file (the key itself is never passed)")
    k.add_argument("--key-env", help="NAME of the env var holding the agent wallet key")
    b = p.add_mutually_exclusive_group()
    b.add_argument("--builder-address", default=None, help="builder code address (default: the OMX default for "
                                                            "the network, if any)")
    b.add_argument("--no-builder", action="store_true", help="send no builder code (fee 0)")
    p.add_argument("--fee-tenths-bps", type=int, default=None,
                   help="builder fee in 0.1 bps (default 10 = 1 bps with the OMX default builder, else 0)")
    p.add_argument("--enable-agent", action="store_true")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("eligibility", help="answer the eligibility questions")
    p = sub.add_parser("daemon", help="run the OMX daemon (owns the engine); keep it running while trading")
    p.add_argument("--cancel-only", action="store_true",
                   help="start without the eligibility check; only cancel resting OMX orders, refuse new intents")

    for name in ("preview", "submit"):
        p = sub.add_parser(name)
        p.add_argument("coin")
        p.add_argument("side", choices=["buy", "sell"])
        p.add_argument("size", type=float)
        p.add_argument("--algo", choices=[a.value for a in Algo], default="twap")
        p.add_argument("--limit-px", type=float, default=None)
        p.add_argument("--duration-s", type=int, default=0)
        p.add_argument("--slices", type=int, default=1)
        p.add_argument("--pov-rate", type=float, default=0.0)
        p.add_argument("--post-only", action="store_true")
        p.add_argument("--reduce-only", action="store_true")
        p.add_argument("--max-slippage-bps", type=float, default=50.0)
        p.add_argument("--note", default="")
        if name == "submit":
            p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
            p.add_argument("--intent-id", default=None,
                           help="reuse an id (retry after a timeout); the daemon refuses an id it already took")

    for name in ("status", "cancel"):
        sub.add_parser(name).add_argument("intent_id")
    p = sub.add_parser("kill", help="stop new orders and cancel resting OMX orders (works without a daemon)")
    p.add_argument("--reason", default="manual kill from CLI")
    p.add_argument("--all", action="store_true", help="cancel every open order on the account, not only OMX ones")
    sub.add_parser("unkill", help="clear the kill switch (asks you to type I UNDERSTAND)")

    p = sub.add_parser("approvals")
    asub = p.add_subparsers(dest="acmd", required=True)
    asub.add_parser("list").add_argument("--all", action="store_true")
    a = asub.add_parser("approve")
    a.add_argument("approval_id")
    a.add_argument("--yes", action="store_true")
    a = asub.add_parser("reject")
    a.add_argument("approval_id")
    a.add_argument("--note", default="")

    p = sub.add_parser("audit")
    asub = p.add_subparsers(dest="acmd", required=True)
    asub.add_parser("verify").add_argument("--path", default=None)
    asub.add_parser("repair", help="move a torn last line aside (asks you to type REPAIR AUDIT)").add_argument(
        "--path", default=None)

    sub.add_parser("fees", help="show the builder fee in plain words")
    p = sub.add_parser("approve-builder", help="approve (or revoke) the builder fee with your MAIN wallet in "
                                               "your browser; OMX never sees a key")
    p.add_argument("--max-fee-rate", default=None,
                   help="max rate to approve, a percent string like 0.01%% (default: the configured fee)")
    p.add_argument("--revoke", action="store_true", help="sign maxFeeRate 0%% (revoke the approval)")
    p.add_argument("--no-browser", action="store_true", help="only print the local URL")
    p.add_argument("--timeout-s", type=float, default=900.0, help="stop serving the page after this long")
    sub.add_parser("doctor", help="read-only checks of config, key, account, agent, builder approval, daemon")
    sub.add_parser("mcp", help="run the MCP server for AI agents on stdio (a client of the daemon)")
    return ap


def _confirm(input_fn: Callable[[str], str], prompt: str) -> bool:
    return input_fn(prompt + " [y/N] ").strip().lower() in ("y", "yes")


def main(argv: Optional[list[str]] = None, *,
         engine_factory: Callable[[OMXConfig], Any] = default_engine_factory,
         input_fn: Callable[[str], str] = input, out: TextIO = sys.stdout, err: TextIO = sys.stderr,
         clock: Callable[[], float] = time.time, transport_factory: Optional[Callable[[str], Any]] = None,
         serve_page: Optional[Callable[..., int]] = None) -> int:
    """engine_factory builds the daemon's engine (`omx daemon`) and the one-shot kill engine.
    transport_factory(base_url) -> transport for `omx doctor` (tests: a fake); serve_page replaces
    omx.approve_page.serve for `omx approve-builder` (tests)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(contains_key_like(a) for a in argv):
        err.write("refused: an argument looks like a private key. OMX never takes keys on the command line; "
                  "put the key FILE PATH or ENV VAR NAME in the config instead.\n")
        return 2
    ns = _parser().parse_args(argv)
    try:
        return _run(ns, engine_factory, input_fn, out, err, clock, transport_factory, serve_page)
    except (CLIError, ApprovalError, onboarding.NotEligible, ValueError) as e:
        err.write(f"error: {e}\n")
        return 1


def _ask(cfg: OMXConfig, op: str, args: dict, timeout_s: Optional[float] = None,
         request_id: Optional[str] = None) -> Any:
    try:
        return ipc.call(cfg.state_dir, op, args, TIMEOUT_S if timeout_s is None else timeout_s,
                        request_id=request_id)
    except ipc.IPCError as e:
        raise CLIError(str(e)) from None


def _ask_confirmed(cfg: OMXConfig, op: str, args: dict, out: TextIO) -> Any:
    """A human-privileged request: the daemon runs it only after a typed confirmation in its console.
    R3-10: the request id is printed here and shown by the daemon with what it would do, so the human
    can match them; the confirm code exists only in the daemon console."""
    rid = ipc.new_id()
    out.write(f"Request id {rid} ({op}).\n" + CONSOLE_NOTE.format(CONFIRM_TIMEOUT_S))
    out.flush()
    return _ask(cfg, op, args, CONFIRM_TIMEOUT_S, request_id=rid)


def _require_human(input_fn: Callable[[str], str]) -> None:
    """Local human-only commands read the answer from this process's stdin: require a real console
    (omx.tty.is_console: not NUL, a pipe or a file, which is how a program would answer for a human).
    Injected input_fn: tests/embedding."""
    if input_fn is builtins.input and not is_console(sys.stdin):
        raise CLIError("HUMAN_CONSOLE_REQUIRED: run this command yourself in an interactive terminal")


def _sweep_warning(rec: Any) -> Optional[str]:
    """R3-7: the same check for a daemon kill and a one-shot kill: did the venue sweep fail?"""
    if not isinstance(rec, dict):
        return None
    if rec.get("venue_sweep_error") or rec.get("cancel_errors"):
        why = rec.get("venue_sweep_error") or f"{rec.get('cancel_errors')} cancel errors"
        return (f"kill file set, so no new orders can be sent; but the venue sweep FAILED ({why}): "
                "resting orders may still be live. Re-run `omx kill` or cancel them in the Hyperliquid UI.")
    if rec.get("cancels_skipped"):
        return (f"kill file set, so no new orders can be sent; but {rec['cancels_skipped']} open order(s) could not "
                "be cancelled by OMX (no asset id, e.g. another dex): cancel them in the Hyperliquid UI.")
    return None


def _kill(cfg: OMXConfig, ns, engine_factory, out: TextIO) -> int:
    # 1) the KILL file stops every engine sharing state_dir, even with the venue or the daemon down
    reason = f"human:cli: {ns.reason}"
    kf = kill_from_outside(cfg.state_dir, reason, "all" if ns.all else "omx")
    scope = kf["scope"]                       # a stored "all" from an earlier kill is kept
    res: dict = {"kill_file": kf, "scope": scope}
    # 2) a live daemon cancels the resting orders
    if ipc.daemon_alive(cfg.state_dir):
        try:
            res["daemon"] = ipc.call(cfg.state_dir, "kill", {"reason": reason, "scope": scope}, TIMEOUT_S)
        except ipc.IPCError as e:
            res["daemon_error"] = str(e)
        else:
            warn = _sweep_warning(res["daemon"])
            if warn:
                res["warning"] = warn
            _dump(out, res)
            if warn:
                out.write("WARNING: " + warn + "\n")
                return 3
            return 0
    # 3) no live daemon: a one-shot cancel-only engine cancels them (no eligibility gate)
    try:
        eng = build_engine(engine_factory, cfg, "kill")
    except Exception as e:
        eng, res["engine_error"] = None, f"{type(e).__name__}: {e}"[:300]
    if eng is None:
        AuditLog(cfg.audit_path).append("kill_external", {"reason": reason, "scope": scope, "engine": False})
        res["warning"] = ("kill file set, so no new orders can be sent; resting orders were NOT cancelled "
                          "because no engine could be built. Cancel them in the Hyperliquid UI.")
        _dump(out, res)
        return 3
    rec = kill_engine(eng, reason, scope)
    res["one_shot_engine"] = rec
    warn = _sweep_warning(rec)
    if warn:
        res["warning"] = warn
        _dump(out, res)
        out.write("WARNING: " + res["warning"] + "\n")
        return 3
    _dump(out, res)
    return 0


def _approvals(cfg: OMXConfig, ns, input_fn, out: TextIO, clock, need_eligible) -> int:
    if ns.acmd in ("list", "reject") and not ipc.daemon_alive(cfg.state_dir):
        # no engine needed and no daemon writing the audit log: done locally
        if ns.acmd == "reject":
            _require_human(input_fn)
        q = ApprovalQueue(cfg.state_dir, AuditLog(cfg.audit_path), cfg.agent.approval_ttl_s, clock=clock)
        _dump(out, q.list(include_final=ns.all) if ns.acmd == "list" else q.reject(ns.approval_id, note=ns.note))
        return 0
    if ns.acmd == "list":
        _dump(out, _ask(cfg, "approvals.list", {"include_final": ns.all}))
        return 0
    if ns.acmd == "reject":
        _dump(out, _ask_confirmed(cfg, "approvals.reject", {"approval_id": ns.approval_id, "note": ns.note}, out))
        return 0
    need_eligible()
    rec = _ask(cfg, "approvals.get", {"approval_id": ns.approval_id})
    if rec["state"] != "pending":
        raise CLIError(f"{ns.approval_id} is {rec['state']}")
    _dump(out, rec)
    out.write(onboarding.fee_disclosure(cfg) + "\n")
    if not ns.yes and not _confirm(input_fn, f"Approve and submit {rec['intent']['intent_id']}?"):
        out.write("left pending\n")
        return 1
    res = _ask_confirmed(cfg, "approvals.approve", {"approval_id": ns.approval_id}, out)   # the daemon executes it
    _dump(out, res["report"])
    return 0 if res["ok"] else 1


def _init_builder(ns) -> tuple:
    """(builder address or None, fee in 0.1 bps) for `omx init`."""
    from . import config as config_mod
    if ns.no_builder:
        return None, 0
    if ns.builder_address:
        return ns.builder_address, 0 if ns.fee_tenths_bps is None else ns.fee_tenths_bps
    default = config_mod.DEFAULT_BUILDER.get(ns.network)
    if default is None:
        return None, 0 if ns.fee_tenths_bps is None else ns.fee_tenths_bps
    fee = config_mod.DEFAULT_BUILDER_FEE_TENTHS_BPS if ns.fee_tenths_bps is None else ns.fee_tenths_bps
    return default, fee


def _run(ns, engine_factory, input_fn, out, err, clock, transport_factory=None, serve_page=None) -> int:
    if ns.cmd == "doctor":                 # its own config check comes first, as one of the checks
        from .doctor import run_doctor
        return run_doctor(config_path(ns.config), out, transport_factory)

    if ns.cmd == "init":
        path = config_path(ns.config)
        if path.exists() and not ns.force:
            raise CLIError(f"{path} exists; use --force to overwrite")
        builder, fee = _init_builder(ns)
        text = render(ns.account, ns.network, ns.key_path, ns.key_env, builder, fee, ns.enable_agent)
        cfg = loads(text, base_dir=path.resolve().parent)
        if not onboarding.run_onboarding(cfg, input_fn, out):
            return 2
        path.write_text(text, encoding="utf-8")
        out.write(f"\nwrote {path}\n")
        return 0

    cfg = load_config_or_exit(ns.config, err)
    if cfg is None:
        return 2

    def need_eligible() -> None:
        prob = eligibility_gate(cfg)
        if prob:
            raise CLIError(f"{prob}: run `omx eligibility` first")

    if ns.cmd == "eligibility":
        _require_human(input_fn)
        if not ipc.daemon_alive(cfg.state_dir):                # no daemon: this process writes the audit record
            ok, _ = onboarding.run_eligibility(cfg.state_dir, input_fn, out, audit=AuditLog(cfg.audit_path),
                                               ttl_s=cfg.eligibility_ttl_s)
            return 0 if ok else 2
        att = onboarding.ask_attestation(input_fn, out)        # the human answers here,
        rec = _ask_confirmed(cfg, "eligibility.record", {"attestation": dataclasses.asdict(att)}, out)  # daemon records
        if rec["eligible"]:
            out.write("Eligibility confirmed until "
                      f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(rec['expires_s']))}; "
                      "you will be asked again after that.\n")
            return 0
        out.write("OMX cannot be used: " + ", ".join(rec["reasons"]) + "\n" + onboarding.NOT_AVAILABLE + "\n")
        return 2

    if ns.cmd == "fees":
        out.write(onboarding.fee_disclosure(cfg) + "\n")
        return 0

    if ns.cmd == "approve-builder":
        from .approve_page import ApprovePageError, approval_rate, serve
        try:
            approval_rate(cfg, ns.max_fee_rate, ns.revoke)          # fail fast, before serving anything
        except ApprovePageError as e:
            raise CLIError(str(e)) from None
        if not ns.revoke:
            out.write(onboarding.fee_disclosure(cfg) + "\n\n")
        return (serve_page or serve)(cfg, max_fee_rate=ns.max_fee_rate, revoke=ns.revoke, out=out,
                                     open_browser=not ns.no_browser, timeout_s=ns.timeout_s)

    if ns.cmd == "daemon":
        from .daemon import DaemonError, run_daemon
        if not ns.cancel_only:
            need_eligible()
        try:
            return run_daemon(cfg, engine_factory, out=out, cancel_only=ns.cancel_only)
        except DaemonError as e:
            raise CLIError(str(e)) from None

    if ns.cmd in ("preview", "submit"):
        need_eligible()
        order = {k: getattr(ns, k) for k in ORDER_FIELDS}
        iid = getattr(ns, "intent_id", None) or "hu-" + uuid.uuid4().hex[:16]    # client-side id
        intent = make_intent(**order, source="human", allowed_coins=cfg.policy.allowed_coins, intent_id=iid,
                             max_slippage_cap_bps=cfg.policy.price_band_bps)     # fail fast, before the daemon
        order["intent_id"] = intent.intent_id
        _dump(out, _ask(cfg, "preview", {"order": order}))
        out.write(onboarding.fee_disclosure(cfg) + "\n")
        if ns.cmd == "preview":
            return 0
        if not ns.yes and not _confirm(input_fn, f"Submit {intent.intent_id}?"):
            out.write("not submitted\n")
            return 1
        try:
            rep = ipc.call(cfg.state_dir, "submit", {"order": order}, TIMEOUT_S)
        except ipc.IPCTimeout as e:
            sent = "was NOT sent" if e.executed is False else "may have been sent"
            raise CLIError(f"{e}. Intent {intent.intent_id} {sent}: check with `omx status {intent.intent_id}`. "
                           f"To retry safely use `--intent-id {intent.intent_id}`: the daemon refuses an id it "
                           "already took, so a retry cannot double-order.") from None
        except ipc.IPCError as e:
            raise CLIError(str(e)) from None
        _dump(out, rep)
        return 0 if rep.get("state") != "rejected" else 1

    if ns.cmd in ("status", "cancel"):
        _dump(out, _ask(cfg, ns.cmd, {"intent_id": ns.intent_id}))
        return 0

    if ns.cmd == "kill":
        return _kill(cfg, ns, engine_factory, out)

    if ns.cmd == "unkill":
        _require_human(input_fn)
        out.write("Clearing the kill switch lets OMX send new orders again.\n")
        typed = input_fn(f"Type {UNKILL_PHRASE} to continue: ").strip()
        if typed != UNKILL_PHRASE:
            out.write("kill switch left on\n")
            return 1
        if ipc.daemon_alive(cfg.state_dir):              # the running engine must clear its own flag too
            res = _ask_confirmed(cfg, "unkill", {"confirm": typed}, out)
            _dump(out, res)
            return 0 if res.get("ok") else 1
        ok = KillSwitch(cfg.state_dir).clear(typed)
        AuditLog(cfg.audit_path).append("unkill_external", {"ok": ok})
        _dump(out, {"ok": ok, "daemon_running": False})
        return 0

    if ns.cmd == "approvals":
        return _approvals(cfg, ns, input_fn, out, clock, need_eligible)

    if ns.cmd == "audit":
        path = Path(ns.path) if ns.path else cfg.audit_path
        if not path.exists():
            raise CLIError(f"no audit log at {path}")
        ok, n, msg = AuditLog.verify(path)
        torn = msg.startswith(TRUNCATED_TAIL)
        if ns.acmd == "verify":
            if torn:     # a crash mid-write, not tampering
                out.write(f"{TRUNCATED_TAIL}: {n} valid records, then a partial last line from a crash "
                          "(not tampering). Run `omx audit repair` to move it aside.\n")
                return 4
            out.write(f"{'OK' if ok else 'BROKEN'}: {n} records checked, {msg}\n")
            return 0 if ok else 3
        _require_human(input_fn)
        if ok:
            out.write(f"OK: {n} records, nothing to repair\n")
            return 0
        if not torn:
            out.write(f"BROKEN: {msg}. This is not a torn last line; repair refused (possible tampering).\n")
            return 3
        out.write(f"The partial last line after {n} valid records will be moved to {path.name}.tail-<ms> and a "
                  "`repair` record appended. Nothing valid is deleted.\n")
        if input_fn(f"Type {REPAIR_PHRASE} to continue: ").strip() != REPAIR_PHRASE:
            out.write("not repaired\n")
            return 1
        if ipc.daemon_alive(cfg.state_dir) and path.resolve() == Path(cfg.audit_path).resolve():
            res = _ask_confirmed(cfg, "audit.repair", {}, out)       # the daemon is the audit writer
            out.write(f"moved tail to {res.get('moved_to')}; now {'OK' if res['ok'] else 'BROKEN'}: "
                      f"{res['records']} records, {res['message']}\n")
            return 0 if res["ok"] else 3
        side = AuditLog.repair(path)          # takes the log's cross-process lock
        ok, n, msg = AuditLog.verify(path)
        out.write(f"moved tail to {side}; now {'OK' if ok else 'BROKEN'}: {n} records, {msg}\n")
        return 0 if ok else 3

    if ns.cmd == "mcp":
        from .mcp_server import serve
        need_eligible()
        if not cfg.agent.enabled:
            raise CLIError("agent access is off; set [agent] enabled = true")
        if not ipc.daemon_alive(cfg.state_dir):
            err.write("warning: `omx daemon` is not running; MCP tools answer DAEMON_NOT_RUNNING until it is\n")
        serve(cfg)
        return 0
    raise CLIError(f"unknown command {ns.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
