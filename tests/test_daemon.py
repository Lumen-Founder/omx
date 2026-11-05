"""omx daemon + file IPC (fix contract R1, item 8). Offline: SimVenue or FakeEngine, no keys."""
import io
import json
import threading
import time

import pytest

from omx import ipc
from omx.audit import AuditLog
from omx.cli import main
from omx.config import load, render
from omx.daemon import Daemon, DaemonError, OwnedEngine
from omx.engine import Engine
from omx.killswitch import is_killed
from omx.mcp_server import DaemonGateway
from omx.venue.sim import CoinSpec, SimConfig, SimVenue

from test_mcp import ACCOUNT, FakeEngine, daemon_running, make_eligible


def quiet():
    spec = dict(vol_bps_sqrt_s=0.0, trade_rate_hz=0.0)
    return (CoinSpec("ETH", 3000.0, 0.1, 4, **spec), CoinSpec("SOL", 150.0, 0.01, 2, level_sz=100.0, **spec))


class SimTimeEngine(Engine):
    """Real Engine whose step() first advances the simulated venue clock (only ever called by the daemon)."""
    ADVANCE_MS = 250

    def step(self):
        self.venue.advance(self.ADVANCE_MS)
        return super().step()


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text(render(ACCOUNT, "testnet", key_path="secrets/agent.key", agent_enabled=True), encoding="utf-8")
    make_eligible(tmp_path / "state")
    return p


def cli(cfgfile, *argv, answers=(), factory=None):
    out, err = io.StringIO(), io.StringIO()
    a = list(answers)
    code = main(["--config", str(cfgfile), *argv], engine_factory=factory or (lambda c: None),
                input_fn=lambda p: a.pop(0), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def sim_factory(sim, cls=SimTimeEngine):
    return lambda c: cls(sim, c.policy, AuditLog(c.audit_path), state_dir=c.state_dir)


def wait_for(fn, timeout_s=20.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def status(cfgfile, iid):
    code, out, err = cli(cfgfile, "status", iid)
    assert code == 0, err
    return json.loads(out)


def submit(cfgfile, *args):
    code, out, err = cli(cfgfile, "submit", *args, "--yes")
    assert code == 0, err
    return json.loads(out[: out.index("\nBuilder fee")])["intent"]["intent_id"]


def test_daemon_loop_drives_twap_to_completion(cfgfile):
    cfg = load(cfgfile)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):
        iid = submit(cfgfile, "ETH", "buy", "1.0", "--algo", "twap", "--duration-s", "10", "--slices", "5")
        rep = wait_for(lambda: (r := status(cfgfile, iid))["state"] == "done" and r)
    assert rep["filled_size"] == pytest.approx(1.0) and rep["children_sent"] == 5
    assert sim.place_calls == 5 and sim.account().position("ETH") == pytest.approx(1.0)
    assert AuditLog.verify(cfg.audit_path)[0]


def test_cli_kill_with_daemon_alive_cancels_resting_orders(cfgfile):
    cfg = load(cfgfile)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):
        iid = submit(cfgfile, "ETH", "buy", "1.0", "--algo", "limit", "--limit-px", "2990")
        wait_for(lambda: status(cfgfile, iid)["children_sent"] == 1)
        time.sleep(0.3)                                   # a few daemon steps: the GTC is live on the venue
        code, out, err = cli(cfgfile, "kill", "--reason", "stop")
        assert code == 0 and '"daemon"' in out, err
        assert status(cfgfile, iid)["state"] == "killed"
    sim.advance(2_000)                                    # cancel latency
    assert sim.account().open_orders == () and is_killed(cfg.state_dir)


def test_cli_kill_with_daemon_down_cancels_via_one_shot_engine(cfgfile):
    cfg = load(cfgfile)
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    with daemon_running(cfg, sim_factory(sim)):
        submit(cfgfile, "ETH", "buy", "1.0", "--algo", "limit", "--limit-px", "2990")
        time.sleep(0.5)
    sim.advance(1_000)
    assert sim.account().open_orders, "precondition: shutdown leaves the OMX order resting"
    assert not is_killed(cfg.state_dir)                   # shutdown does not set the kill switch
    code, out, err = cli(cfgfile, "kill", "--reason", "daemon is down", factory=sim_factory(sim, Engine))
    assert code == 0 and '"one_shot_engine"' in out, err
    sim.advance(2_000)
    assert sim.account().open_orders == ()
    assert AuditLog.verify(cfg.audit_path)[0]


class SlowFake(FakeEngine):
    def submit(self, intent):
        time.sleep(0.05)
        return super().submit(intent)


def test_concurrent_mcp_submissions_cannot_exceed_budget(cfgfile):
    cfg = load(cfgfile)
    eng = SlowFake()
    gw = DaemonGateway(cfg.state_dir, timeout_s=30)
    res = []
    with daemon_running(cfg, lambda c: eng):
        ts = [threading.Thread(target=lambda i=i: res.append(
            gw.submit_order(f"agent:a{i % 3}", {"coin": "ETH", "side": "buy", "size": 9.0}))) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        used = json.loads((cfg.state_dir / "agent_budget.json").read_text())["used_usd"]
    assert len(eng.submitted) == 5 and used <= 5_000.0
    assert sorted(r["status"] for r in res) == ["pending_approval"] * 7 + ["submitted"] * 5


class ThreadRecorder(FakeEngine):
    def __init__(self):
        super().__init__()
        self.threads = set()

    def __getattribute__(self, name):
        if not name.startswith("_") and name not in ("threads",):
            object.__getattribute__(self, "threads").add(threading.get_ident())
        return object.__getattribute__(self, name)


def test_engine_is_only_touched_from_the_daemon_thread(cfgfile):
    cfg = load(cfgfile)
    eng = ThreadRecorder()
    gw = DaemonGateway(cfg.state_dir)
    with daemon_running(cfg, lambda c: eng) as d:
        jobs = [lambda: gw.submit_order("agent:x", {"coin": "ETH", "side": "buy", "size": 1.0}),
                lambda: gw.get_quote("ETH"), lambda: gw.account_summary(),
                lambda: cli(cfgfile, "submit", "ETH", "buy", "1", "--yes"), lambda: cli(cfgfile, "status", "hu-x")]
        ts = [threading.Thread(target=j) for j in jobs * 3]
        [t.start() for t in ts]
        [t.join() for t in ts]
        owner = d.thread_id
        with pytest.raises(RuntimeError, match="ENGINE_THREAD_VIOLATION"):
            d.engine.status("x")                          # the test thread is refused by the proxy
    assert eng.threads == {owner} and len(eng.submitted) == 6


def test_owned_engine_proxy():
    p = OwnedEngine(FakeEngine())
    assert p.status("x").message == "UNKNOWN_INTENT"
    err = []

    def other():
        try:
            p.step()
        except RuntimeError as e:
            err.append(str(e))
    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert err and "ENGINE_THREAD_VIOLATION" in err[0]


def test_single_instance_and_clean_shutdown(cfgfile):
    cfg = load(cfgfile)
    with daemon_running(cfg, lambda c: FakeEngine()):
        assert ipc.daemon_alive(cfg.state_dir)
        second = Daemon(cfg, lambda c: FakeEngine())
        assert second.run() == 1 and isinstance(second.error, DaemonError) and "already runs" in str(second.error)
    assert not (cfg.state_dir / ipc.HEARTBEAT).exists() and not ipc.daemon_alive(cfg.state_dir)
    assert not is_killed(cfg.state_dir)
    recs = [json.loads(line) for line in cfg.audit_path.read_text().splitlines()]
    assert recs[-1]["kind"] == "daemon_stop" and recs[-1]["data"]["kill_switch_set"] is False
    with daemon_running(cfg, lambda c: FakeEngine()):     # the lock was released
        assert ipc.call(cfg.state_dir, "ping", {})["pid"]


def test_daemon_start_refused_without_eligibility(tmp_path):
    p = tmp_path / "omx.toml"
    p.write_text(render(ACCOUNT, "testnet", key_env="OMX_KEY"), encoding="utf-8")
    d = Daemon(load(p), lambda c: FakeEngine())
    assert d.run() == 1 and "ELIGIBILITY_NOT_CONFIRMED" in str(d.error)

    def refusing(c):                                      # what from_config does when require_valid fails
        from omx.eligibility import EligibilityError
        raise EligibilityError(["ELIGIBILITY_EXPIRED"])
    make_eligible(tmp_path / "state")
    code, _, err = cli(p, "daemon", factory=refusing)
    assert code == 1 and "daemon not started" in err and "ELIGIBILITY_EXPIRED" in err
    assert not ipc.daemon_alive(tmp_path / "state")


def test_stale_heartbeat_means_no_daemon(cfgfile):
    cfg = load(cfgfile)
    ipc.write_heartbeat(cfg.state_dir, {"pid": 1})
    hb = json.loads((cfg.state_dir / ipc.HEARTBEAT).read_text())
    hb["ts_s"] -= 6
    (cfg.state_dir / ipc.HEARTBEAT).write_text(json.dumps(hb))
    with pytest.raises(ipc.DaemonDown):
        ipc.call(cfg.state_dir, "ping", {})
    code, _, err = cli(cfgfile, "submit", "ETH", "buy", "1", "--yes")
    assert code == 1 and "start it with `omx daemon`" in err


def test_ipc_timeout_withdraws_unclaimed_request(cfgfile):
    cfg = load(cfgfile)
    ipc.write_heartbeat(cfg.state_dir, {"pid": 1})        # looks alive, nobody serves
    with pytest.raises(ipc.IPCTimeout) as e:
        ipc.call(cfg.state_dir, "ping", {}, timeout_s=0.3)
    assert e.value.executed is False and not list(ipc.dirs(cfg.state_dir)["inbox"].iterdir())


def test_key_like_requests_refused_on_both_sides(cfgfile):
    cfg = load(cfgfile)
    key = "0x" + "1f" * 32
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng) as d:
        with pytest.raises(ipc.IPCError, match="KEY_LIKE_REFUSED"):
            ipc.call(cfg.state_dir, "kill", {"reason": key})
        rid = ipc.new_id()                                # a hand-written request that skips the client check
        req = {"id": rid, "op": "kill", "args": {"reason": key}, "client": "human:cli",
               "deadline_ms": int(time.time() * 1000) + 10_000}
        ipc.write_atomic(ipc.dirs(cfg.state_dir)["inbox"] / f"{rid}.req", json.dumps(req))
        resp = wait_for(lambda: (p := ipc.dirs(cfg.state_dir)["outbox"] / f"{rid}.resp").exists()
                        and json.loads(p.read_text()))
        assert resp["ok"] is False and resp["error"].startswith("KEY_LIKE_REFUSED")
    assert eng.kills == [] and key not in cfg.audit_path.read_text()


def test_expired_and_role_violating_requests_not_executed(cfgfile):
    cfg = load(cfgfile)
    eng = FakeEngine()
    with daemon_running(cfg, lambda c: eng):
        rid = ipc.new_id()
        req = {"id": rid, "op": "kill", "args": {"reason": "late"}, "client": "human:cli",
               "deadline_ms": int(time.time() * 1000) - 1}
        ipc.write_atomic(ipc.dirs(cfg.state_dir)["inbox"] / f"{rid}.req", json.dumps(req))
        resp = wait_for(lambda: (p := ipc.dirs(cfg.state_dir)["outbox"] / f"{rid}.resp").exists()
                        and json.loads(p.read_text()))
        assert resp["error"].startswith("EXPIRED")
        for op, args in (("unkill", {"confirm": "I UNDERSTAND"}), ("approvals.approve", {"approval_id": "ap-x"}),
                         ("submit", {"order": {}})):
            with pytest.raises(ipc.IPCError, match="ROLE_REFUSED"):
                ipc.call(cfg.state_dir, op, args, client="agent:x")
        with pytest.raises(ipc.IPCError, match="ROLE_REFUSED"):
            ipc.call(cfg.state_dir, "agent.kill_switch", {"reason": "x"}, client="human:cli")
        with pytest.raises(ipc.IPCError, match="UNKNOWN_OP"):
            ipc.call(cfg.state_dir, "agent.unkill", {}, client="agent:x")
        with pytest.raises(ipc.IPCError, match="BAD_ARGS"):
            ipc.call(cfg.state_dir, "status", {"intent_id": "x", "extra": 1})
    assert eng.kills == [] and eng.unkills == [] and eng.submitted == []
