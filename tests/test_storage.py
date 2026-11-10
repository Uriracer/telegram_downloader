from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tgdl import storage
from tgdl.config import Settings


def test_no_sentinel_means_no_check(tmp_path: Path) -> None:
    assert storage.storage_problem(Settings(download_dir=str(tmp_path))) is None


def test_missing_sentinel_blocks_writes(tmp_path: Path) -> None:
    s = Settings(download_dir=str(tmp_path / "Descarregues"), storage_sentinel=str(tmp_path / ".tgdl-nas"))
    problem = storage.storage_problem(s)
    assert problem and "no està muntat" in problem
    with pytest.raises(storage.StorageUnavailable):
        storage.require_storage(s)
    status = storage.status(s)
    assert not status.ok and status.free_bytes is None


def test_present_sentinel_allows_writes(tmp_path: Path) -> None:
    (tmp_path / ".tgdl-nas").touch()
    s = Settings(download_dir=str(tmp_path / "Descarregues" / "telegram"), storage_sentinel=str(tmp_path / ".tgdl-nas"))
    storage.require_storage(s)
    status = storage.status(s)
    assert status.ok and status.free_bytes and status.free_bytes > 0   # measured on the existing parent


def test_download_dir_must_be_on_the_share(tmp_path: Path) -> None:
    share = tmp_path / "nas"
    share.mkdir()
    (share / ".tgdl-nas").touch()
    s = Settings(download_dir=str(tmp_path / "local"), storage_sentinel=str(share / ".tgdl-nas"))
    problem = storage.storage_problem(s)
    assert problem and "no és dins" in problem


def test_wait_for_storage_waits_for_the_share(tmp_path: Path) -> None:
    sentinel = tmp_path / ".tgdl-nas"
    s = Settings(download_dir=str(tmp_path), storage_sentinel=str(sentinel))
    stop = threading.Event()
    threading.Timer(0.2, sentinel.touch).start()
    started = time.monotonic()
    assert storage.wait_for_storage(lambda: s, stop, interval=0.05)
    assert time.monotonic() - started >= 0.15


def test_wait_for_storage_can_be_stopped(tmp_path: Path) -> None:
    s = Settings(download_dir=str(tmp_path), storage_sentinel=str(tmp_path / "never"))
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    assert storage.wait_for_storage(lambda: s, stop, interval=0.05) is False
