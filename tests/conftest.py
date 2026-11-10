"""Shared fixtures. Telegram is never contacted: tests use :class:`tgdl.telegram.fake.FakeGateway`."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tgdl.config import Settings  # noqa: E402
from tgdl.db import Database  # noqa: E402
from tgdl.telegram.fake import FakeGateway  # noqa: E402
from tgdl.telegram.service import TelegramService  # noqa: E402

API_HASH = "0123456789abcdef0123456789abcdef"
CONAN_CHAT = -1001111111111

#: Real file names of the Detective Conan channel (four naming eras, multipart, cover).
CONAN_FILES = [
    (2, "poster.jpg"),
    (3, "Animelliure_DC_001_Multi_Audio+Subs_Forzados_V2_96D508D8.rar"),
    (4, "13x501_El_xoc_entre_el_vermell_i_el_negre_La_sospita_D4BAF093.rar"),
    (5, "10x424-El fotomissatge del pallasso [DADCAEE6].rar"),
    (6, "Animelliure_TotsRucs_El_Detectiu_Conan_525_720pCat_Jap+Subs_ForçatsB8C16FEA.rar"),
    (7, "DC-928 (El vermell de l'amor) [70BF1E30].part1.rar"),
    (8, "DC-928 (El vermell de l'amor) [70BF1E30].part2.rar"),
    (9, "DC-1004 (Nus) [5A052F3A].rar"),
]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the user's real settings, session or database."""
    monkeypatch.setenv("TGDL_DATA_DIR", str(tmp_path_factory.mktemp("tgdl-data")))
    for var in ("TGDL_FAKE_TELEGRAM", "TGDL_API_ID", "TGDL_API_HASH", "TGDL_DOWNLOAD_DIR", "TGDL_API_KEY",
                "TGDL_PASSWORD", "TGDL_AUTH", "TGDL_STORAGE_SENTINEL", "TGDL_TEMP_DIR"):
        monkeypatch.delenv(var, raising=False)


def wait_for(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> Any:
    """Poll ``predicate`` until it returns something truthy (tests with background threads)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout} s waiting for {predicate}")


def conan_gateway(**scenario: Any) -> FakeGateway:
    data = {"me": {"id": 777, "name": "Prova", "premium": True}, "code": "12345", **scenario}
    gw = FakeGateway(data)
    from tgdl.telegram.gateway import Dialog

    gw.add_dialog(Dialog(CONAN_CHAT, "El Detectiu Conan - Còpia de seguretat completa (1996-2024) en català",
                         "channel"),
                  [FakeGateway.file(mid, name, size=500_000_000) for mid, name in CONAN_FILES])
    gw.add_dialog(Dialog(-1002222222222, "Hawai 5.0 (castellano)", "channel"), [])
    return gw


class Harness:
    """A running :class:`TelegramService` on a fake gateway, with the events it published."""

    def __init__(self, tmp_path: Path, gateway: FakeGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.settings = settings
        self.events: list[tuple[str, Any]] = []
        self.db = Database(tmp_path / "tgdl.db")
        self.service = TelegramService(lambda: self.settings, self.db, self.publish, data_dir=tmp_path,
                                       gateway_factory=lambda _s, _p: gateway)

    def publish(self, name: str, payload: Any) -> None:
        self.events.append((name, payload))

    def states(self) -> list[str]:
        return [p["state"] for n, p in self.events if n == "telegram"]

    def wait_state(self, state: str, timeout: float = 5.0) -> None:
        wait_for(lambda: self.service.state == state, timeout)

    def login(self) -> None:
        self.wait_state("logged_out")
        self.service.send_code("+34600000000")
        self.service.submit_code("12345")
        self.wait_state("ready")

    def on_loop(self, fn: Callable[[], Any]) -> Any:
        async def run() -> Any:
            return fn()
        return self.service.call(run)


@pytest.fixture
def configured() -> Settings:
    return Settings(telegram_api_id=12345, telegram_api_hash=API_HASH)


@pytest.fixture
def harness(tmp_path: Path, configured: Settings) -> Iterator[Harness]:
    h = Harness(tmp_path, conan_gateway(), configured)
    h.service.start()
    yield h
    h.service.stop()
    h.db.close()


SKYHOOK_FIXTURE = Path(__file__).parent / "fixtures" / "skyhook-72454.json"


def skyhook_transport(calls: list[str] | None = None, *, fail: bool = False) -> Any:
    """httpx transport answering like SkyHook for Detective Conan (72454), from a trimmed real copy."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if fail:
            raise httpx.ConnectError("sense xarxa", request=request)
        if request.url.path.endswith("/72454"):
            return httpx.Response(200, content=SKYHOOK_FIXTURE.read_bytes(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)
