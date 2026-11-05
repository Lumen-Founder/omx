"""Key FILES for the API (agent) wallet: user-only permissions, never overwritten, never printed.

restrict_to_user() is the one permission helper (tools/new_test_wallet.py imports it from here).
write_new_key_file() is what `omx setup` uses to save an API wallet key the user typed into a hidden
prompt: the key is written to a temporary file in the target directory, restricted to the current
user, then moved into place without replacing an existing file (Windows rename refuses an existing
target; POSIX uses link + unlink). On any failure the temporary file is removed and the target is
left untouched. No message raised here contains the key.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional


def restrict_to_user(path: Path) -> Optional[str]:
    """Best effort: only the current user may read the file. Returns a warning, or None when done."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return None
        except OSError as e:
            return f"could not chmod 600: {e}"
    user = os.environ.get("USERNAME")
    if not user:
        return "USERNAME is not set: restrict the file to your user yourself"
    dom = os.environ.get("USERDOMAIN")
    who = f"{dom}\\{user}" if dom else user
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"icacls failed: {e}"
    return None if r.returncode == 0 else f"icacls exit {r.returncode}: {(r.stdout + r.stderr).strip()[:200]}"


class KeyFileExists(FileExistsError):
    pass


def write_new_key_file(path: Path, account, restrict: Callable[[Path], Optional[str]] = restrict_to_user
                       ) -> Optional[str]:
    """Write `account`'s key (an eth_account LocalAccount) to the NEW file `path` as 0x + 64 hex.
    Refuses an existing file (KeyFileExists). Returns the permission warning, or None."""
    path = Path(path)
    if path.exists():
        raise KeyFileExists(f"{path} already exists; OMX never overwrites a key file")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    os.close(fd)
    moved = False
    try:
        warn = restrict(tmp)                  # restricted while still empty, before the key is written
        fd = os.open(tmp, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            data = ("0x" + bytes(account.key).hex() + "\n").encode("ascii")
            os.write(fd, data)
            os.fsync(fd)
        finally:
            data = None
            os.close(fd)
        if os.name == "nt":
            try:
                os.rename(tmp, path)          # Windows: fails if the target exists, never replaces
            except FileExistsError:
                raise KeyFileExists(f"{path} already exists; OMX never overwrites a key file") from None
        else:
            try:
                os.link(tmp, path)            # POSIX: fails if the target exists
            except FileExistsError:
                raise KeyFileExists(f"{path} already exists; OMX never overwrites a key file") from None
            os.unlink(tmp)
        moved = True
        return warn
    finally:
        if not moved:
            try:
                os.unlink(tmp)
            except OSError:
                pass
