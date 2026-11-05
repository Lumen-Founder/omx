"""File-based request/response channel between thin clients (CLI, MCP) and `omx daemon`.

Layout under <state_dir>/ipc/:
  inbox/<id>.req    client writes <id>.tmp then renames (atomic); daemon claims by renaming to work/
  work/<id>.req     claimed by the daemon, being executed
  outbox/<id>.resp  daemon writes <id>.tmp then renames; the client reads and deletes it

A request carries an id, op, args, client label ("human:cli" or "agent:<name>") and a wall-clock
deadline. The daemon never starts a request past its deadline. On timeout the client tries to
withdraw its inbox file: if that works the request was never executed; if the daemon already
claimed it the outcome is unknown and the caller must check status.

Any request whose JSON contains a private-key-shaped string is refused on both sides.

Trust (R2/B2): the files are a local-user boundary. Any process of the same OS user can write a
request and put any `client` label in it, so the label is not authentication. The daemon
therefore never runs a human-privileged op (approvals approve/reject, unkill, eligibility.record,
audit.repair) from a file alone: it asks for a typed confirmation in its own console, or refuses
with HUMAN_CONSOLE_REQUIRED when it has none. Each request id is executed at most once: the
daemon records processed ids with their deadline in ipc/processed.jsonl and refuses a replay.
The real boundary against an agent that has a shell or file access as the same user is running
that agent as a different OS user.

Liveness (R2/B3): a separate daemon thread writes the heartbeat every second with the time the
step loop last made progress. Fresh heartbeat = daemon running; a fresh heartbeat whose loop
progress is old = daemon busy (e.g. waiting on the venue), not "not running".
"""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import contains_key_like

HEARTBEAT = "daemon.heartbeat"
LOCK = "daemon.lock"
LIVE_S = 5.0                       # heartbeat older than this = no live daemon
BUSY_S = 5.0                       # loop progress older than this (with a fresh heartbeat) = busy
MAX_DEADLINE_S = 900.0             # requests with a later deadline are refused (bounds the replay ledger)
PROCESSED = "processed.jsonl"
POLL_S = 0.02
REQ_ID = re.compile(r"^[0-9]{20}-[0-9a-f]{12}$")
CLIENT = re.compile(r"^(human:cli|agent:[A-Za-z0-9._-]{1,40})$")


class IPCError(RuntimeError):
    pass


class DaemonDown(IPCError):
    pass


class IPCTimeout(IPCError):
    def __init__(self, msg: str, executed: Optional[bool]):
        super().__init__(msg)
        self.executed = executed           # False = withdrawn, never ran; None = unknown


def dirs(state_dir: str | os.PathLike) -> dict[str, Path]:
    root = Path(state_dir) / "ipc"
    d = {k: root / k for k in ("inbox", "work", "outbox")}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _default(o: Any):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=_default)


def _retry(fn, tries: int = 50):
    """Windows refuses to replace/read a file another process has open for a moment: retry briefly."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.01)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        _retry(lambda: os.replace(tmp, path))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_text(path: Path) -> str:
    return _retry(lambda: path.read_text(encoding="utf-8"), tries=20)


# ---------------------------------------------------------------- heartbeat

def write_heartbeat(state_dir: str | os.PathLike, info: dict) -> None:
    write_atomic(Path(state_dir) / HEARTBEAT, dumps({**info, "ts_s": time.time()}))


def read_heartbeat(state_dir: str | os.PathLike) -> Optional[dict]:
    try:
        return json.loads(read_text(Path(state_dir) / HEARTBEAT))
    except (OSError, ValueError):
        return None


def heartbeat_age_s(state_dir: str | os.PathLike, now_s: Optional[float] = None) -> Optional[float]:
    hb = read_heartbeat(state_dir)
    if not hb or not isinstance(hb.get("ts_s"), (int, float)):
        return None
    return (time.time() if now_s is None else now_s) - float(hb["ts_s"])


def daemon_alive(state_dir: str | os.PathLike, max_age_s: float = LIVE_S) -> bool:
    age = heartbeat_age_s(state_dir)
    return age is not None and -max_age_s <= age <= max_age_s


def daemon_state(state_dir: str | os.PathLike, now_s: Optional[float] = None) -> tuple[str, str]:
    """("not_running" | "busy" | "running", human-readable detail)."""
    now_s = time.time() if now_s is None else now_s
    hb = read_heartbeat(state_dir)
    if not daemon_alive(state_dir):
        return "not_running", "heartbeat missing or older than 5 s"
    prog = hb.get("loop_progress_ms") if hb else None
    if isinstance(prog, (int, float)) and now_s * 1000 - prog > BUSY_S * 1000:
        when = time.strftime("%H:%M:%S", time.localtime(prog / 1000))
        return "busy", f"daemon busy: step loop last progressed {now_s - prog / 1000:.0f} s ago ({when})"
    return "running", "running"


class IdLedger:
    """Append-only id ledger (JSONL) with an optional keep-until per id; bounded to max_n ids.
    Used for processed request ids (kept until their deadline) and submitted intent ids."""

    def __init__(self, path: Path, max_n: int = 20_000, clock=time.time):
        self.path, self.max_n, self.clock = Path(path), max_n, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ids: dict[str, Optional[int]] = {}
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    self.ids[str(d["id"])] = d.get("until_ms")
                except (ValueError, KeyError, TypeError):
                    continue          # a torn last line from a crash: skip it
        except FileNotFoundError:
            pass
        self._lines = len(self.ids)
        self._prune(force=True)

    def _prune(self, force: bool = False) -> None:
        now_ms = int(self.clock() * 1000)
        keep = {k: u for k, u in self.ids.items() if u is None or u >= now_ms}
        if len(keep) > self.max_n:
            keep = dict(list(keep.items())[-self.max_n:])
        if force or len(keep) < len(self.ids) or self._lines > 2 * max(len(keep), 1000):
            self.ids = keep
            write_atomic(self.path, "".join(json.dumps({"id": k, "until_ms": u}) + "\n" for k, u in keep.items()))
            self._lines = len(keep)

    def __contains__(self, rid: str) -> bool:
        return rid in self.ids

    def add(self, rid: str, until_ms: Optional[int] = None) -> None:
        self.ids[rid] = until_ms
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": rid, "until_ms": until_ms}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._lines += 1
        if len(self.ids) > self.max_n or self._lines > 2 * max(len(self.ids), 1000):
            self._prune()


# ---------------------------------------------------------------- client

def new_id() -> str:
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:12]}"


def call(state_dir: str | os.PathLike, op: str, args: Optional[dict] = None, timeout_s: float = 15.0,
         client: str = "human:cli", request_id: Optional[str] = None) -> Any:
    """Send one request to the daemon and wait for its result. Raises DaemonDown, IPCTimeout,
    or IPCError(<daemon error text>). request_id: a new_id() the caller already showed the human
    (R3-10: the daemon console shows the same id with the confirmation)."""
    if not CLIENT.match(client):
        raise IPCError("BAD_CLIENT")
    if request_id is not None and not REQ_ID.match(request_id):
        raise IPCError("BAD_REQUEST_ID")
    timeout_s = min(float(timeout_s), MAX_DEADLINE_S)
    if not daemon_alive(state_dir):
        raise DaemonDown("no running OMX daemon (heartbeat missing or older than 5 s): "
                         "start it with `omx daemon` in another terminal")
    rid = request_id or new_id()
    req = {"id": rid, "op": op, "args": args or {}, "client": client,
           "created_ms": int(time.time() * 1000), "deadline_ms": int((time.time() + timeout_s) * 1000)}
    text = dumps(req)
    if contains_key_like(text):
        raise IPCError("KEY_LIKE_REFUSED: the request contains a private-key-shaped string; never send keys")
    d = dirs(state_dir)
    write_atomic(d["inbox"] / f"{rid}.req", text)
    resp_path = d["outbox"] / f"{rid}.resp"
    end = time.monotonic() + timeout_s
    next_check, why = time.monotonic() + 1.0, f"did not answer {op} within {timeout_s:g} s"
    while time.monotonic() < end:
        if resp_path.exists():
            try:
                resp = json.loads(read_text(resp_path))
            except (OSError, ValueError):
                time.sleep(POLL_S)
                continue
            resp_path.unlink(missing_ok=True)
            if not resp.get("ok"):
                raise IPCError(str(resp.get("error") or "daemon error"))
            return resp.get("result")
        if time.monotonic() > next_check:            # stop waiting early if the daemon died
            next_check = time.monotonic() + 1.0
            st, detail = daemon_state(state_dir)
            if st == "not_running":
                why = f"stopped before answering {op}"
                break
            if st == "busy":
                why = f"did not answer {op} within {timeout_s:g} s ({detail})"
        time.sleep(POLL_S)
    try:
        os.remove(d["inbox"] / f"{rid}.req")
        raise IPCTimeout(f"daemon {why}; request withdrawn, not executed", executed=False)
    except FileNotFoundError:
        raise IPCTimeout(f"daemon {why} after claiming it; outcome unknown, check status", executed=None) from None


# ---------------------------------------------------------------- server side

def claim_pending(state_dir: str | os.PathLike) -> list[tuple[str, Optional[dict], Optional[str]]]:
    """Claim every inbox request (FIFO). Returns (id, request or None, error or None)."""
    d = dirs(state_dir)
    out = []
    for p in sorted(d["inbox"].glob("*.req")):
        rid = p.stem
        if not REQ_ID.match(rid):
            p.unlink(missing_ok=True)
            continue
        work = d["work"] / p.name
        try:
            os.replace(p, work)
        except FileNotFoundError:          # withdrawn by its client
            continue
        except PermissionError:            # briefly held open (Windows): take it next loop
            continue
        try:
            text = read_text(work)
        except OSError as e:
            out.append((rid, None, f"BAD_REQUEST: unreadable: {e}"))
            continue
        if contains_key_like(text):
            out.append((rid, None, "KEY_LIKE_REFUSED: request contains a private-key-shaped string"))
            continue
        try:
            req = json.loads(text)
        except ValueError:
            out.append((rid, None, "BAD_REQUEST: not JSON"))
            continue
        if (not isinstance(req, dict) or req.get("id") != rid or not isinstance(req.get("op"), str)
                or not isinstance(req.get("args"), dict) or not CLIENT.match(str(req.get("client")))
                or not isinstance(req.get("deadline_ms"), int)):
            out.append((rid, None, "BAD_REQUEST: malformed"))
            continue
        if req["deadline_ms"] > (time.time() + MAX_DEADLINE_S) * 1000:
            out.append((rid, None, f"BAD_REQUEST: deadline more than {MAX_DEADLINE_S:g} s ahead"))
            continue
        out.append((rid, req, None))
    return out


def respond(state_dir: str | os.PathLike, rid: str, ok: bool, result: Any = None, error: str = "") -> None:
    d = dirs(state_dir)
    body = {"id": rid, "ok": ok, "result": result} if ok else {"id": rid, "ok": False, "error": error}
    write_atomic(d["outbox"] / f"{rid}.resp", dumps(body))
    (d["work"] / f"{rid}.req").unlink(missing_ok=True)


def sweep(state_dir: str | os.PathLike, max_age_s: float = 120.0) -> None:
    """Drop responses nobody collected and stray temp files."""
    now = time.time()
    d = dirs(state_dir)
    for p in list(d["outbox"].iterdir()) + list(d["inbox"].glob("*.tmp")):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def orphaned_work(state_dir: str | os.PathLike) -> list[str]:
    """Requests a previous daemon claimed but never answered (it crashed mid-request)."""
    return [p.stem for p in dirs(state_dir)["work"].glob("*.req") if REQ_ID.match(p.stem)]
