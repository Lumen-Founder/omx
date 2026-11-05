"""Kill switch (control 9) persisted as a file so another process can trip it.

The flag is the existence of <state_dir>/KILL, a small JSON file holding the reason, time,
source and scope ("omx": cancel OMX orders; "all": cancel every open order on the account).
The scope only ever widens while the switch is on: a later "omx" kill keeps a stored "all", so a
daemon or one-shot engine that reads the file later honours the widest request. The engine re-reads it on every step and right before every child is sent, so a
human can stop trading from the CLI while the engine runs. Clearing requires the exact
confirmation phrase. Without a state_dir the flag lives in memory only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

CONFIRM_PHRASE = "I UNDERSTAND"
FILE_NAME = "KILL"
SCOPES = ("omx", "all")


class KillSwitch:
    def __init__(self, state_dir: Optional[str | os.PathLike] = None):
        self.path = Path(state_dir) / FILE_NAME if state_dir is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mem: Optional[dict] = None

    def info(self) -> Optional[dict]:
        if self.path is None:
            return self._mem
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"reason": "unreadable kill file"}   # fail closed: an unreadable flag is still a kill

    def is_killed(self) -> bool:
        if self.path is None:
            return self._mem is not None
        return self.path.exists()

    def scope(self) -> str:
        """Stored scope; "omx" when the switch is off or the file has none."""
        info = self.info() or {}
        return info.get("scope") if info.get("scope") in SCOPES else "omx"

    def kill(self, reason: str, ts_ms: Optional[int] = None, source: str = "engine", scope: str = "omx") -> dict:
        if scope not in SCOPES:
            raise ValueError(f"kill scope must be one of {SCOPES}")
        if self.is_killed() and self.scope() == "all":
            scope = "all"                                # never narrow a stored "all"
        rec = {"reason": str(reason), "ts_ms": int(ts_ms if ts_ms is not None else time.time() * 1000),
               "source": source, "scope": scope}
        if self.path is None:
            self._mem = rec
            return rec
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        return rec

    def clear(self, confirm: str) -> bool:
        if confirm != CONFIRM_PHRASE:
            return False
        self._mem = None
        if self.path is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        return True


def kill_from_outside(state_dir: str | os.PathLike, reason: str, scope: str = "omx") -> dict:
    """For the CLI / another process: trip the switch of an engine sharing state_dir.
    Returns the stored record; its "scope" is the effective (possibly wider) scope."""
    return KillSwitch(state_dir).kill(reason, source="external", scope=scope)


def is_killed(state_dir: str | os.PathLike) -> bool:
    return KillSwitch(state_dir).is_killed()
