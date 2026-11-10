"""Never write to the wrong disk.

When the NAS is not mounted, ``/mnt/Plex`` is just an empty folder on the
container host's root disk (32 GB in the reference setup). Docker still
bind-mounts it, so *inside* the container ``/data`` looks like a mount point
either way: ``os.path.ismount`` cannot tell. A **sentinel file** can: create
it once on the share (``touch /mnt/Plex/.tgdl-nas`` while it is mounted); it
only exists when the share is there.

Every writer (downloads, extraction, library moves) calls
:func:`require_storage` first. At start-up :func:`wait_for_storage` waits for
the share instead of failing (after a power cut the NAS may boot later than
the server).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class StorageUnavailable(RuntimeError):
    """The configured storage is not there (message in Catalan)."""


@dataclass
class StorageStatus:
    ok: bool
    problem: Optional[str]
    sentinel: str
    download_dir: str
    free_bytes: Optional[int]
    filesystem: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def storage_problem(settings: Any) -> Optional[str]:
    """Why nothing may be written now, or None."""
    sentinel = str(getattr(settings, "storage_sentinel", "") or "")
    if sentinel and not os.path.exists(sentinel):
        return (f"El disc de xarxa no està muntat: no hi ha el fitxer sentinella «{sentinel}». "
                "No s'escriurà res fins que hi sigui.")
    download_dir = str(getattr(settings, "download_dir", "") or "")
    if sentinel and download_dir:
        # The sentinel says the share is there; the download folder must be on it.
        root = Path(sentinel).parent
        if not is_within(Path(download_dir), root):
            return (f"La carpeta de baixades «{download_dir}» no és dins del disc on hi ha el sentinella "
                    f"(«{root}»).")
    return None


def require_storage(settings: Any) -> None:
    problem = storage_problem(settings)
    if problem:
        raise StorageUnavailable(problem)


def status(settings: Any) -> StorageStatus:
    problem = storage_problem(settings)
    download_dir = str(getattr(settings, "download_dir", "") or "")
    return StorageStatus(ok=problem is None, problem=problem,
                         sentinel=str(getattr(settings, "storage_sentinel", "") or ""),
                         download_dir=download_dir,
                         free_bytes=None if problem else free_bytes(download_dir),
                         filesystem=filesystem_type(download_dir))


def free_bytes(path: str) -> Optional[int]:
    """Free space where ``path`` is (or would be created)."""
    probe = Path(path) if path else None
    while probe is not None and not probe.exists():
        probe = probe.parent if probe.parent != probe else None
    if probe is None:
        return None
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def filesystem_type(path: str) -> Optional[str]:
    """``nfs4``, ``ext4``… of the mount holding ``path`` (Linux only; shown in the UI)."""
    if sys.platform != "linux" or not path:
        return None
    try:
        target = os.path.realpath(path)
        best, best_type = "", None
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                mount_point = left.split()[4].replace("\\040", " ")
                if is_within(Path(target), Path(mount_point)) and len(mount_point) > len(best):
                    best, best_type = mount_point, right.split()[0]
        return best_type
    except (OSError, IndexError):
        return None


def wait_for_storage(settings_getter: Any, stop: threading.Event, *, interval: float = 10.0) -> bool:
    """Block until :func:`storage_problem` is None (True) or ``stop`` is set (False)."""
    warned = False
    while not stop.is_set():
        problem = storage_problem(settings_getter())
        if problem is None:
            if warned:
                log.info("El disc de xarxa ja hi és; es continua.")
            return True
        if not warned:
            log.warning("%s Esperant…", problem)
            warned = True
        stop.wait(interval)
    return False


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.normcase(os.path.abspath(path))).relative_to(os.path.normcase(os.path.abspath(root)))
        return True
    except ValueError:
        return False
