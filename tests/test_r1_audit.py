"""Fix round R1, contract 9: audit lock, TRUNCATED_TAIL, repair (ports review F8a-F8d)."""
import json
import multiprocessing as mp
import os
import threading

import pytest

from omx import audit as audit_mod
from omx.audit import DURABLE_KINDS, TRUNCATED_TAIL, AuditLog
from omx.engine import Engine
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.sim import CoinSpec, SimConfig, SimVenue


def kinds(p):
    return [json.loads(l)["kind"] for l in p.read_bytes().splitlines() if l.strip()]


def _proc_writer(path, tag, n, start):
    start.wait(30)
    log = AuditLog(path)
    for i in range(n):
        log.append("proc", {"tag": tag, "i": i}, durable=False)


# ---------------------------------------------------------------- several writers

def test_two_objects_one_file_keep_one_chain(tmp_path):          # F8a
    p = tmp_path / "audit.jsonl"
    cli_log, mcp_log = AuditLog(p), AuditLog(p)
    for i in range(3):
        cli_log.append("kill_external", {"r": i})
        mcp_log.append("agent_submit", {"r": i})
    assert AuditLog.verify(p) == (True, 6, "ok")


def test_two_objects_many_threads_concurrently(tmp_path):
    p = tmp_path / "audit.jsonl"
    logs = [AuditLog(p), AuditLog(p)]
    go = threading.Barrier(4)

    def work(log, t):
        go.wait()
        for i in range(60):
            log.append("t", {"t": t, "i": i}, durable=False)

    ts = [threading.Thread(target=work, args=(logs[t % 2], t)) for t in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert AuditLog.verify(p) == (True, 240, "ok")


def test_two_os_processes_append_concurrently(tmp_path):
    p = tmp_path / "audit.jsonl"
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    procs = [ctx.Process(target=_proc_writer, args=(str(p), t, 120, start)) for t in ("a", "b")]
    [q.start() for q in procs]
    start.set()
    parent = AuditLog(p)                                           # and a third writer in this process
    for i in range(40):
        parent.append("parent", {"i": i}, durable=False)
    for q in procs:
        q.join(120)
        assert q.exitcode == 0
    ok, n, msg = AuditLog.verify(p)
    assert ok and n == 280, msg
    recs = [json.loads(l) for l in p.read_bytes().splitlines()]
    assert sorted(r["data"]["i"] for r in recs if r["data"].get("tag") == "a") == list(range(120))


def test_engine_and_second_writer_share_file(tmp_path):         # F8b / F8c shape
    p = tmp_path / "audit.jsonl"
    sim = SimVenue(SimConfig(seed=1), coins=(CoinSpec("ETH", 3000.0, 0.1, 4, vol_bps_sqrt_s=0.0,
                                                      trade_rate_hz=0.0),))
    eng = Engine(sim, PolicyConfig(default_coin_notional_usd=50_000, max_account_notional_usd=100_000,
                                   max_child_notional_usd=20_000), AuditLog(p), state_dir=tmp_path / "state")
    other = AuditLog(p)                                            # CLI / MCP gateway writer
    other.append("agent_submit", {"coin": "ETH"})
    eng.submit(Intent("m1", "ETH", Side.BUY, 0.5, algo=Algo.MARKET))
    for _ in range(5):
        sim.advance(100)
        eng.step()
        other.append("approval_note", {"x": 1})
    ok, n, msg = AuditLog.verify(p)
    assert ok and n > 7, msg


def test_lock_sidecar_is_used(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).append("x", {})
    assert (tmp_path / "audit.jsonl.lock").exists()


# ---------------------------------------------------------------- torn tail

def torn(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).append("child_decision", {"x": 1})
    with p.open("ab") as fh:                                       # power loss mid-write
        fh.write(b'{"data":{"x":2},"kind":"child_dec')
    return p


def test_verify_reports_truncated_tail_distinct_from_tamper(tmp_path):
    p = torn(tmp_path)
    ok, n, msg = AuditLog.verify(p)
    assert not ok and n == 1 and msg.startswith(TRUNCATED_TAIL) and "torn" in msg
    q = tmp_path / "t.jsonl"                                        # tampering a middle line is not TRUNCATED_TAIL
    log = AuditLog(q)
    for i in range(3):
        log.append("x", {"i": i})
    body = q.read_bytes().replace(b'"i":1', b'"i":7')
    q.write_bytes(body)
    ok, _, msg = AuditLog.verify(q)
    assert not ok and "chain broken" in msg and not msg.startswith(TRUNCATED_TAIL)


def test_repair_moves_tail_and_appends_durable_record(tmp_path, monkeypatch):
    p = torn(tmp_path)
    syncs = []
    real = os.fsync
    monkeypatch.setattr(audit_mod.os, "fsync", lambda fd: (syncs.append(fd), real(fd)))
    side = AuditLog.repair(p)
    assert side is not None and side.name.startswith("audit.jsonl.tail-")
    assert side.read_bytes() == b'{"data":{"x":2},"kind":"child_dec'
    assert AuditLog.verify(p) == (True, 2, "ok")
    rec = json.loads(p.read_bytes().splitlines()[-1])
    assert rec["kind"] == "repair" and rec["data"]["tail_file"] == side.name and rec["data"]["tail_bytes"] == 33
    assert len(syncs) >= 3                                          # sidecar, truncate, repair record
    assert AuditLog.repair(p) is None                               # nothing left to cut
    assert kinds(p) == ["child_decision", "repair"]


def test_restart_append_on_torn_tail_repairs_first(tmp_path):    # F8d
    p = torn(tmp_path)
    AuditLog(p).append("child_sent", {"x": 3})
    assert AuditLog.verify(p) == (True, 3, "ok")
    assert kinds(p) == ["child_decision", "repair", "child_sent"]
    assert list(tmp_path.glob("audit.jsonl.tail-*"))


def test_existing_object_repairs_tail_written_by_crashed_peer(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p)
    log.append("a", {})
    with p.open("ab") as fh:
        fh.write(b"\x00\x00\x00")                                   # zero-filled tail after power loss
    log.append("b", {})
    assert AuditLog.verify(p) == (True, 3, "ok")


def test_whole_record_missing_newline_is_kept(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).append("a", {"i": 1})
    p.write_bytes(p.read_bytes().rstrip(b"\n"))
    assert AuditLog.verify(p) == (True, 1, "ok")
    assert AuditLog.repair(p) is None
    AuditLog(p).append("b", {"i": 2})
    assert AuditLog.verify(p) == (True, 2, "ok") and kinds(p) == ["a", "b"]


def test_repair_missing_file_is_noop(tmp_path):
    assert AuditLog.repair(tmp_path / "none.jsonl") is None


# ---------------------------------------------------------------- durability and API

def test_child_ack_is_durable(tmp_path, monkeypatch):
    assert "child_ack" in DURABLE_KINDS
    syncs = []
    real = os.fsync
    monkeypatch.setattr(audit_mod.os, "fsync", lambda fd: (syncs.append(fd), real(fd)))
    log = AuditLog(tmp_path / "a.jsonl")
    log.append("child_decision", {})
    assert syncs == []
    log.append("child_ack", {"oid": "1"})
    assert len(syncs) == 1
    log.append("child_decision", {}, durable=True)
    assert len(syncs) == 2


def test_public_api_unchanged(tmp_path):
    p = tmp_path / "a.jsonl"
    log = AuditLog(p)
    h = log.append("x", {"i": 1})
    assert isinstance(h, str) and len(h) == 64 and log._prev == h
    assert AuditLog(p)._prev == h                                    # resume reads the last hash
    with pytest.raises(ValueError):
        log.append("x", {"secret": "nope"})
    assert AuditLog.verify(p) == (True, 1, "ok")
