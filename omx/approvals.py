"""Human approval queue for agent intents, plus the agent's daily notional budget.

State lives in state_dir as small JSON files, replaced atomically under a
cross-process lock file (the MCP server and the CLI run as separate processes).
Every queue decision is written to the audit log. Agents can only enqueue;
approve / reject are reached from the CLI alone, and an approved intent is executed
by `omx daemon` only (execute_approval), which owns the single engine.

Lifecycle: pending -> approved -> executed | failed
           pending -> rejected
           pending -> expired          (no decision within ttl_s)
           approved -> unknown_after_crash   (the daemon died between approve and its result;
                                              marked at the next daemon start, never re-executed)
An approval is one-shot: an approved record is executed at most once.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .audit import AuditLog, file_lock
from .algos.planner import NATIVE_TWAP_SLIPPAGE_BPS
from .types import Algo, Intent, Side, to_record

Clock = Callable[[], float]
COIN = re.compile(r"^[A-Za-z0-9]{1,15}$")
KEY_LIKE = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
SOURCE = re.compile(r"^(human|agent:[A-Za-z0-9._-]{1,40})$")


class ApprovalError(RuntimeError):
    pass


# ---------------------------------------------------------------- intents

def make_intent(*, coin: str, side: str, size: float, algo: str = "twap", limit_px: Optional[float] = None,
                duration_s: int = 0, slices: int = 1, pov_rate: float = 0.0, post_only: bool = False,
                reduce_only: bool = False, max_slippage_bps: float = 50.0, source: str = "human",
                note: str = "", intent_id: Optional[str] = None, allowed_coins: Optional[frozenset] = None,
                max_slippage_cap_bps: float = 1_000.0) -> Intent:
    """Build an Intent from untrusted input. Raises ValueError with a stable code prefix."""
    def fin(x, name):
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError(f"BAD_{name.upper()}: must be a finite number")
        return float(x)

    if not isinstance(coin, str) or not COIN.match(coin):
        raise ValueError("BAD_COIN: letters and digits only")
    if allowed_coins is not None and coin not in allowed_coins:
        raise ValueError(f"COIN_NOT_ALLOWED:{coin}")
    try:
        side_e = Side(str(side).lower())
        algo_e = Algo(str(algo).lower())
    except ValueError:
        raise ValueError("BAD_SIDE_OR_ALGO: side buy|sell, algo " + "|".join(a.value for a in Algo)) from None
    size = fin(size, "size")
    if not 0 < size <= 1e9:
        raise ValueError("BAD_SIZE: must be > 0")
    if limit_px is not None and not fin(limit_px, "limit_px") > 0:
        raise ValueError("BAD_LIMIT_PX: must be > 0")
    if isinstance(duration_s, bool) or not isinstance(duration_s, int) or not 0 <= duration_s <= 7 * 86_400:
        raise ValueError("BAD_DURATION_S: integer 0..604800")
    if isinstance(slices, bool) or not isinstance(slices, int) or not 1 <= slices <= 10_000:
        raise ValueError("BAD_SLICES: integer 1..10000")
    pov_rate = fin(pov_rate, "pov_rate")
    if not 0 <= pov_rate <= 1 or (algo_e is Algo.POV and pov_rate == 0):
        raise ValueError("BAD_POV_RATE: 0 < pov_rate <= 1 for pov")
    if algo_e in (Algo.TWAP, Algo.POV) and slices > 1 and duration_s == 0:
        raise ValueError("BAD_DURATION_S: sliced algos need duration_s > 0")
    if algo_e is Algo.LIMIT and limit_px is None:
        raise ValueError("BAD_LIMIT_PX: limit orders need limit_px")
    slip = fin(max_slippage_bps, "max_slippage_bps")
    if not 0 < slip <= max_slippage_cap_bps:
        raise ValueError(f"BAD_MAX_SLIPPAGE_BPS: 0 < x <= {max_slippage_cap_bps:g}")
    if not isinstance(post_only, bool) or not isinstance(reduce_only, bool):
        raise ValueError("BAD_FLAGS: post_only / reduce_only must be booleans")
    if not isinstance(note, str) or len(note) > 200 or not note.isprintable():
        raise ValueError("BAD_NOTE: printable text, max 200 chars")
    if KEY_LIKE.search(note):
        raise ValueError("BAD_NOTE: looks like a private key; never send keys to OMX")
    if not SOURCE.match(source):
        raise ValueError("BAD_SOURCE")
    iid = intent_id or (("ag-" if source.startswith("agent:") else "hu-") + uuid.uuid4().hex[:16])
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", iid):
        raise ValueError("BAD_INTENT_ID")
    return Intent(intent_id=iid, coin=coin, side=side_e, size=size, algo=algo_e,
                  limit_px=None if limit_px is None else float(limit_px), duration_s=duration_s, slices=slices,
                  pov_rate=pov_rate, post_only=post_only, reduce_only=reduce_only, max_slippage_bps=slip,
                  source=source, note=note)


def intent_from_record(d: dict) -> Intent:
    d = dict(d)
    d["side"], d["algo"] = Side(d["side"]), Algo(d["algo"])
    return Intent(**d)


def notional_bound_usd(intent: Intent, mid: float) -> float:
    """Upper bound on what the intent can trade: size at mid moved by the full slippage cap (for a native
    TWAP at least the protocol's 3 % suborder slippage, which OMX cannot narrow)."""
    slip = intent.max_slippage_bps
    if intent.algo is Algo.NATIVE_TWAP:
        slip = max(slip, NATIVE_TWAP_SLIPPAGE_BPS)
    px = mid * (1 + slip / 1e4)
    if intent.limit_px is not None and intent.side is Side.BUY:
        px = min(px, intent.limit_px)
    return intent.size * px


# ---------------------------------------------------------------- storage

@contextmanager
def _locked(path: Path, timeout_s: float = 5.0):
    """Exclusive OS lock on <path>.lock (audit.file_lock): released by the OS if a holder dies, so no
    stale-lock guessing. The sidecar file is left in place; it is not the lock, the OS lock is."""
    try:
        with file_lock(path, timeout_s=timeout_s):
            yield
    except TimeoutError:
        raise ApprovalError(f"state file busy: {path}") from None


def _retry(fn, tries: int = 50):
    """Windows briefly refuses to read/replace a file another process has open."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.01)


def _read(path: Path, default):
    if not path.exists():
        return default
    return json.loads(_retry(lambda: path.read_text(encoding="utf-8")))


def _write(path: Path, obj) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}.{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True), encoding="utf-8")
    _retry(lambda: os.replace(tmp, path))


class ApprovalQueue:
    FINAL = {"rejected", "expired", "executed", "failed", "unknown_after_crash"}

    def __init__(self, state_dir: str | Path, audit: Optional[AuditLog] = None, ttl_s: int = 600,
                 clock: Clock = time.time):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "approvals.json"
        self.audit, self.ttl_s, self.clock = audit, ttl_s, clock

    def _log(self, kind: str, rec: dict, **extra) -> None:
        if self.audit is not None:
            self.audit.append(kind, {"approval_id": rec["approval_id"], "state": rec["state"],
                                     "intent": rec["intent"], "reasons": rec["reasons"], **extra})

    def _load_expiring(self) -> dict:
        recs = _read(self.path, {})
        now = self.clock()
        for r in recs.values():
            if r["state"] == "pending" and now >= r["expires_s"]:
                r["state"], r["decided_s"], r["decided_by"] = "expired", now, "clock"
                self._log("approval_expired", r)
        return recs

    def _txn(self):
        return _locked(self.path)

    def enqueue(self, intent: Intent, reasons: list[str], notional_est_usd: float) -> dict:
        now = self.clock()
        rec = {"approval_id": "ap-" + uuid.uuid4().hex[:12], "state": "pending", "created_s": now,
               "expires_s": now + self.ttl_s, "intent": to_record(intent), "reasons": list(reasons),
               "notional_est_usd": round(notional_est_usd, 2), "decided_s": None, "decided_by": None,
               "note": "", "result": None}
        with self._txn():
            recs = self._load_expiring()
            recs[rec["approval_id"]] = rec
            _write(self.path, recs)
        self._log("approval_queued", rec, notional_est_usd=rec["notional_est_usd"])
        return rec

    def list(self, include_final: bool = False) -> list[dict]:
        with self._txn():
            recs = self._load_expiring()
            _write(self.path, recs)
        out = [r for r in recs.values() if include_final or r["state"] not in self.FINAL]
        return sorted(out, key=lambda r: r["created_s"])

    def get(self, approval_id: str) -> dict:
        with self._txn():
            recs = self._load_expiring()
            _write(self.path, recs)
        if approval_id not in recs:
            raise ApprovalError(f"unknown approval id: {approval_id}")
        return recs[approval_id]

    def _decide(self, approval_id: str, new_state: str, by: str, note: str, want: str) -> dict:
        with self._txn():
            recs = self._load_expiring()
            rec = recs.get(approval_id)
            if rec is None:
                _write(self.path, recs)
                raise ApprovalError(f"unknown approval id: {approval_id}")
            if rec["state"] != want:
                _write(self.path, recs)
                raise ApprovalError(f"{approval_id} is {rec['state']}, not {want}")
            rec.update(state=new_state, decided_s=self.clock(), decided_by=by, note=note[:200])
            _write(self.path, recs)
        self._log(f"approval_{new_state}", rec, by=by, note=rec["note"])
        return rec

    def approve(self, approval_id: str, by: str = "human:cli", note: str = "") -> Intent:
        """pending -> approved. Returns the intent the caller must now submit exactly once."""
        return intent_from_record(self._decide(approval_id, "approved", by, note, "pending")["intent"])

    def reject(self, approval_id: str, by: str = "human:cli", note: str = "") -> dict:
        return self._decide(approval_id, "rejected", by, note, "pending")

    def mark_crashed(self) -> list[str]:
        """Daemon start: approvals left "approved" without a result were being submitted when the
        previous daemon died. Their outcome is unknown; they become unknown_after_crash and are
        never executed again (check the intent with `omx status <intent_id>`)."""
        with self._txn():
            recs = self._load_expiring()
            hit = [r for r in recs.values() if r["state"] == "approved" and r.get("result") is None]
            for r in hit:
                r["state"] = "unknown_after_crash"
                r["result"] = {"error": "daemon stopped between approve and submit result; outcome unknown",
                               "intent_id": r["intent"]["intent_id"]}
            _write(self.path, recs)
        for r in hit:
            self._log("approval_unknown_after_crash", r)
        return [r["approval_id"] for r in hit]

    def record_result(self, approval_id: str, ok: bool, result: dict) -> dict:
        with self._txn():
            recs = self._load_expiring()
            rec = recs.get(approval_id)
            if rec is None or rec["state"] != "approved":
                _write(self.path, recs)
                raise ApprovalError(f"{approval_id} is not awaiting execution")
            rec["state"], rec["result"] = ("executed" if ok else "failed"), result
            _write(self.path, recs)
        self._log(f"approval_{rec['state']}", rec, result=result)
        return rec


class AgentBudget:
    """Agent notional per UTC day. Counts agent intents that were sent, whether
    auto-submitted or approved by a human."""

    def __init__(self, state_dir: str | Path, limit_usd: float, clock: Clock = time.time):
        self.path = Path(state_dir) / "agent_budget.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit_usd, self.clock = limit_usd, clock

    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def used(self) -> float:
        d = _read(self.path, {})
        return float(d.get("used_usd", 0.0)) if d.get("day") == self._day() else 0.0

    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.used())

    def would_exceed(self, notional_usd: float) -> bool:
        return self.used() + notional_usd > self.limit_usd

    def _change(self, delta_usd: float, cap: bool) -> Optional[float]:
        with _locked(self.path):
            day = self._day()
            d = _read(self.path, {})
            used = float(d.get("used_usd", 0.0)) if d.get("day") == day else 0.0
            if cap and used + delta_usd > self.limit_usd:
                return None
            used = max(0.0, used + delta_usd)
            _write(self.path, {"day": day, "used_usd": round(used, 6)})
        return used

    def add(self, notional_usd: float) -> float:
        """Count notional without a cap (a human approved it)."""
        return self._change(notional_usd, cap=False)

    def reserve(self, notional_usd: float) -> bool:
        """Atomic check-and-add under the file lock. False = over budget, nothing reserved."""
        return self._change(notional_usd, cap=True) is not None

    def refund(self, notional_usd: float) -> float:
        """Give back a reservation whose intent was rejected or failed."""
        return self._change(-notional_usd, cap=False)


def execute_approval(queue: ApprovalQueue, approval_id: str, submit: Callable[[Intent], object],
                     budget: Optional[AgentBudget] = None) -> tuple[bool, dict, Intent]:
    """pending -> approved -> submit once -> executed | failed. Run by the daemon only.
    Approved agent intents count toward the agent budget (no cap: a human decided)."""
    import dataclasses
    rec = queue.get(approval_id)
    intent = queue.approve(approval_id)
    try:
        rep = submit(intent)
    except Exception as e:
        queue.record_result(approval_id, False, {"error": f"{type(e).__name__}: {e}"[:300]})
        raise ApprovalError(f"submit failed: {e}") from None
    repd = dataclasses.asdict(rep) if dataclasses.is_dataclass(rep) else dict(rep)
    ok = repd.get("state") != "rejected"
    queue.record_result(approval_id, ok, repd)
    if ok and budget is not None and intent.source.startswith("agent:"):
        budget.add(rec["notional_est_usd"])
    return ok, repd, intent
