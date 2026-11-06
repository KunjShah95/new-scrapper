"""
Single-instance guard for long runs.

Two scrapes running at once do not crash, which is worse than crashing: they
probe the same projects, download the same documents, and write two conflicting
change journals for the same day. systemd already refuses to overlap the units
it owns, but nothing stops a human from starting a second invocation in another
terminal while the first is still working — and that is exactly when it happens.

The lock is a file holding the holder's pid, hostname and start time. A lock is
only broken automatically when we can *prove* the holder is gone:

  * same host, pid no longer exists  → stale, take it
  * different host                   → fall back to a TTL, because we cannot
                                       check a pid on another machine
  * same host, pid alive             → refuse, and say who holds it
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("gujrera.lock")

# A lock from another machine older than this is assumed dead. Generous because
# a full weekly pass can legitimately run for hours.
FOREIGN_HOST_TTL = 24 * 3600


class LockHeld(RuntimeError):
    """Raised when another run holds the lock."""

    def __init__(self, holder: Dict[str, Any]):
        self.holder = holder
        super().__init__(
            "another run is already in progress: "
            f"pid {holder.get('pid')} on {holder.get('host')} "
            f"since {holder.get('started')} ({holder.get('purpose') or 'no purpose'}). "
            "Wait for it, or stop it — the lock file is "
            f"{holder.get('path')}"
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on the dev box only
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours
        return True
    except OSError:
        return False


class RunLock:
    """Context manager: `with RunLock(path, purpose="daily"): ...`"""

    def __init__(self, path: Path, purpose: str = "", ttl: int = FOREIGN_HOST_TTL):
        self.path = Path(path)
        self.purpose = purpose
        self.ttl = ttl
        self.holder: Optional[Dict[str, Any]] = None
        self._acquired = False

    # ------------------------------------------------------------------ internals ---
    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not raw:
            return None
        try:
            holder = json.loads(raw)
        except ValueError:
            return None
        holder["path"] = str(self.path)
        return holder

    def _is_stale(self, holder: Dict[str, Any]) -> bool:
        if holder.get("host") == socket.gethostname():
            return not _pid_alive(int(holder.get("pid") or 0))
        started = float(holder.get("epoch") or 0)
        return (time.time() - started) > self.ttl

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "epoch": time.time(),
                    "purpose": self.purpose,
                },
                handle,
            )
        return True

    # --------------------------------------------------------------------- public ---
    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._try_create():
            self._acquired = True
            return

        holder = self._read()
        if holder is None:
            # An unreadable or empty lock is not something we can reason about;
            # replace it rather than refusing to run forever.
            log.warning("removing unreadable lock file %s", self.path)
            self.path.unlink(missing_ok=True)
            if self._try_create():
                self._acquired = True
                return
            raise LockHeld(self._read() or {})

        if self._is_stale(holder):
            log.warning(
                "breaking stale lock left by pid %s on %s (%s)",
                holder.get("pid"), holder.get("host"), holder.get("started"),
            )
            self.path.unlink(missing_ok=True)
            if self._try_create():
                self._acquired = True
                return
            raise LockHeld(self._read() or {})

        raise LockHeld(holder)

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            holder = self._read()
            # Only remove the lock if it is still ours.
            if holder and holder.get("pid") == os.getpid() and \
                    holder.get("host") == socket.gethostname():
                self.path.unlink(missing_ok=True)
        except OSError:
            log.debug("could not remove lock file", exc_info=True)
        finally:
            self._acquired = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def holder_info(path: Path) -> Optional[Dict[str, Any]]:
    """What is in the lock file right now, for `--status` style output."""
    lock = RunLock(path)
    holder = lock._read() if Path(path).exists() else None
    if holder and lock._is_stale(holder):
        holder["stale"] = True
    return holder
