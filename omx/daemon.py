"""`omx daemon`: the one process that owns the Engine, the AuditLog writer and the approvals executor.

Fix contract R1, item 8. The CLI and the MCP server are thin clients over the file IPC in
<state_dir>/ipc/ (omx.ipc). The daemon thread is the only thread that ever touches the engine:
each loop it calls engine.step() (every 200 ms), then reads its console, then serves the queued
requests in FIFO order. Because requests run one at a time in that thread, the agent budget
check+reserve is atomic.

Heartbeat (R2/B3): a separate thread writes <state_dir>/daemon.heartbeat every second with the
wall time the loop last made progress, so a loop that waits on the venue reads as "busy", never
as "not running".

Human-privileged ops (R2/B2): approvals approve/reject, unkill, eligibility.record and
audit.repair are never executed from an IPC file alone (any same-user process can write one).
The daemon prints the request in its own console with its substance (R3-10: for an approval the
intent's coin, side, size, notional, algo and source agent; for unkill the kill reason and scope)
and the request id the CLI printed, and executes it only after a human types the exact line
`confirm <code>` there; the code is shown only in this console. `deny <code>` refuses it. Without
an interactive console (omx.tty: a real console, not NUL, a pipe or a file) these ops are refused
with HUMAN_CONSOLE_REQUIRED at once. Agent ops keep working over IPC. Every request id is executed
at most once (ipc/processed.jsonl); a submit whose intent_id was already taken is not executed
again (state_dir/intent_ids.jsonl, shared by human submits and agent client_intent_ids, R3-3), so
a retry after an IPC timeout cannot double-order.

Eligibility (R2/B1): re-checked every 10 minutes; when it fails the daemon switches to
cancel-only for the rest of its life: working intents are cancelled, resting OMX orders are
cancelled, new intents are refused with CANCEL_ONLY. `omx daemon --cancel-only` starts without
the eligibility gate in that mode (to sweep orphans of an expired or blocked user).

Builder approval: with a builder configured (address + fee > 0) the daemon reads maxBuilderFee for
(account, builder) through the engine at start and every BUILDER_CHECK_S (10 min; every
BUILDER_RETRY_S = 60 s while not approved). Approved < configured fee, or no answer at start, puts it
in BUILDER_NOT_APPROVED: new intents (submit, approvals approve, agent submit_order) are refused
with that reason and the hint `omx approve-builder`; working intents keep running and can be
cancelled; kill always works. A read error after a good read keeps the last state and retries in
60 s (Hyperliquid itself rejects orders above the approval). The state lifts at the next check that
sees the approval.

Single instance: an exclusive OS lock on <state_dir>/daemon.lock and (R3-5) one on a per-account
lock file in a user-level dir (config.account_lock_path: lock_dir / $OMX_LOCK_DIR /
%LOCALAPPDATA%/omx/locks/<network>-<account>.lock), both released by the OS if the process dies.
A second daemon on the same state_dir, or on the same account from another state_dir, refuses to
start.

Start: approvals left "approved" without a result by a crashed daemon become
unknown_after_crash and are never re-executed.

Shutdown (Ctrl+C or stop()): finishes the current loop, answers queued requests with
DAEMON_STOPPING, writes `daemon_stop` to the audit log, removes the heartbeat and releases the
lock. It does NOT set the kill switch and asks the engine to cancel NOTHING: resting OMX child
orders stay on the venue and sliced intents (TWAP/POV/chase) simply stop sending. On the next
start the engine cancels orphan OMX orders from the previous run (contract item 6). To cancel
resting orders now, run `omx kill`, which works with or without a daemon.
"""
from __future__ import annotations

import dataclasses
import inspect
import os
import queue
import secrets
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from . import ipc
from .approvals import AgentBudget, ApprovalError, ApprovalQueue, execute_approval, make_intent
from .audit import TRUNCATED_TAIL, AuditLog
from .builder import builder_of, not_approved_message
from .config import OMXConfig, account_lock_path, eligibility_gate
from .engine_api import build_engine, kill_engine
from .mcp_server import ORDER_KEYS, AgentGateway
from .tty import is_console

from .types import to_record

STEP_S = 0.2
HB_S = 1.0                         # heartbeat thread period
ELIG_CHECK_S = 600.0               # eligibility re-check period of a running daemon
HUMAN_CONFIRM_OPS = frozenset({"approvals.approve", "approvals.reject", "unkill", "eligibility.record",
                               "audit.repair"})
CANCEL_ONLY_REFUSED = frozenset({"preview", "submit", "approvals.approve", "agent.preview_order",
                                 "agent.submit_order"})
BUILDER_CHECK_S = 600.0            # maxBuilderFee re-check period while approved
BUILDER_RETRY_S = 60.0             # ... while not approved, or after a read error
BUILDER_REFUSED = frozenset({"submit", "approvals.approve", "agent.submit_order"})   # new intents


class DaemonError(RuntimeError):
    pass


def default_factory(cfg: OMXConfig, purpose: str = "trade"):
    from .engine import from_config
    return from_config(cfg, purpose=purpose)


class StdConsole:
    """The daemon's own terminal: lines typed on its stdin (read by a helper thread), text to its stdout."""

    def __init__(self, stream_in: TextIO, stream_out: TextIO):
        self.inp, self.out, self.q = stream_in, stream_out, queue.Queue()
        threading.Thread(target=self._read, name="omx-console", daemon=True).start()

    def _read(self) -> None:
        try:
            for line in iter(self.inp.readline, ""):
                self.q.put(line)
        except (OSError, ValueError):
            pass

    def show(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def lines(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out


class QueueConsole(StdConsole):
    """In-memory console (tests, embedding): feed() types a line; responder(text) may answer what is shown."""

    def __init__(self, responder: Optional[Callable[[str], Optional[str]]] = None):
        self.q, self.shown, self.responder = queue.Queue(), [], responder

    def feed(self, line: str) -> None:
        self.q.put(line)

    def show(self, text: str) -> None:
        self.shown.append(text)
        if self.responder is not None:
            line = self.responder(text)
            if line:
                self.q.put(line)


def interactive_console(out: Optional[TextIO]) -> Optional[StdConsole]:
    """A console on the daemon's stdin when it is a real interactive console (omx.tty.is_console:
    not NUL, a pipe or a file); None otherwise (human ops are then refused)."""
    return StdConsole(sys.stdin, out or sys.stdout) if is_console(sys.stdin) else None


class InstanceLock:
    """Exclusive, non-blocking OS file lock (msvcrt on Windows, flock elsewhere)."""

    def __init__(self, path: Path, busy: str = "another OMX daemon already runs on this state_dir"):
        self.path, self.fh, self.busy = Path(path), None, busy

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            try:
                owner = self.path.with_suffix(".owner").read_text(encoding="utf-8")[:300]
            except OSError:
                owner = "unknown"
            raise DaemonError(f"{self.busy} ({self.path}; held by {owner})") from None
        self.fh = fh
        try:                          # who holds it, for the next refusal message (best effort)
            ipc.write_atomic(self.path.with_suffix(".owner"), f"pid {os.getpid()}")
        except OSError:
            pass

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


class OwnedEngine:
    """Proxy that refuses any engine use from a thread other than its owner (the engine is not thread-safe)."""

    def __init__(self, engine: Any, owner: Optional[int] = None):
        self.__dict__["_engine"] = engine
        self.__dict__["_owner"] = owner if owner is not None else threading.get_ident()

    def __getattr__(self, name: str) -> Any:
        if threading.get_ident() != self.__dict__["_owner"]:
            raise RuntimeError(f"ENGINE_THREAD_VIOLATION: engine.{name} used outside the daemon thread")
        return getattr(self.__dict__["_engine"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("OwnedEngine is read-only")


def _rep(obj: Any) -> Any:
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


AGENT_OPS: dict[str, Callable] = {
    "get_quote": lambda gw, src, coin: gw.get_quote(coin),
    "preview_order": lambda gw, src, args: gw.preview_order(src, args),
    "submit_order": lambda gw, src, args: gw.submit_order(src, args),
    "approval_status": lambda gw, src, approval_id: gw.approval_status(approval_id),
    "order_status": lambda gw, src, intent_id: gw.order_status(intent_id),
    "get_receipt": lambda gw, src, intent_id: gw.get_receipt(intent_id),
    "cancel_order": lambda gw, src, intent_id: gw.cancel_order(src, intent_id),
    "kill_switch": lambda gw, src, reason: gw.kill_switch(src, reason),
    "account_summary": lambda gw, src: gw.account_summary(),
    "policy_limits": lambda gw, src: gw.policy_limits(),
}


class Daemon:
    def __init__(self, cfg: OMXConfig, engine_factory: Optional[Callable[..., Any]] = None,
                 step_s: float = STEP_S, clock: Callable[[], float] = time.time, out: Optional[TextIO] = None,
                 console: Any = "auto", cancel_only: bool = False, elig_check_s: float = ELIG_CHECK_S,
                 hb_s: float = HB_S, builder_check_s: float = BUILDER_CHECK_S,
                 builder_retry_s: float = BUILDER_RETRY_S):
        """console: "auto" = the process stdin when it is a TTY (else none); a StdConsole/QueueConsole;
        or None = no console, human-privileged ops are refused with HUMAN_CONSOLE_REQUIRED."""
        self.cfg, self.factory, self.step_s, self.clock, self.out = cfg, engine_factory or default_factory, step_s, clock, out
        self.console, self.cancel_only, self.elig_check_s, self.hb_s = console, bool(cancel_only), elig_check_s, hb_s
        self.state_dir = Path(cfg.state_dir)
        self.ready, self._stop, self._hb_stop = threading.Event(), threading.Event(), threading.Event()
        self.error: Optional[BaseException] = None
        self.engine = self.audit = self.queue = self.budget = self.gw = None
        self.processed = self.intent_ids = None
        self.loops, self.thread_id = 0, None
        self._progress_ms = time.time() * 1000
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_errors = 0
        self._pending: dict[str, tuple[str, dict]] = {}     # console code -> (request id, request)
        self._elig_next = 0.0
        self._last_err: tuple = ("", 0.0)
        self.builder = builder_of(cfg.builder_address, cfg.builder_fee_tenths_bps)
        self.builder_check_s, self.builder_retry_s = builder_check_s, builder_retry_s
        self.builder_block: Optional[str] = None       # BUILDER_NOT_APPROVED: ... while new intents are refused
        self.builder_last: Optional[dict] = None
        self._builder_next = 0.0
        self._builder_ok_once = False
        self._human = {"ping": self.h_ping, "preview": self.h_preview, "submit": self.h_submit,
                       "status": self.h_status, "receipt": self.h_receipt, "cancel": self.h_cancel, "kill": self.h_kill, "unkill": self.h_unkill,
                       "approvals.list": self.h_approvals_list, "approvals.get": self.h_approvals_get,
                       "approvals.reject": self.h_approvals_reject, "approvals.approve": self.h_approvals_approve,
                       "eligibility.record": self.h_eligibility_record, "audit.repair": self.h_audit_repair}

    # ------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        self._stop.set()

    def start_thread(self, timeout_s: float = 10.0) -> threading.Thread:
        """Run the daemon in a background thread (embedding, tests). The engine is built in that thread."""
        t = threading.Thread(target=self.run, name="omx-daemon", daemon=True)
        t.start()
        if not self.ready.wait(timeout_s):
            raise DaemonError("daemon did not start in time")
        if self.error is not None:
            raise self.error
        return t

    def _say(self, text: str) -> None:
        if self.console is not None:
            self.console.show(text)
        elif self.out is not None:
            self.out.write(text + "\n")
            self.out.flush()

    def run(self) -> int:
        """Blocking loop. Returns 0 on a clean stop, 1 on error (self.error holds it)."""
        lock = InstanceLock(self.state_dir / ipc.LOCK)
        acct_lock = InstanceLock(account_lock_path(self.cfg),
                                 "another OMX daemon already trades this account (from another state_dir)")
        try:
            lock.acquire()
            try:
                acct_lock.acquire()
            except DaemonError:
                lock.release()
                raise
        except DaemonError as e:
            self.error = e
            self.ready.set()
            return 1
        prev_sigint, reason = None, "stop"
        try:
            if not self.cancel_only:
                prob = eligibility_gate(self.cfg)
                if prob:
                    raise DaemonError(f"{prob}: run `omx eligibility` first (or `omx daemon --cancel-only` "
                                      "to only cancel resting OMX orders)")
            self.thread_id = threading.get_ident()
            raw = build_engine(self.factory, self.cfg, "kill" if self.cancel_only else "trade")
            if raw is None:
                raise DaemonError("engine factory returned no engine")
            self.engine = OwnedEngine(raw, self.thread_id)
            self.audit = getattr(raw, "audit", None) or AuditLog(self.cfg.audit_path)   # the single writer
            self.queue = ApprovalQueue(self.state_dir, self.audit, self.cfg.agent.approval_ttl_s, clock=self.clock)
            crashed = self.queue.mark_crashed()
            self.budget = AgentBudget(self.state_dir, self.cfg.agent.daily_budget_usd, clock=self.clock)
            self.processed = ipc.IdLedger(ipc.dirs(self.state_dir)["inbox"].parent / ipc.PROCESSED)
            self.intent_ids = ipc.IdLedger(self.state_dir / "intent_ids.jsonl", max_n=100_000)
            self.gw = AgentGateway(self.engine, self.cfg, self.queue, self.budget, self.audit, clock=self.clock,
                                   intent_ids=self.intent_ids)
            if self.console == "auto":
                self.console = interactive_console(self.out)
            for rid in ipc.orphaned_work(self.state_dir):
                ipc.respond(self.state_dir, rid, False, error="DAEMON_RESTARTED: outcome unknown, check status")
            if self.cancel_only and getattr(self.engine, "enter_cancel_only", None) is not None:
                self.engine.enter_cancel_only("daemon --cancel-only")
            self._check_builder(force=True)
            if threading.current_thread() is threading.main_thread():
                prev_sigint = signal.signal(signal.SIGINT, lambda *_: self._stop.set())
            self.audit.append("daemon_start", {"pid": os.getpid(), "network": self.cfg.network,
                                               "step_ms": int(self.step_s * 1000), "cancel_only": self.cancel_only,
                                               "console": self.console is not None,
                                               "approvals_unknown_after_crash": crashed,
                                               "builder": list(self.builder) if self.builder else None,
                                               "builder_block": self.builder_block})
            self._elig_next = time.monotonic() + self.elig_check_s
            self._progress_ms = time.time() * 1000
            self._beat()
            self._hb_thread = threading.Thread(target=self._hb_loop, name="omx-heartbeat", daemon=True)
            self._hb_thread.start()
            self.ready.set()
            if self.out is not None:
                mode = "CANCEL-ONLY (no new orders) " if self.cancel_only else ""
                self.out.write(f"OMX daemon running {mode}(pid {os.getpid()}, state {self.state_dir}). Ctrl+C stops "
                               "it; it does not set the kill switch or cancel resting orders.\n")
                if self.console is None:
                    self.out.write("No interactive console: approvals, unkill, eligibility and audit repair "
                                   "requests are refused (HUMAN_CONSOLE_REQUIRED).\n")
                else:
                    self.out.write("Human requests (approve, reject, unkill, eligibility, audit repair) are shown "
                                   "here and run only after you type the confirm line shown.\n")
                if crashed:
                    self.out.write(f"Approvals with unknown outcome after a crash (not re-executed): {crashed}\n")
                if self.builder_block:
                    self.out.write(self.builder_block + "\n")
                self.out.flush()
            while not self._stop.is_set():
                t0 = time.monotonic()
                self._step()
                self._drain_console()
                self._serve()
                self._expire_confirmations()
                self._check_eligibility()
                self._check_builder()
                self.loops += 1
                self._progress_ms = time.time() * 1000
                if self.loops % 100 == 0:
                    ipc.sweep(self.state_dir)
                self._stop.wait(max(0.0, self.step_s - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            reason = "keyboard_interrupt"
        except BaseException as e:                     # noqa: BLE001 - reported via self.error
            self.error, reason = e, f"error: {type(e).__name__}: {e}"[:300]
        finally:
            self._hb_stop.set()
            if self._hb_thread is not None:
                self._hb_thread.join(5)
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            try:
                if self.audit is not None and self.ready.is_set():
                    self._shutdown(reason)
            finally:
                acct_lock.release()
                lock.release()
                self.ready.set()
        return 1 if self.error is not None else 0

    def _shutdown(self, reason: str) -> None:
        for rid, _, _ in ipc.claim_pending(self.state_dir):
            ipc.respond(self.state_dir, rid, False, error="DAEMON_STOPPING: not executed")
        for rid, _ in self._pending.values():
            ipc.respond(self.state_dir, rid, False, error="DAEMON_STOPPING: not confirmed, not executed")
        self._pending.clear()
        intents = getattr(self.engine, "intents", None)
        try:
            self.audit.append("daemon_stop", {"pid": os.getpid(), "reason": reason, "loops": self.loops,
                                              "intents": len(intents()) if intents else None,
                                              "kill_switch_set": False, "orders_cancelled": 0})
        finally:
            (self.state_dir / ipc.HEARTBEAT).unlink(missing_ok=True)

    def _beat(self) -> None:
        try:
            ipc.write_heartbeat(self.state_dir, {"pid": os.getpid(), "loops": self.loops,
                                                 "step_ms": int(self.step_s * 1000),
                                                 "loop_progress_ms": int(self._progress_ms),
                                                 "cancel_only": self.cancel_only,
                                                 "builder_not_approved": self.builder_block is not None,
                                                 "confirmations_pending": len(self._pending)})
        except OSError:                        # a missed beat must not stop the daemon
            self._hb_errors += 1

    def _hb_loop(self) -> None:
        """Heartbeat thread: liveness of the process, independent of how long one loop takes."""
        while not self._hb_stop.wait(self.hb_s):
            self._beat()

    def _note_error(self, kind: str, e: BaseException) -> None:
        msg = f"{type(e).__name__}: {e}"[:300]
        last, at = self._last_err
        if msg != last or time.time() - at > 60:          # do not flood the audit log at 5 Hz
            self._last_err = (msg, time.time())
            self.audit.append(kind, {"error": msg})

    def _step(self) -> None:
        try:
            self.engine.step()
        except Exception as e:                             # the engine keeps running on venue errors
            self._note_error("daemon_step_error", e)

    # ------------------------------------------------------------ eligibility
    def _check_eligibility(self) -> None:
        if self.cancel_only or time.monotonic() < self._elig_next:
            return
        self._elig_next = time.monotonic() + self.elig_check_s
        prob = eligibility_gate(self.cfg)
        if prob:
            self.lose_eligibility(prob)

    def lose_eligibility(self, prob: str) -> None:
        """Cancel-only from now on: no new intents or children; working intents and resting OMX orders
        are cancelled. Stays so until the daemon is restarted after `omx eligibility` passes again."""
        self.cancel_only = True
        rec: Any = None
        fn = getattr(self.engine, "enter_cancel_only", None)
        try:
            if fn is not None:
                rec = fn(f"ELIGIBILITY:{prob}")
            else:                                          # engines without the mode: cancel what we know
                for iid in getattr(self.engine, "intents", lambda: [])():
                    self.engine.cancel(iid)
        except Exception as e:                             # the engine loop keeps sweeping
            rec = {"error": f"{type(e).__name__}: {e}"[:300]}
        self.audit.append("eligibility_lost", {"reason": prob, "result": rec})
        self._say(f"OMX: eligibility check failed ({prob}). The daemon is now CANCEL-ONLY: working intents "
                  "were cancelled, resting OMX orders are being cancelled and new intents are refused. Run "
                  "`omx eligibility`, then restart the daemon.")

    # ------------------------------------------------------------ builder approval
    def _check_builder(self, force: bool = False) -> None:
        """Read maxBuilderFee through the engine and set / lift BUILDER_NOT_APPROVED (see module doc)."""
        if self.builder is None or self.cancel_only:
            return
        if not force and time.monotonic() < self._builder_next:
            return
        fn = getattr(self.engine, "builder_approval", None)
        if fn is None:
            res = {"ok": False, "approved": None, "error": "the engine cannot read the builder approval"}
        else:
            try:
                res = dict(fn())
            except Exception as e:  # noqa: BLE001 - an unknown approval is not an approval
                res = {"ok": False, "approved": None, "error": f"{type(e).__name__}: {e}"[:300]}
        self.builder_last = res
        if not res.get("ok") and res.get("error") and self._builder_ok_once and self.builder_block is None:
            self._builder_next = time.monotonic() + self.builder_retry_s      # transient: keep, retry soon
            self._note_error("builder_check_error", RuntimeError(res["error"]))
            return
        block = None if res.get("ok") else not_approved_message(self.builder, res.get("approved"),
                                                                 str(res.get("error") or ""))
        changed = (block is None) != (self.builder_block is None)
        self.builder_block = block
        if res.get("ok"):
            self._builder_ok_once = True
        setter = getattr(self.engine, "set_builder_block", None)
        if setter is not None:
            setter(block)
        self._builder_next = time.monotonic() + (self.builder_check_s if block is None else self.builder_retry_s)
        if changed and not force:                   # the start result is in the daemon_start record
            self.audit.append("builder_check", {"ok": bool(res.get("ok")), "approved": res.get("approved"),
                                                "configured": self.builder[1], "builder": self.builder[0],
                                                "error": res.get("error"), "blocked": block is not None})
        if changed and block is not None and not force:
            self._say("OMX: " + block)
        elif changed and block is None and not force:
            self._say(f"OMX: builder fee approval confirmed ({res.get('approved')} >= {self.builder[1]} "
                      "tenths of a bps); new intents are accepted again.")

    # ------------------------------------------------------------ requests
    def _serve(self) -> None:
        for rid, req, err in ipc.claim_pending(self.state_dir):
            if err:
                ipc.respond(self.state_dir, rid, False, error=err)
                continue
            if time.time() * 1000 > req["deadline_ms"]:
                ipc.respond(self.state_dir, rid, False, error="EXPIRED: deadline passed, not executed")
                continue
            if rid in self.processed:
                self.audit.append("ipc_replay_refused", {"id": rid, "op": req["op"], "client": req["client"]})
                ipc.respond(self.state_dir, rid, False, error="REPLAYED: this request id was already processed")
                continue
            self.processed.add(rid, req["deadline_ms"])
            if req["op"] in HUMAN_CONFIRM_OPS:
                self._human_request(rid, req)
                continue
            self._execute(rid, req)

    def _execute(self, rid: str, req: dict) -> None:
        try:
            ipc.respond(self.state_dir, rid, True, self.handle(req["op"], req["args"], req["client"]))
        except (ValueError, ApprovalError, DaemonError) as e:
            ipc.respond(self.state_dir, rid, False, error=str(e)[:500])
        except Exception as e:
            self._note_error("daemon_request_error", e)
            ipc.respond(self.state_dir, rid, False, error=f"INTERNAL:{type(e).__name__}: {e}"[:500])

    def _human_request(self, rid: str, req: dict) -> None:
        """A human-privileged op: check it, then wait for a typed confirmation in the daemon console."""
        op, args = req["op"], req["args"]
        try:
            self._resolve(op, args, req["client"])         # role / args / cancel-only fail fast
        except ValueError as e:
            ipc.respond(self.state_dir, rid, False, error=str(e)[:500])
            return
        if self.console is None:
            self.audit.append("human_op_refused", {"id": rid, "op": op, "why": "HUMAN_CONSOLE_REQUIRED"})
            ipc.respond(self.state_dir, rid, False, error=(
                "HUMAN_CONSOLE_REQUIRED: the daemon has no interactive console, so it does not run "
                f"{op} from an IPC request. Start `omx daemon` in a terminal and confirm there."))
            return
        code = secrets.token_hex(3)
        self._pending[code] = (rid, req)
        left = max(0.0, req["deadline_ms"] / 1000 - time.time())
        what = self._describe(op, args)
        self.audit.append("human_op_pending", {"id": rid, "op": op, "args": args, "shown": what})
        self.console.show(f"\nOMX CONFIRMATION NEEDED [{code}]: {op}\n  {what}\n"
                          f"  request id {rid} (the CLI printed the id it sent: they must match).\n"
                          "  It arrived over IPC; any program of this OS user can write one.\n"
                          f"  Type exactly `confirm {code}` to execute it, or `deny {code}` ({left:.0f} s left).")

    def _describe(self, op: str, args: dict) -> str:
        """R3-10: what a confirmation would actually do, from the daemon's own state (not the request text)."""
        try:
            if op in ("approvals.approve", "approvals.reject"):
                r = self.queue.get(str(args.get("approval_id")))
                it = r["intent"]
                verb = "APPROVE and SUBMIT" if op == "approvals.approve" else "REJECT"
                lim = f" limit {it['limit_px']:g}" if it.get("limit_px") is not None else ""
                ro = " reduce-only" if it.get("reduce_only") else ""
                return (f"{verb} {r['approval_id']}: {it['side'].upper()} {it['size']:g} {it['coin']} algo "
                        f"{it['algo']}{lim}{ro}, notional bound {r['notional_est_usd']:,.2f} USD, from "
                        f"{it['source']}, intent {it['intent_id']}, queued for {', '.join(r['reasons'])}, "
                        f"state {r['state']}" + (f", note {args.get('note')!r}" if args.get("note") else ""))
            if op == "unkill":
                from .killswitch import KillSwitch
                k = KillSwitch(self.state_dir).info()
                if not k:
                    return "CLEAR the kill switch (it is not set now)"
                when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(k.get("ts_ms", 0)) / 1000))
                return (f"CLEAR the kill switch: scope {k.get('scope', 'omx')}, reason {k.get('reason')!r}, "
                        f"set by {k.get('source')} at {when}; new orders become possible again")
            if op == "eligibility.record":
                a = args.get("attestation") or {}
                return (f"RECORD eligibility answers: residence {a.get('residence_country')!r}, citizenships "
                        f"{a.get('citizenships')!r}, current country {a.get('current_country')!r}")
            if op == "audit.repair":
                return f"MOVE the torn last line of {self.cfg.audit_path} aside"
        except Exception as e:                             # an unknown approval id etc.: show the request
            return f"{ipc.dumps(args)[:300]} (could not resolve: {type(e).__name__}: {e})"[:500]
        return ipc.dumps(args)[:300]

    def _drain_console(self) -> None:
        if self.console is None:
            return
        for line in self.console.lines():
            words = line.strip().split()
            if len(words) == 2 and words[0] in ("confirm", "deny") and words[1] in self._pending:
                rid, req = self._pending.pop(words[1])
                if time.time() * 1000 > req["deadline_ms"]:
                    ipc.respond(self.state_dir, rid, False, error="EXPIRED: not confirmed in time, not executed")
                    self.console.show(f"[{words[1]}] expired, not executed")
                elif words[0] == "deny":
                    self.audit.append("human_op_denied", {"id": rid, "op": req["op"]})
                    ipc.respond(self.state_dir, rid, False, error="DENIED_AT_CONSOLE: not executed")
                    self.console.show(f"[{words[1]}] denied")
                else:
                    self.audit.append("human_op_confirmed", {"id": rid, "op": req["op"]})
                    self._execute(rid, req)
                    self.console.show(f"[{words[1]}] {req['op']} executed")
            elif line.strip():
                waiting = ", ".join(f"{c} ({r['op']})" for c, (_, r) in self._pending.items()) or "none"
                self.console.show(f"not understood: {line.strip()[:80]!r}. Pending: {waiting}")

    def _expire_confirmations(self) -> None:
        now_ms = time.time() * 1000
        for code, (rid, req) in list(self._pending.items()):
            if now_ms > req["deadline_ms"]:
                del self._pending[code]
                ipc.respond(self.state_dir, rid, False, error="EXPIRED: not confirmed at the daemon console in time")
                self.console.show(f"[{code}] {req['op']} expired, not executed")

    def _resolve(self, op: str, args: dict, client: str) -> Callable[[], Any]:
        """Check role, args and mode; return the call. Agents reach only agent.* ops (the AgentGateway)."""
        if self.cancel_only and op in CANCEL_ONLY_REFUSED:
            raise ValueError("CANCEL_ONLY: this daemon only cancels; no new intents")
        if self.builder_block and op in BUILDER_REFUSED:
            raise ValueError(self.builder_block)
        if op.startswith("agent."):
            fn = AGENT_OPS.get(op[6:])
            if fn is None:
                raise ValueError(f"UNKNOWN_OP:{op}")
            if not client.startswith("agent:"):
                raise ValueError("ROLE_REFUSED: agent ops need an agent client")
            self._bind(fn, self.gw, client, **args)
            return lambda: fn(self.gw, client, **args)
        hfn = self._human.get(op)
        if hfn is None:
            raise ValueError(f"UNKNOWN_OP:{op}")
        if client != "human:cli":
            raise ValueError(f"ROLE_REFUSED: agents cannot call {op}")
        self._bind(hfn, **args)
        return lambda: hfn(**args)

    def handle(self, op: str, args: dict, client: str) -> Any:
        """Run one request in the daemon thread (console confirmation, if needed, is already done)."""
        return self._resolve(op, args, client)()

    @staticmethod
    def _bind(fn: Callable, *a, **kw) -> None:
        try:
            inspect.signature(fn).bind(*a, **kw)
        except TypeError as e:
            raise ValueError(f"BAD_ARGS: {e}") from None

    def _need_eligible(self) -> None:
        prob = eligibility_gate(self.cfg)
        if prob:
            raise ValueError(f"{prob}: run `omx eligibility` first")

    def _human_intent(self, order: dict):
        if not isinstance(order, dict) or set(order) - ORDER_KEYS - {"intent_id"}:
            raise ValueError("BAD_ARGS: order fields")
        return make_intent(source="human", allowed_coins=self.cfg.policy.allowed_coins,
                           max_slippage_cap_bps=self.cfg.policy.price_band_bps, **order)

    def _claim_intent_id(self, iid: str) -> None:
        """Each intent id is submitted at most once, across daemon restarts."""
        if iid in self.intent_ids:
            raise ValueError(f"DUPLICATE_INTENT_ID: {iid} was already submitted; see `omx status {iid}`")
        self.intent_ids.add(iid)

    def h_ping(self) -> dict:
        return {"pid": os.getpid(), "network": self.cfg.network, "loops": self.loops, "cancel_only": self.cancel_only,
                "builder_block": self.builder_block}

    def h_preview(self, order: dict) -> dict:
        self._need_eligible()
        intent = self._human_intent(order)
        return {"intent": to_record(intent), "preview": self.engine.preview(intent)}

    def h_submit(self, order: dict) -> dict:
        self._need_eligible()
        intent = self._human_intent(order)
        self._claim_intent_id(intent.intent_id)
        return _rep(self.engine.submit(intent))

    def h_status(self, intent_id: str) -> dict:
        rep = _rep(self.engine.status(str(intent_id)))
        if isinstance(rep, dict) and rep.get("state") == "unknown" and str(intent_id) in self.intent_ids:
            rep["message"] = ("submitted to an earlier daemon run (it restarted since): state unknown here, it may "
                              "have filled; check the audit log (`intent_done`) and the venue. " + rep.get("message", ""))
        return rep

    def h_receipt(self, intent_id: str) -> dict:
        try:
            return self.engine.receipt(str(intent_id), network=self.cfg.network)
        except KeyError:
            raise ValueError(f"UNKNOWN_INTENT_ID: {intent_id} is not known to this daemon run; "
                             "earlier runs are in the audit log") from None

    def h_cancel(self, intent_id: str) -> dict:
        return _rep(self.engine.cancel(str(intent_id)))

    def h_kill(self, reason: str, scope: str = "omx") -> dict:
        return kill_engine(self.engine, str(reason)[:300], scope)

    def h_unkill(self, confirm: str) -> dict:
        return self.engine.unkill(str(confirm))

    def h_approvals_list(self, include_final: bool = False) -> list:
        return self.queue.list(include_final=bool(include_final))

    def h_approvals_get(self, approval_id: str) -> dict:
        return self.queue.get(str(approval_id))

    def h_approvals_reject(self, approval_id: str, note: str = "") -> dict:
        return self.queue.reject(str(approval_id), note=str(note))

    def h_eligibility_record(self, attestation: dict) -> dict:
        """Store a human's answers (asked by the CLI) with the daemon's audit writer. The rules are re-run."""
        from .eligibility import attestation_of, check, record_attempt
        if not isinstance(attestation, dict):
            raise ValueError("BAD_ARGS: attestation")
        try:
            att = attestation_of(attestation)
        except TypeError as e:
            raise ValueError(f"BAD_ARGS: attestation: {e}") from None
        ok, reasons = check(att)
        return record_attempt(self.state_dir, att, ok, reasons, now_s=time.time(),
                              ttl_s=self.cfg.eligibility_ttl_s, audit=self.audit)

    def h_audit_repair(self) -> dict:
        """Move a torn last line of the configured audit log aside (never a tampered one)."""
        path = self.cfg.audit_path
        ok, n, msg = AuditLog.verify(path)
        if ok:
            return {"ok": True, "repaired": False, "records": n, "message": msg}
        if not msg.startswith(TRUNCATED_TAIL):
            raise ValueError(f"REPAIR_REFUSED: {msg}; not a torn last line (possible tampering)")
        side = AuditLog.repair(path)
        ok, n, msg = AuditLog.verify(path)
        return {"ok": ok, "repaired": True, "moved_to": str(side), "records": n, "message": msg}

    def h_approvals_approve(self, approval_id: str) -> dict:
        self._need_eligible()

        def submit(intent):
            if intent.intent_id not in self.intent_ids:
                self.intent_ids.add(intent.intent_id)
            return self.engine.submit(intent)
        ok, rep, intent = execute_approval(self.queue, str(approval_id), submit, self.budget)
        if ok:
            self.gw.register_owner(intent.intent_id, intent.source)
        return {"ok": ok, "report": rep}


def run_daemon(cfg: OMXConfig, engine_factory: Optional[Callable[..., Any]] = None,
               out: Optional[TextIO] = None, cancel_only: bool = False, console: Any = "auto") -> int:
    d = Daemon(cfg, engine_factory, out=out, cancel_only=cancel_only, console=console)
    code = d.run()
    if d.error is not None:
        # e.g. from_config -> eligibility.require_valid refused: re-answer `omx eligibility`, or --cancel-only
        what = "daemon not started" if d.engine is None else "daemon stopped on error"
        raise DaemonError(f"{what}: {type(d.error).__name__}: {d.error}")
    if out is not None:
        out.write("OMX daemon stopped (kill switch unchanged, resting orders left as they were)\n")
    return code
