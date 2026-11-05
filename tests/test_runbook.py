"""The testnet runbook (docs/TESTNET_RUNBOOK_KO.md) must match the real CLI and policy."""
import re
import shlex
from pathlib import Path

from omx import cli as cli_mod
from omx.audit import AuditLog
from omx.engine import Engine
from omx.policy import PolicyConfig
from omx.types import Algo, Intent, Side
from omx.venue.sim import SimConfig, SimVenue

from test_daemon import quiet

RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "TESTNET_RUNBOOK_KO.md"


def _command_lines():
    return [ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines()
            if re.search(r"omx\.cli\s", ln) and not ln.lstrip().startswith("#")]


def _argv(line):
    rest = re.search(r"omx\.cli\s+(.*)$", line).group(1).split("#")[0]
    rest = re.sub(r"<[^>]*>", "1", rest).replace("$cfg", "omx.toml")
    return shlex.split(rest.replace("\\", "/"))


def test_every_runbook_command_parses():
    bad = []
    for line in _command_lines():
        try:
            cli_mod._parser().parse_args(_argv(line))
        except SystemExit:
            bad.append(line)
    assert not bad, bad


def test_every_command_names_the_config_and_the_daemon_window_confirms():
    lines = _command_lines()
    assert lines and all("--config" in ln for ln in lines), lines
    assert any(_argv(ln)[-1] == "daemon" for ln in lines)
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "confirm" in text and "I UNDERSTAND" in text


def test_runbook_resting_limit_at_99_3_pct_of_mid_passes_the_default_band(tmp_path):
    assert "0.993" in RUNBOOK.read_text(encoding="utf-8")
    sim = SimVenue(SimConfig(seed=1), coins=quiet())
    eng = Engine(sim, PolicyConfig(min_day_ntl_usd=0.0, min_depth_usd=0.0), AuditLog(tmp_path / "a.jsonl"),
                 state_dir=tmp_path / "s")
    sim.advance(100)
    eng.step()
    mid = sim.snapshot("ETH").mid
    rep = eng.submit(Intent("hu-rb", "ETH", Side.BUY, 0.005, algo=Algo.LIMIT, limit_px=round(0.993 * mid, 1),
                            post_only=True))
    assert rep.state == "working", (rep.state, rep.block_reasons)
