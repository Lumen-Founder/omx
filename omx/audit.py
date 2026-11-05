"""Append-only, hash-chained audit log (control 10).

Each line is one JSON record holding the sha256 of the previous line, so any
edit, deletion or reordering breaks verify(). Records never contain keys or
signatures; callers pass only order/intent/decision data.

Several writers (objects, threads, OS processes) may share one file: every
append takes an exclusive OS lock on the sidecar `<path>.lock` and re-reads the
last line's hash inside it, so the chain stays single and valid.

A crash mid-write can leave a partial final line (no trailing newline).
verify() reports it as TRUNCATED_TAIL, distinct from tampering. repair() moves
the partial bytes to `<path>.tail-<unix_ms>` and appends a durable `repair`
record; append() does the same automatically before writing on top of it.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

GENESIS = "0" * 64
# fsync costs ~70 ms per record on a desktop disk; only records that must survive a
# crash before the next venue action pay it. Others are flushed to the OS at once.
DURABLE_KINDS = {"child_sent", "child_ack", "fill", "kill", "unkill", "cancel", "intent_done", "approval",
                 "repair", "eligibility_attempt"}
FORBIDDEN_KEYS = {"private_key", "secret", "signature", "sig", "mnemonic", "seed", "api_secret"}
TRUNCATED_TAIL = "TRUNCATED_TAIL"
LOCK_TIMEOUT_S = 30.0
_CHUNK = 65536


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _scrub(obj):
    if isinstance(obj, dict):
        bad = FORBIDDEN_KEYS.intersection(k.lower() for k in obj)
        if bad:
            raise ValueError(f"audit record carries forbidden field(s): {sorted(bad)}")
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


# ---------------------------------------------------------------- cross-process lock

@contextmanager
def file_lock(path: str | os.PathLike, timeout_s: float = LOCK_TIMEOUT_S):
    """Exclusive lock on `<path>.lock`, held across threads, objects and OS processes.

    Each call opens its own descriptor, so two objects in one process exclude each
    other too (Windows LockFileEx and BSD flock both bind to the open handle)."""
    lp = str(path) + ".lock"
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            deadline, wait = time.monotonic() + timeout_s, 0.0005
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"audit lock busy: {lp}")
                    time.sleep(wait)
                    wait = min(wait * 2, 0.02)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- tail scanning

def _rfind_nl(fh, before: int) -> int:
    """Index of the last b'\\n' strictly before `before`, or -1."""
    pos = before
    while pos > 0:
        start = max(0, pos - _CHUNK)
        fh.seek(start)
        i = fh.read(pos - start).rfind(b"\n")
        if i >= 0:
            return start + i
        pos = start
    return -1


def _is_record(line: bytes, prev: str) -> bool:
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(rec, dict) and rec.get("prev") == prev and _canon(rec) == line


def _tail(fh) -> tuple[str, int, int]:
    """(hash of the last complete record, offset where a partial tail starts, file size).

    Bytes after the last newline that do not form a valid record chained to the
    previous one are a partial tail. A whole record that only lost its newline is kept."""
    end = fh.seek(0, os.SEEK_END)
    cut = end
    if end:
        fh.seek(end - 1)
        if fh.read(1) != b"\n":
            cut = _rfind_nl(fh, end) + 1
    stop, last = cut - 1, None
    while stop >= 0:
        start = _rfind_nl(fh, stop) + 1
        if start < stop:
            fh.seek(start)
            last = fh.read(stop - start)
            break
        stop = start - 1
    prev = hashlib.sha256(last).hexdigest() if last else GENESIS
    if cut < end:
        fh.seek(cut)
        piece = fh.read(end - cut)
        if _is_record(piece, prev):        # complete record, only the newline is missing
            return hashlib.sha256(piece).hexdigest(), end, end
    return prev, cut, end


def _durable(kind: str, durable: bool | None) -> bool:
    return durable if durable is not None else (kind in DURABLE_KINDS or kind.startswith("approval"))


class AuditLog:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        with self.path.open("rb") as fh:
            return _tail(fh)[0]

    @staticmethod
    def _write(fh, kind: str, data: dict, prev: str, durable: bool) -> str:
        line = _canon({"ts_ms": int(time.time() * 1000), "kind": kind, "prev": prev, "data": _scrub(data)})
        fh.seek(0, os.SEEK_END)
        fh.write(line + b"\n")
        fh.flush()
        if durable:
            os.fsync(fh.fileno())
        return hashlib.sha256(line).hexdigest()

    @staticmethod
    def _ends_nl(fh, end: int) -> bool:
        fh.seek(end - 1)
        return fh.read(1) == b"\n"

    @classmethod
    def _repair_locked(cls, fh, path: Path) -> Path | None:
        prev, cut, end = _tail(fh)
        if cut == end:
            if end and not cls._ends_nl(fh, end):    # whole last record lost only its newline
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            return None
        fh.seek(cut)
        piece = fh.read(end - cut)
        ms = int(time.time() * 1000)
        while (side := path.with_name(f"{path.name}.tail-{ms}")).exists():
            ms += 1
        with side.open("xb") as sf:                  # keep the partial bytes before cutting them
            sf.write(piece)
            sf.flush()
            os.fsync(sf.fileno())
        fh.truncate(cut)
        fh.flush()
        os.fsync(fh.fileno())
        cls._write(fh, "repair", {"tail_file": side.name, "tail_offset": cut, "tail_bytes": len(piece),
                                  "tail_sha256": hashlib.sha256(piece).hexdigest()}, prev, True)
        return side

    def append(self, kind: str, data: dict, durable: bool | None = None) -> str:
        data = _scrub(data)                          # refuse secrets before touching the file
        with self._lock, file_lock(self.path), self.path.open("a+b") as fh:
            prev, cut, end = _tail(fh)
            if cut < end or (end and not self._ends_nl(fh, end)):
                self._repair_locked(fh, self.path)
                prev = _tail(fh)[0]
            self._prev = self._write(fh, kind, data, prev, _durable(kind, durable))
            return self._prev

    @classmethod
    def repair(cls, path: str | os.PathLike) -> Path | None:
        """Move a partial final line to `<path>.tail-<unix_ms>` and append a durable
        `repair` record. Returns the sidecar path, or None if there was nothing to cut."""
        path = Path(path)
        if not path.exists():
            return None
        with file_lock(path), path.open("r+b") as fh:
            return cls._repair_locked(fh, path)

    @staticmethod
    def verify(path: str | os.PathLike) -> tuple[bool, int, str]:
        """Returns (ok, records_checked, message). A partial final line gives
        ok=False with a message starting TRUNCATED_TAIL (crash, not tampering)."""
        path = Path(path)
        with ExitStack() as stack:
            if path.exists():
                try:
                    stack.enter_context(file_lock(path))
                except OSError:
                    pass                             # read-only location: verify without the lock
            return AuditLog._verify(path)

    @staticmethod
    def _verify(path: Path) -> tuple[bool, int, str]:
        prev, n = GENESIS, 0
        with path.open("rb") as fh:
            for i, raw in enumerate(fh, 1):
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                last = not raw.endswith(b"\n")
                if last and not _is_record(line, prev):
                    return False, n, (f"{TRUNCATED_TAIL}: line {i} is a torn partial write after {n} valid "
                                      "records (crash, not tampering); run AuditLog.repair")
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return False, n, f"line {i}: not JSON"
                if rec.get("prev") != prev:
                    return False, n, f"line {i}: chain broken"
                if _canon(rec) != line:
                    return False, n, f"line {i}: not canonical"
                prev = hashlib.sha256(line).hexdigest()
                n += 1
        return True, n, "ok"
