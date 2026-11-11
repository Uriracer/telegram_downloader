"""The download queue against the fake Telegram: resume, watchdog, FLOOD_WAIT, storage guard."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterator

import pytest

from conftest import CONAN_CHAT, Harness, conan_gateway, wait_for
from tgdl import downloads
from tgdl.config import Settings
from tgdl.db import DOWNLOADED, ERROR, NEW, QUEUED, IndexedMessage
from tgdl.downloads import BLOCK, DownloadManager, PartState, safe_name, target_path
from tgdl.telegram.fake import FakeGateway, fake_content
from tgdl.telegram.gateway import FloodWait, TelegramError

SIZE = 3 * BLOCK + 1234          # four blocks, the last one short
MSG = 3


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloads, "RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(downloads, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(downloads, "PUBLISH_EVERY", 0.05)
    monkeypatch.setattr(downloads, "FREE_MARGIN", 0)


class DL:
    def __init__(self, tmp_path: Path, **settings: object) -> None:
        gw = conan_gateway(authorized=True)
        gw.files[CONAN_CHAT] = [FakeGateway.file(mid, name, size=SIZE) for mid, name in
                                [(3, "DC-0001.rar"), (4, "DC-0002.part1.rar"), (5, "DC-0002.part2.rar")]]
        base = Settings(telegram_api_id=12345, telegram_api_hash="0123456789abcdef0123456789abcdef",
                        download_dir=str(tmp_path / "dl"), stall_seconds=15)
        self.h = Harness(tmp_path, gw, dataclasses.replace(base, **settings))
        self.gw = gw
        self.h.service.start()
        self.h.wait_state("ready")
        self.src = self.h.db.add_source(CONAN_CHAT, "Conan", "channel")
        self.h.db.upsert_messages(self.src.id, [IndexedMessage(m.msg_id, m.date, "", m.file_name, m.size)
                                                for m in gw.files[CONAN_CHAT]])
        self.manager = DownloadManager(lambda: self.h.settings, self.h.db, self.h.service, self.h.publish)

    @property
    def folder(self) -> Path:
        return Path(self.h.settings.effective_temp_dir) / f"src-{self.src.id}"

    def queue(self, *ids: int) -> None:
        self.h.db.set_state(self.src.id, ids, QUEUED, only_from=(NEW, ERROR))
        self.manager.wake()

    def msg(self, msg_id: int) -> dict:
        m = self.h.db.message(self.src.id, msg_id)
        assert m is not None
        return m

    def wait_msg(self, msg_id: int, state: str, timeout: float = 10.0) -> dict:
        return wait_for(lambda: self.msg(msg_id)["state"] == state and self.msg(msg_id), timeout)

    def close(self) -> None:
        self.h.service.stop()
        self.h.db.close()


@pytest.fixture
def dl(tmp_path: Path) -> Iterator[DL]:
    d = DL(tmp_path)
    yield d
    d.close()


def test_downloads_a_queued_file_in_blocks(dl: DL) -> None:
    dl.manager.start()
    dl.queue(MSG)
    m = dl.wait_msg(MSG, DOWNLOADED)
    path = Path(m["local_path"])
    assert path == dl.folder / "DC-0001.rar"
    assert path.read_bytes() == fake_content(MSG, 0, SIZE)
    assert m["bytes_done"] == SIZE
    assert sorted(o for mid, o in dl.gw.block_reads if mid == MSG) == [0, BLOCK, 2 * BLOCK, 3 * BLOCK]
    assert not list(dl.folder.glob(".*"))          # no .part / .part.json left
    assert any(n == "downloads" for n, _ in dl.h.events)


def test_resumes_only_the_missing_blocks(dl: DL) -> None:
    # A previous run got blocks 0 and 2 (then the power went).
    dl.folder.mkdir(parents=True)
    part = dl.folder / f".{MSG}.part"
    with open(part, "wb") as fh:
        fh.truncate(SIZE)
        fh.seek(0)
        fh.write(fake_content(MSG, 0, BLOCK))
        fh.seek(2 * BLOCK)
        fh.write(fake_content(MSG, 2 * BLOCK, BLOCK))
    PartState(size=SIZE, done={0, 2}).save(dl.folder / f".{MSG}.part.json")
    dl.h.db.set_state(dl.src.id, [MSG], "downloading")      # as left by the crash

    dl.manager.start()                                      # requeues the interrupted file
    m = dl.wait_msg(MSG, DOWNLOADED)
    assert sorted(o for mid, o in dl.gw.block_reads if mid == MSG) == [BLOCK, 3 * BLOCK]
    assert Path(m["local_path"]).read_bytes() == fake_content(MSG, 0, SIZE)


def test_a_part_without_its_state_starts_again(dl: DL) -> None:
    dl.folder.mkdir(parents=True)
    (dl.folder / f".{MSG}.part").write_bytes(b"x" * SIZE)   # tdl-style leftover: cannot be trusted
    dl.manager.start()
    dl.queue(MSG)
    m = dl.wait_msg(MSG, DOWNLOADED)
    assert Path(m["local_path"]).read_bytes() == fake_content(MSG, 0, SIZE)
    assert len([1 for mid, _ in dl.gw.block_reads if mid == MSG]) == 4


def test_watchdog_asks_again_when_telegram_does_not_answer(tmp_path: Path) -> None:
    d = DL(tmp_path)
    try:
        d.h.settings.stall_seconds = 0.2          # below the UI minimum (15 s), for the test
        d.gw.block_faults[(MSG, BLOCK)] = "hang"
        d.gw.block_faults[(MSG, 2 * BLOCK)] = TelegramError("connexió perduda")
        d.manager.start()
        d.queue(MSG)
        m = d.wait_msg(MSG, DOWNLOADED)
        reads = [o for mid, o in d.gw.block_reads if mid == MSG]
        assert reads.count(BLOCK) == 2 and reads.count(2 * BLOCK) == 2
        assert Path(m["local_path"]).read_bytes() == fake_content(MSG, 0, SIZE)
    finally:
        d.close()


def test_flood_wait_is_waited_out_not_counted(dl: DL, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloads, "MAX_FAILURES", 1)
    dl.gw.block_faults[(MSG, 0)] = FloodWait(0)
    dl.manager.start()
    dl.queue(MSG)
    dl.wait_msg(MSG, DOWNLOADED)


def test_gives_up_after_repeated_failures(dl: DL, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloads, "MAX_FAILURES", 2)

    original = dl.gw.read_block

    async def broken(remote, offset, limit):  # type: ignore[no-untyped-def]
        if remote.msg_id == MSG and offset == BLOCK:
            raise TelegramError("error del servidor")
        return await original(remote, offset, limit)

    dl.gw.read_block = broken  # type: ignore[method-assign]
    dl.manager.start()
    dl.queue(MSG, 4)
    m = dl.wait_msg(MSG, ERROR)
    assert "error del servidor" in m["error"] and "2 intents" in m["error"]
    dl.wait_msg(4, DOWNLOADED)                               # the rest of the queue goes on


def test_deleted_message_is_an_error(dl: DL) -> None:
    dl.gw.remove_file(CONAN_CHAT, MSG)
    dl.manager.start()
    dl.queue(MSG)
    assert "ja no existeix" in dl.wait_msg(MSG, ERROR)["error"]


def test_nothing_is_written_while_the_nas_is_missing(tmp_path: Path) -> None:
    sentinel = tmp_path / "nas" / ".tgdl-nas"
    d = DL(tmp_path, storage_sentinel=str(sentinel), download_dir=str(tmp_path / "nas" / "dl"))
    try:
        d.manager.start()
        d.queue(MSG)
        wait_for(lambda: d.manager.status()["blocked"])
        assert d.msg(MSG)["state"] == QUEUED and not (tmp_path / "nas").exists()
        sentinel.parent.mkdir()
        sentinel.touch()
        d.manager.wake()
        d.wait_msg(MSG, DOWNLOADED)
        assert d.manager.status()["blocked"] is None
    finally:
        d.close()


def test_pause_keeps_progress_and_resume_finishes(tmp_path: Path) -> None:
    d = DL(tmp_path, chunks_per_file=1)
    try:
        d.gw.block_delay = 0.2
        d.manager.start()
        d.queue(MSG)
        wait_for(lambda: d.manager.status()["active"] and d.manager.status()["active"][0]["bytes_done"] >= BLOCK)
        d.manager.pause()
        m = d.wait_msg(MSG, QUEUED)
        assert m["bytes_done"] >= BLOCK and d.manager.status()["paused"]
        assert PartState.load(d.folder / f".{MSG}.part.json", SIZE).done
        d.gw.block_delay = 0
        reads_before = len(d.gw.block_reads)
        d.manager.resume()
        d.wait_msg(MSG, DOWNLOADED)
        assert len(d.gw.block_reads) - reads_before < 4     # resumed, not restarted
    finally:
        d.close()


def test_unqueue_stops_an_active_download(tmp_path: Path) -> None:
    d = DL(tmp_path, chunks_per_file=1)
    try:
        d.gw.block_delay = 0.2
        d.manager.start()
        d.queue(MSG)
        wait_for(lambda: d.manager.active() == 1)
        assert d.manager.unqueue(d.src.id, [MSG]) == 1
        d.wait_msg(MSG, NEW)
        wait_for(lambda: d.manager.active() == 0)
    finally:
        d.close()


def test_parallel_files_and_multipart_names(tmp_path: Path) -> None:
    d = DL(tmp_path, max_parallel_files=2)
    try:
        d.gw.block_delay = 0.05
        d.manager.start()
        d.queue(4, 5)
        wait_for(lambda: d.manager.active() == 2)
        d.wait_msg(4, DOWNLOADED)
        d.wait_msg(5, DOWNLOADED)
        assert sorted(p.name for p in d.folder.iterdir()) == ["DC-0002.part1.rar", "DC-0002.part2.rar"]
    finally:
        d.close()


def test_part_state_round_trip(tmp_path: Path) -> None:
    state = PartState(size=10 * BLOCK, done={0, 1, 2, 5, 9})
    path = tmp_path / "x.part.json"
    state.save(path)
    assert '"done":[[0,2],[5,5],[9,9]]' in path.read_text()
    assert PartState.load(path, 10 * BLOCK).done == {0, 1, 2, 5, 9}
    assert PartState.load(path, 11 * BLOCK) is None              # another file
    assert state.missing() == [3, 4, 6, 7, 8]


def test_safe_names_and_collisions(tmp_path: Path) -> None:
    assert safe_name("../../etc/passwd", 1) == "_.._etc_passwd"
    assert safe_name("a:b?.rar", 1) == "a_b_.rar"
    assert safe_name("...", 7) == "7.bin"
    assert len(safe_name("x" * 400 + ".rar", 1).encode()) <= 240
    (tmp_path / "DC-1.rar").write_bytes(b"")
    assert target_path(tmp_path, "DC-1.rar", 42).name == "DC-1 [42].rar"
    assert target_path(tmp_path, "DC-2.rar", 42).name == "DC-2.rar"


def test_stop_starts_nothing_new_and_keeps_the_state(tmp_path: Path) -> None:
    d = DL(tmp_path, chunks_per_file=1, max_parallel_files=1)
    try:
        d.gw.block_delay = 0.2
        d.manager.start()
        d.queue(MSG, 4)
        # Stop once the first block is on disk (before that there is no .part.json to keep).
        wait_for(lambda: d.manager.status()["active"] and d.manager.status()["active"][0]["bytes_done"] >= BLOCK)
        d.manager.stop()
        wait_for(lambda: d.manager.active() == 0)
        d.manager.wake()
        import time
        time.sleep(0.3)
        assert d.manager.active() == 0
        assert d.msg(MSG)["state"] == "downloading" and d.msg(4)["state"] == QUEUED   # resumed on the next start
        assert PartState.load(d.folder / f".{MSG}.part.json", SIZE) is not None
    finally:
        d.close()
