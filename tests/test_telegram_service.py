from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CONAN_CHAT, CONAN_FILES, Harness, conan_gateway, wait_for
from tgdl import db as dbm
from tgdl.config import Settings
from tgdl.telegram import fake as fakemod
from tgdl.telegram.fake import FakeGateway
from tgdl.telegram.gateway import ChatUnavailable, LoginError, NotAuthorized


def test_unconfigured_without_api_credentials(tmp_path: Path) -> None:
    h = Harness(tmp_path, conan_gateway(), Settings())
    h.service.start()
    try:
        h.wait_state("unconfigured")
        assert h.service.status()["configured"] is False
        with pytest.raises(Exception):
            h.service.start_qr()
    finally:
        h.service.stop()


def test_phone_login(harness: Harness) -> None:
    harness.wait_state("logged_out")
    with pytest.raises(LoginError):
        harness.service.send_code("600000000")           # no country prefix
    assert harness.service.send_code("+34 600 00 00 00")["state"] == "code"
    assert harness.service.status()["phone"] == "+34600000000"
    with pytest.raises(LoginError, match="codi"):
        harness.service.submit_code("99999")
    assert harness.service.submit_code("12-345")["state"] == "ready"   # non-digits ignored
    me = harness.service.status()["me"]
    assert me["name"] == "Prova" and me["premium"] is True


def test_phone_login_with_two_factor(tmp_path: Path, configured: Settings) -> None:
    h = Harness(tmp_path, conan_gateway(password="secret"), configured)
    h.service.start()
    try:
        h.wait_state("logged_out")
        h.service.send_code("+34600000000")
        status = h.service.submit_code("12345")
        assert status["state"] == "password" and status["password_hint"] == "pista de prova"
        with pytest.raises(LoginError, match="contrasenya"):
            h.service.submit_password("wrong")
        assert h.service.submit_password("secret")["state"] == "ready"
    finally:
        h.service.stop()


def test_qr_login(harness: Harness) -> None:
    harness.wait_state("logged_out")
    status = harness.service.start_qr()
    assert status["state"] == "qr" and status["qr_url"].startswith("tg://login?token=")
    harness.on_loop(harness.gateway.scan_qr)
    harness.wait_state("ready")
    assert harness.service.status()["qr_url"] is None


def test_qr_is_renewed_when_it_expires(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fakemod, "QR_LIFETIME", 0.2)
    harness.wait_state("logged_out")
    first = harness.service.start_qr()["qr_url"]
    wait_for(lambda: harness.service.status()["qr_url"] not in (None, first), timeout=3)
    assert harness.service.state == "qr"
    harness.on_loop(harness.gateway.scan_qr)
    harness.wait_state("ready")


def test_qr_with_two_factor(tmp_path: Path, configured: Settings) -> None:
    h = Harness(tmp_path, conan_gateway(password="secret"), configured)
    h.service.start()
    try:
        h.wait_state("logged_out")
        h.service.start_qr()
        h.on_loop(h.gateway.scan_qr)
        h.wait_state("password")
        assert h.service.submit_password("secret")["state"] == "ready"
    finally:
        h.service.stop()


def test_cancel_login(harness: Harness) -> None:
    harness.wait_state("logged_out")
    harness.service.start_qr()
    assert harness.service.cancel_login()["state"] == "logged_out"


def test_saved_session_is_restored_and_indexed(tmp_path: Path, configured: Settings) -> None:
    """After a power cut: the session is reused and due sources are indexed without anyone clicking."""
    seed = Harness(tmp_path, conan_gateway(), configured)
    src = seed.db.add_source(CONAN_CHAT, "Conan", "channel")
    seed.db.close()

    h = Harness(tmp_path, conan_gateway(authorized=True), configured)
    h.service.start()
    try:
        h.wait_state("ready")
        assert "logged_out" not in h.states()
        wait_for(lambda: (h.db.source(src.id) or src).indexed_at)
        assert h.db.source(src.id).counts == {"new": len(CONAN_FILES) - 1}   # the cover is not counted
    finally:
        h.service.stop()
        h.db.close()


def test_dialogs_have_full_names(harness: Harness) -> None:
    with pytest.raises(NotAuthorized):
        harness.service.dialogs()
    harness.login()
    titles = [d["title"] for d in harness.service.dialogs()]
    assert "El Detectiu Conan - Còpia de seguretat completa (1996-2024) en català" in titles


def test_index_incremental_then_full(harness: Harness) -> None:
    harness.login()
    src = harness.db.add_source(CONAN_CHAT, "Conan", "channel")
    assert harness.service.index_source(src.id)
    wait_for(lambda: harness.service.indexing(src.id) is None and harness.db.source(src.id).indexed_at)
    s = harness.db.source(src.id)
    assert s.last_msg_id == 9 and s.index_error is None
    total, items = harness.db.messages(src.id)
    assert total == 7
    assert {i["file_name"] for i in items} >= {"DC-1004 (Nus) [5A052F3A].rar",
                                                "DC-928 (El vermell de l'amor) [70BF1E30].part2.rar"}

    harness.db.set_state(src.id, [3], dbm.IN_LIBRARY)
    files = harness.gateway.files[CONAN_CHAT]
    files.append(FakeGateway.file(10, "DC-1005 (Nou) [ABCDEF12].rar"))
    files[:] = [f for f in files if f.msg_id != 4]          # message 4 deleted in the channel

    seen: list[int] = []
    real_iter = harness.gateway.iter_files

    async def spy(chat_id: int, min_id: int = 0):
        seen.append(min_id)
        async for m in real_iter(chat_id, min_id):
            yield m
    harness.gateway.iter_files = spy  # type: ignore[method-assign]

    harness.service.index_source(src.id)
    wait_for(lambda: harness.db.source(src.id).last_msg_id == 10 and harness.service.indexing(src.id) is None)
    assert seen == [9]                                        # incremental: only newer messages
    assert harness.db.messages(src.id)[0] == 8                # deleted message not noticed yet

    harness.service.index_source(src.id, full=True)
    wait_for(lambda: harness.service.indexing(src.id) is None and harness.db.messages(src.id)[0] == 7)
    assert seen[-1] == 0
    assert harness.db.message(src.id, 3)["state"] == dbm.IN_LIBRARY   # state survives reindexing


def test_index_error_is_kept_and_reported(harness: Harness) -> None:
    harness.login()
    src = harness.db.add_source(CONAN_CHAT, "Conan", "channel")
    harness.gateway.fail_next_index = ChatUnavailable("Aquest xat no existeix o el compte ja no hi té accés.")
    harness.service.index_source(src.id)
    wait_for(lambda: harness.db.source(src.id).index_error)
    assert harness.db.source(src.id).indexed_at is None
    payloads = [p for n, p in harness.events if n == "source"]
    assert payloads[-1]["indexing"] is None and "accés" in payloads[-1]["index_error"]


def test_index_twice_is_refused_while_running(harness: Harness) -> None:
    harness.login()
    src = harness.db.add_source(CONAN_CHAT, "Conan", "channel")
    gate = {"open": False}
    real_iter = harness.gateway.iter_files

    async def slow(chat_id: int, min_id: int = 0):
        import asyncio
        while not gate["open"]:
            await asyncio.sleep(0.01)
        async for m in real_iter(chat_id, min_id):
            yield m
    harness.gateway.iter_files = slow  # type: ignore[method-assign]
    assert harness.service.index_source(src.id) is True
    assert harness.service.index_source(src.id) is False
    gate["open"] = True
    wait_for(lambda: harness.service.indexing(src.id) is None)


def test_logout(harness: Harness) -> None:
    harness.login()
    assert harness.service.logout()["state"] in ("connecting", "logged_out")
    harness.wait_state("logged_out")
    assert harness.service.status()["me"] is None
    assert "log_out" in harness.gateway.calls


def test_premium_change_is_noticed(harness: Harness) -> None:
    harness.login()
    harness.gateway.set_premium(False)
    harness.service.call(harness.service._refresh_me)
    assert harness.service.status()["me"]["premium"] is False
