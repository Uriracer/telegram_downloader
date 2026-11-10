"""Single-instance lock per data folder.

Two processes must never work on the same data folder: both would open the
same Telegram session (Telegram drops one of them) and resume the same
downloads into the same ``.part`` files. The HTTP port cannot be used as the
guard (the app falls back to a random port when the preferred one is taken by
another program), so the running instance holds an OS lock on
``<data_dir>/instance.lock`` and publishes its real port in
``<data_dir>/instance.json``.

The OS releases the lock when the process exits or crashes, so a stale lock
file never blocks a new start.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import IO, Any, Optional

from .config import atomic_write_json

log = logging.getLogger(__name__)

LOCK_FILE = "instance.lock"
INFO_FILE = "instance.json"


class InstanceLock:
    """An acquired lock; keep the object alive for the life of the process."""

    def __init__(self, handle: IO[bytes], data_dir: Path) -> None:
        self._handle: Optional[IO[bytes]] = handle
        self.data_dir = data_dir

    @property
    def info_path(self) -> Path:
        return self.data_dir / INFO_FILE

    def publish(self, port: int) -> None:
        """Record the port this instance serves on (read by later launches)."""
        try:
            atomic_write_json(self.info_path, {"port": int(port), "pid": os.getpid()})
        except OSError as exc:
            log.warning("Cannot write %s: %s", self.info_path, exc)

    def release(self) -> None:
        """Remove the published info and release the lock (idempotent)."""
        if self._handle is None:
            return
        try:
            self.info_path.unlink()
        except OSError:
            pass
        handle, self._handle = self._handle, None
        try:
            _unlock(handle)
        except OSError:
            pass
        handle.close()


def _lock(handle: IO[bytes]) -> None:
    """Non-blocking exclusive lock on byte 0; raises OSError if held elsewhere."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - exercised on POSIX only
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_instance_lock(data_dir: str | os.PathLike[str]) -> Optional[InstanceLock]:
    """Take the lock for ``data_dir``; ``None`` if another process holds it."""
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / LOCK_FILE, "a+b")  # noqa: SIM115 - kept open while locked
    try:
        _lock(handle)
    except OSError:
        handle.close()
        return None
    return InstanceLock(handle, folder)


def running_instance_port(data_dir: str | os.PathLike[str]) -> Optional[int]:
    """Port published by the instance holding the lock, if any."""
    try:
        with open(Path(data_dir) / INFO_FILE, encoding="utf-8") as fh:
            data: Any = json.load(fh)
        port = int(data.get("port")) if isinstance(data, dict) else 0
    except (OSError, ValueError, TypeError):
        return None
    return port if 0 < port <= 65535 else None
