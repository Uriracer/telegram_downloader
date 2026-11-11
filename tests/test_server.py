from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import API_HASH, CONAN_CHAT, Harness, conan_gateway, skyhook_transport, wait_for
from tgdl.config import Settings
from tgdl.events import EventBroadcaster
from tgdl.runtime import RuntimeConfig
from tgdl.server import SettingsStore, create_app
from tgdl.tvdb import TvdbClient

ORIGIN = {"Origin": "http://testserver"}


class Env:
    def __init__(self, tmp_path: Path, *, server_mode: bool = False, overrides: dict | None = None) -> None:
        self.saved: list[Settings] = []
        self.store = SettingsStore(loader=lambda: Settings(telegram_api_id=12345, telegram_api_hash=API_HASH),
                                   saver=self.saved.append, overrides=overrides or {})
        self.harness = Harness(tmp_path, conan_gateway(), self.store.get())
        self.harness.settings = None  # type: ignore[assignment]
        self.harness.service._settings = self.store.get
        self.events = EventBroadcaster()
        runtime = (RuntimeConfig(mode="server", auth_required=True, allowed_hosts=None) if server_mode
                   else RuntimeConfig(mode="local", allowed_hosts=frozenset({"testserver", "127.0.0.1"})))
        self.app = create_app(self.store, db=self.harness.db, telegram=self.harness.service, events=self.events,
                              runtime=runtime, data_dir=tmp_path,
                              tvdb=TvdbClient(self.harness.db, transport=skyhook_transport()))
        self.harness.service.start()
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.client.close()
        self.harness.service.stop()
        self.harness.db.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.close()


def login_telegram(e: Env) -> None:
    c = e.client
    wait_for(lambda: c.get("/api/telegram/status").json()["state"] == "logged_out")
    assert c.post("/api/telegram/code", json={"phone": "+34600000000"}, headers=ORIGIN).json()["state"] == "code"
    assert c.post("/api/telegram/code/verify", json={"code": "12345"}, headers=ORIGIN).json()["state"] == "ready"


def test_health_and_ui(env: Env) -> None:
    health = env.client.get("/api/health").json()
    assert health["app"] == "telegram-downloader" and health["storage"]["ok"] is True
    page = env.client.get("/")
    assert page.status_code == 200 and "Telegram Downloader" in page.text


def test_settings_hide_secrets_and_validate(env: Env) -> None:
    data = env.client.get("/api/settings").json()
    assert "telegram_api_hash" not in data and "password_hash" not in data
    assert data["telegram_api_hash_set"] is True
    bad = env.client.put("/api/settings", json={"max_parallel_files": 50}, headers=ORIGIN)
    assert bad.status_code == 422 and "max_parallel_files" in bad.json()["detail"]
    ok = env.client.put("/api/settings", json={"chunks_per_file": 8, "telegram_api_hash": ""}, headers=ORIGIN)
    assert ok.status_code == 200 and ok.json()["chunks_per_file"] == 8
    assert env.saved[-1].telegram_api_hash == API_HASH      # empty hash = keep the stored one


def test_env_forced_settings_are_locked(tmp_path: Path) -> None:
    e = Env(tmp_path, overrides={"download_dir": "/data/Descarregues/telegram"})
    try:
        data = e.client.get("/api/settings").json()
        assert data["locked"] == ["download_dir"] and data["download_dir"] == "/data/Descarregues/telegram"
        e.client.put("/api/settings", json={"download_dir": "/elsewhere"}, headers=ORIGIN)
        assert e.client.get("/api/settings").json()["download_dir"] == "/data/Descarregues/telegram"
    finally:
        e.close()


def test_cross_site_requests_are_refused(env: Env) -> None:
    r = env.client.post("/api/telegram/qr", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_qr_login_and_svg(env: Env) -> None:
    c = env.client
    wait_for(lambda: c.get("/api/telegram/status").json()["state"] == "logged_out")
    assert c.get("/api/telegram/qr.svg").status_code == 404
    assert c.post("/api/telegram/qr", headers=ORIGIN).json()["state"] == "qr"
    svg = c.get("/api/telegram/qr.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b'xmlns="http://www.w3.org/2000/svg"' in svg.content     # loads as <img>


def test_login_errors_are_400_in_catalan(env: Env) -> None:
    c = env.client
    wait_for(lambda: c.get("/api/telegram/status").json()["state"] == "logged_out")
    c.post("/api/telegram/code", json={"phone": "+34600000000"}, headers=ORIGIN)
    r = c.post("/api/telegram/code/verify", json={"code": "00000"}, headers=ORIGIN)
    assert r.status_code == 400 and r.json()["detail"] == "El codi no és correcte."


def test_dialogs_require_login(env: Env) -> None:
    wait_for(lambda: env.client.get("/api/telegram/status").json()["state"] == "logged_out")
    assert env.client.get("/api/telegram/dialogs").status_code == 409


def test_sources_flow(env: Env) -> None:
    login_telegram(env)
    c = env.client
    dialogs = c.get("/api/telegram/dialogs").json()["dialogs"]
    conan = next(d for d in dialogs if d["id"] == CONAN_CHAT)
    assert conan["source_id"] is None
    assert c.post("/api/sources", json={"chat_id": 1}, headers=ORIGIN).status_code == 404

    src = c.post("/api/sources", json={"chat_id": CONAN_CHAT, "options": {"language": "ca"}}, headers=ORIGIN).json()
    assert src["title"] == conan["title"]
    wait_for(lambda: c.get(f"/api/sources/{src['id']}").json()["indexed_at"])
    assert c.get("/api/telegram/dialogs").json()["dialogs"][0]["source_id"] == src["id"]

    page = c.get(f"/api/sources/{src['id']}/messages", params={"limit": 2}).json()
    assert page["total"] == 7 and len(page["items"]) == 2
    assert c.get(f"/api/sources/{src['id']}/messages", params={"q": "Nus"}).json()["total"] == 1
    assert c.get(f"/api/sources/{src['id']}/messages", params={"photos": True}).json()["total"] == 8
    assert c.get(f"/api/sources/{src['id']}/messages", params={"state": "bogus"}).status_code == 422

    upd = c.patch(f"/api/sources/{src['id']}", json={"options": {"mode": "library", "language": "es"}},
                  headers=ORIGIN).json()
    assert upd["options"]["mode"] == "library" and upd["options"]["language"] == "es"
    assert upd["options"]["kind"] == "series"   # untouched options are kept

    assert c.post(f"/api/sources/{src['id']}/index", json={"full": True}, headers=ORIGIN).status_code == 200
    wait_for(lambda: c.get(f"/api/sources/{src['id']}").json()["indexing"] is None)
    assert c.delete(f"/api/sources/{src['id']}", headers=ORIGIN).json() == {"ok": True}
    assert c.get(f"/api/sources/{src['id']}").status_code == 404


def test_server_mode_requires_login(tmp_path: Path) -> None:
    e = Env(tmp_path, server_mode=True)
    try:
        c = e.client
        assert c.get("/api/settings").status_code == 401
        assert set(c.get("/api/health").json()) == {"ok", "version", "app"}   # nothing else before login
        assert c.post("/api/auth/setup", json={"password": "curta"}, headers=ORIGIN).status_code == 400
        assert c.post("/api/auth/setup", json={"password": "una-contrasenya"}, headers=ORIGIN).status_code == 200
        assert c.get("/api/settings").status_code == 200                       # session cookie set
        key = c.get("/api/settings").json()["api_key"]
        c.cookies.clear()
        assert c.get("/api/settings").status_code == 401
        assert c.get("/api/settings", headers={"X-Api-Key": key}).status_code == 200
    finally:
        e.close()


def test_check_import_and_reconcile(tmp_path: Path) -> None:
    nas = tmp_path / "Plex"
    season = nas / "Series" / "Detectiu Conan (1996) [tvdbid-72454]" / "Season 17"
    season.mkdir(parents=True)
    (season / "501. El xoc entre el vermell i el negre.mkv").write_bytes(b"video")   # 13x501 is already there
    zips = nas / "_conan" / "zips"
    zips.mkdir(parents=True)
    (nas / ".tgdl-nas").write_text("")
    (zips / "1111111111_9_DC-1004 (Nus) [5A052F3A].rar").write_bytes(b"12345")   # tdl copy, complete
    (zips / "1111111111_7_DC-928 (El vermell de l'amor) [70BF1E30].part1.rar").write_bytes(b"12")  # truncated
    (zips / "2222222222_3_Altre.rar").write_bytes(b"1")                          # another channel
    e = Env(tmp_path, overrides={"library_tv_dir": str(nas / "Series"), "storage_sentinel": str(nas / ".tgdl-nas"),
                                 "download_dir": str(nas / "Descarregues" / "telegram")})
    try:
        login_telegram(e)
        c = e.client
        src = c.post("/api/sources", json={"chat_id": CONAN_CHAT}, headers=ORIGIN).json()
        sid = src["id"]
        wait_for(lambda: c.get(f"/api/sources/{sid}").json()["indexed_at"])
        e.harness.db.execute("UPDATE messages SET size=5 WHERE msg_id=9")

        page = c.get(f"/api/sources/{sid}/messages").json()["items"]
        assert {i["file_name"][:6]: i["episode"] for i in page}["DC-928"] == "928"

        # Without the TVDB id: gaps only, no library.
        report = c.get(f"/api/sources/{sid}/review").json()
        assert report["tvdb"] is None and report["library"]["folder"] is None
        assert c.post(f"/api/sources/{sid}/reconcile", headers=ORIGIN).status_code == 409

        c.patch(f"/api/sources/{sid}", json={"options": {"tvdb_id": "72454", "numbering": "absolute"}},
                headers=ORIGIN)
        assert e.harness.db.message(sid, 4)["state"] == "in_library"      # reconciled on its own
        report = c.get(f"/api/sources/{sid}/review").json()
        assert report["tvdb"]["title"] == "Detective Conan" and report["library"]["episodes"] == 1
        assert report["summary"]["unrecognized"] == 0 and report["summary"]["in_library"] == 1
        rel = next(r for r in report["releases"] if r["absolute"] == 424)
        assert rel["label"] == "S14E35 · 0424"

        bad = c.post(f"/api/sources/{sid}/import", json={"path": str(tmp_path)}, headers=ORIGIN)
        assert bad.status_code == 422                                     # outside the NAS
        dry = c.post(f"/api/sources/{sid}/import", json={"path": str(zips), "dry_run": True}, headers=ORIGIN).json()
        assert dry["imported"] == 1 and dry["other_chat"] == 1 and len(dry["size_mismatch"]) == 1
        assert e.harness.db.message(sid, 9)["state"] == "new"
        done = c.post(f"/api/sources/{sid}/import", json={"path": str(zips)}, headers=ORIGIN).json()
        assert done["imported"] == 1
        imported = e.harness.db.message(sid, 9)
        assert imported["state"] == "downloaded" and imported["local_path"].endswith("[5A052F3A].rar")
        again = c.post(f"/api/sources/{sid}/import", json={"path": str(zips)}, headers=ORIGIN).json()
        assert again["imported"] == 0 and again["already"] == 1

        result = c.post(f"/api/sources/{sid}/reconcile", headers=ORIGIN).json()
        assert result["marked"] == 0 and result["episodes"] == 1

        # A folder without the [tvdbid] tag, chosen by hand with the browser.
        other = nas / "Series" / "Conan en català" / "Temporada 29"
        other.mkdir(parents=True)
        (other / "12 - Nus.mkv").write_bytes(b"v")                             # S29E12 = 1004
        listing = c.get("/api/fs", params={"path": str(nas / "Series")}).json()
        assert [d["name"] for d in listing["dirs"]] == ["Conan en català", "Detectiu Conan (1996) [tvdbid-72454]"]
        assert c.get("/api/fs", params={"path": str(tmp_path)}).json()["path"] == str(nas)   # cannot leave the NAS
        assert c.get("/api/fs", params={"path": str(other)}).json()["videos"] == 1
        c.patch(f"/api/sources/{sid}", json={"options": {"library_folder": str(nas / "Series" / "Conan en català")}},
                headers=ORIGIN)
        assert e.harness.db.message(sid, 9)["state"] == "in_library"      # the tdl copy is not needed any more
        assert c.get("/api/tvdb/72454").json()["title"] == "Detective Conan"
        assert c.get("/api/tvdb/1").status_code == 404
    finally:
        e.close()


def test_queue_missing_unqueue_and_pause(env: Env) -> None:
    c = env.client
    login_telegram(env)
    src = c.post("/api/sources", json={"chat_id": CONAN_CHAT}, headers=ORIGIN).json()
    wait_for(lambda: c.get(f"/api/sources/{src['id']}").json()["indexing"] is None
             and c.get(f"/api/sources/{src['id']}").json()["files"] == 7)
    url = f"/api/sources/{src['id']}"
    assert c.post(f"{url}/queue", json={}, headers=ORIGIN).status_code == 422

    # Every recognised, complete episode (the two volumes of DC-928 together); never the poster.
    res = c.post(f"{url}/queue", json={"missing": True}, headers=ORIGIN).json()
    assert res["queued"] == 7 and res["source"]["counts"] == {"queued": 7}
    assert c.post(f"{url}/queue", json={"missing": True}, headers=ORIGIN).json()["queued"] == 0   # already

    assert c.post(f"{url}/unqueue", json={"msg_ids": [7, 8]}, headers=ORIGIN).json()["unqueued"] == 2
    status = c.get("/api/downloads").json()
    assert status["queued"] == 5 and status["queued_bytes"] == 5 * 500_000_000 and status["active"] == []
    assert c.post(f"{url}/queue", json={"msg_ids": [7]}, headers=ORIGIN).json()["queued"] == 1

    assert c.post("/api/downloads/pause", headers=ORIGIN).json()["paused"] is True
    assert env.harness.db.get_value("downloads_paused") is True
    assert c.post("/api/downloads/resume", headers=ORIGIN).json()["paused"] is False
