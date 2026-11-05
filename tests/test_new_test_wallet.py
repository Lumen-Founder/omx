"""tools/new_test_wallet.py: new key file, only the address printed, never overwrites (tmp dirs only)."""
import importlib.util
import io
import re
from pathlib import Path

from eth_account import Account

SPEC = importlib.util.spec_from_file_location("new_test_wallet",
                                              Path(__file__).resolve().parents[1] / "tools" / "new_test_wallet.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def run(argv, restrict=lambda p: None):
    out, err = io.StringIO(), io.StringIO()
    return tool.main(argv, out=out, err=err, restrict=restrict), out.getvalue(), err.getvalue()


def test_creates_key_prints_only_the_address(tmp_path):
    path = tmp_path / "sub" / "builder.key"
    restricted = []
    code, out, err = run(["--out", str(path)], restrict=lambda p: restricted.append(p))
    assert code == 0 and err == ""
    assert re.fullmatch(r"0x[0-9a-fA-F]{40}\n", out)
    key = path.read_text(encoding="ascii").strip()
    assert re.fullmatch(r"0x[0-9a-f]{64}", key)
    assert Account.from_key(key).address == out.strip()
    assert key[2:] not in out and restricted == [path]


def test_refuses_to_overwrite(tmp_path):
    path = tmp_path / "k.key"
    path.write_text("keep me", encoding="utf-8")
    code, out, err = run(["--out", str(path)])
    assert code == 2 and out == "" and "already exists" in err
    assert path.read_text(encoding="utf-8") == "keep me"


def test_restriction_warning_is_reported_not_fatal(tmp_path):
    code, out, err = run(["--out", str(tmp_path / "w.key")], restrict=lambda p: "icacls exit 5")
    assert code == 0 and "warning: icacls exit 5" in err and out.startswith("0x")


def test_default_restriction_runs_on_this_os(tmp_path):
    path = tmp_path / "real.key"
    code, out, err = run(["--out", str(path)], restrict=tool.restrict_to_user)
    assert code == 0 and path.exists() and out.startswith("0x")
