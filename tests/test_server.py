from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import API_HASH, CONAN_CHAT, Harness, conan_gateway, wait_for
from tgdl.config import Settings
from tgdl.events import EventBroadcaster
from tgdl.runtime import RuntimeConfig
from tgdl.server import SettingsStore, create_app

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
                              runtime=runtime, data_dir=tmp_path)
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
