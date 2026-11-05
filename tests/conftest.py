import pytest


@pytest.fixture(autouse=True)
def _lock_dir_in_tmp(tmp_path, monkeypatch):
    """Per-account daemon locks (R3-5) go to the test's tmp dir, never to the user's LOCALAPPDATA."""
    monkeypatch.setenv("OMX_LOCK_DIR", str(tmp_path / "omx-locks"))
