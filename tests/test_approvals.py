import json

import pytest

from omx.approvals import (AgentBudget, ApprovalError, ApprovalQueue, intent_from_record, make_intent,
                           notional_bound_usd)
from omx.audit import AuditLog
from omx.types import Side, to_record

T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def intent(**kw):
    a = dict(coin="ETH", side="buy", size=2.0, source="agent:test")
    a.update(kw)
    return make_intent(**a)


@pytest.fixture
def q(tmp_path):
    clock = Clock()
    audit = AuditLog(tmp_path / "audit.jsonl")
    return ApprovalQueue(tmp_path / "state", audit, ttl_s=600, clock=clock), clock, tmp_path / "audit.jsonl"


def kinds(path):
    return [json.loads(l)["kind"] for l in path.read_text().splitlines()]


def test_approve_lifecycle(q):
    queue, clock, audit = q
    rec = queue.enqueue(intent(), ["ABOVE_APPROVAL_THRESHOLD"], 2000.0)
    assert [r["approval_id"] for r in queue.list()] == [rec["approval_id"]]
    clock.t += 30
    it = queue.approve(rec["approval_id"])
    assert it.side is Side.BUY and it.source == "agent:test"
    with pytest.raises(ApprovalError, match="approved, not pending"):
        queue.approve(rec["approval_id"])            # one-shot
    queue.record_result(rec["approval_id"], True, {"state": "accepted"})
    assert queue.get(rec["approval_id"])["state"] == "executed"
    with pytest.raises(ApprovalError):
        queue.record_result(rec["approval_id"], True, {})
    assert queue.list() == [] and len(queue.list(include_final=True)) == 1
    assert kinds(audit) == ["approval_queued", "approval_approved", "approval_executed"]
    assert AuditLog.verify(audit)[0]


def test_reject(q):
    queue, clock, audit = q
    rec = queue.enqueue(intent(), ["X"], 1.0)
    queue.reject(rec["approval_id"], note="too big")
    r = queue.get(rec["approval_id"])
    assert r["state"] == "rejected" and r["note"] == "too big" and r["decided_by"] == "human:cli"
    with pytest.raises(ApprovalError):
        queue.approve(rec["approval_id"])
    assert kinds(audit) == ["approval_queued", "approval_rejected"]


def test_expiry_with_fake_clock(q):
    queue, clock, audit = q
    rec = queue.enqueue(intent(), ["X"], 1.0)
    clock.t += 599
    assert queue.get(rec["approval_id"])["state"] == "pending"
    clock.t += 1
    with pytest.raises(ApprovalError, match="expired"):
        queue.approve(rec["approval_id"])
    assert queue.get(rec["approval_id"])["state"] == "expired"
    queue.list()
    assert kinds(audit).count("approval_expired") == 1   # logged once, not on every read


def test_unknown_id_and_persistence(q, tmp_path):
    queue, clock, _ = q
    with pytest.raises(ApprovalError, match="unknown"):
        queue.approve("ap-000000000000")
    rec = queue.enqueue(intent(), ["X"], 1.0)
    again = ApprovalQueue(tmp_path / "state", ttl_s=600, clock=clock)    # another process
    assert again.get(rec["approval_id"])["state"] == "pending"
    with __import__("omx.approvals").approvals._locked(queue.path, timeout_s=0.1):   # not left held
        pass


def test_failed_execution_recorded(q):
    queue, _, audit = q
    rec = queue.enqueue(intent(), ["X"], 1.0)
    queue.approve(rec["approval_id"])
    queue.record_result(rec["approval_id"], False, {"error": "venue down"})
    assert queue.get(rec["approval_id"])["state"] == "failed"
    assert kinds(audit)[-1] == "approval_failed"


def test_budget_rolls_over_at_utc_midnight(tmp_path):
    clock = Clock(1_800_057_600.0 - 10)   # 10 s before a UTC midnight
    b = AgentBudget(tmp_path, 1000.0, clock)
    b.add(900.0)
    assert b.would_exceed(200.0) and not b.would_exceed(100.0)
    assert b.remaining() == pytest.approx(100.0)
    clock.t += 20
    assert b.used() == 0.0 and not b.would_exceed(900.0)


def test_make_intent_validation_and_round_trip():
    it = intent(algo="twap", duration_s=60, slices=3, limit_px=105.0)
    assert intent_from_record(to_record(it)) == it
    assert notional_bound_usd(it, 100.0) == pytest.approx(2.0 * 100.5)
    assert notional_bound_usd(intent(limit_px=100.1), 100.0) == pytest.approx(2.0 * 100.1)
    for bad, code in [(dict(coin="ETH/USD"), "BAD_COIN"), (dict(size=0), "BAD_SIZE"),
                      (dict(size=float("nan")), "BAD_SIZE"), (dict(size=True), "BAD_SIZE"),
                      (dict(algo="iceberg"), "BAD_SIDE_OR_ALGO"), (dict(slices=3), "BAD_DURATION_S"),
                      (dict(algo="pov"), "BAD_POV_RATE"), (dict(algo="limit"), "BAD_LIMIT_PX"),
                      (dict(max_slippage_bps=0), "BAD_MAX_SLIPPAGE"), (dict(source="root"), "BAD_SOURCE"),
                      (dict(note="x" * 201), "BAD_NOTE")]:
        with pytest.raises(ValueError, match=code):
            intent(**bad)
    with pytest.raises(ValueError, match="COIN_NOT_ALLOWED"):
        intent(allowed_coins=frozenset({"BTC"}))


def test_budget_reserve_is_atomic_check_and_refund(tmp_path):
    b = AgentBudget(tmp_path, 1000.0, Clock())
    assert b.reserve(600.0) and not b.reserve(500.0) and b.used() == pytest.approx(600.0)
    b.refund(600.0)
    assert b.used() == 0.0 and b.reserve(1000.0) and not b.reserve(0.01)
    b.refund(5000.0)
    assert b.used() == 0.0                                       # never negative


def test_budget_reserve_under_threads(tmp_path):
    import threading
    b = AgentBudget(tmp_path, 1000.0, Clock())
    got = []
    ts = [threading.Thread(target=lambda: got.append(b.reserve(100.0))) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert got.count(True) == 10 and b.used() == pytest.approx(1000.0)


def test_execute_approval_once_and_counts_budget(q, tmp_path):
    from omx.approvals import execute_approval
    from omx.engine_api import IntentReport
    queue, clock, audit = q
    b = AgentBudget(tmp_path / "state", 5000.0, clock)
    rec = queue.enqueue(intent(), ["ABOVE_APPROVAL_THRESHOLD"], 2000.0)
    sent = []
    ok, rep, it = execute_approval(queue, rec["approval_id"], lambda i: sent.append(i) or IntentReport(i.intent_id, "accepted"), b)
    assert ok and rep["state"] == "accepted" and len(sent) == 1 and b.used() == pytest.approx(2000.0)
    with pytest.raises(ApprovalError, match="executed"):
        execute_approval(queue, rec["approval_id"], sent.append, b)
    rec2 = queue.enqueue(intent(), ["X"], 100.0)

    def boom(i):
        raise RuntimeError("venue down")
    with pytest.raises(ApprovalError, match="submit failed"):
        execute_approval(queue, rec2["approval_id"], boom, b)
    assert queue.get(rec2["approval_id"])["state"] == "failed" and b.used() == pytest.approx(2000.0)
