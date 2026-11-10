"""The Telethon gateway without network: error translation and construction."""
from __future__ import annotations

from pathlib import Path

from telethon import errors

from conftest import API_HASH
from tgdl.telegram.gateway import ChatUnavailable, FloodWait, LoginError, NotAuthorized, TelegramError
from tgdl.telegram.telethon_gateway import TelethonGateway, _me


def test_flood_wait_keeps_the_seconds() -> None:
    exc = TelethonGateway._translate(errors.FloodWaitError(request=None, capture=37))
    assert isinstance(exc, FloodWait) and exc.seconds == 37 and "37 s" in str(exc)


def test_login_errors_are_catalan() -> None:
    cases = {
        errors.PhoneCodeInvalidError(request=None): "El codi no és correcte.",
        errors.PhoneCodeExpiredError(request=None): "caducat",
        errors.PasswordHashInvalidError(request=None): "contrasenya",
        errors.ApiIdInvalidError(request=None): "api_id",
        errors.PhoneNumberInvalidError(request=None): "+34",
    }
    for raw, text in cases.items():
        exc = TelethonGateway._translate(raw)
        assert isinstance(exc, LoginError) and text in str(exc), raw


def test_other_errors() -> None:
    assert isinstance(TelethonGateway._translate(errors.AuthKeyUnregisteredError(request=None)), NotAuthorized)
    assert isinstance(TelethonGateway._translate(errors.ChannelPrivateError(request=None)), ChatUnavailable)
    generic = TelethonGateway._translate(errors.RPCError(request=None, message="WEIRD_ERROR", code=400))
    assert isinstance(generic, TelegramError) and "WEIRD_ERROR" in str(generic)
    plain = ValueError("x")
    assert TelethonGateway._translate(plain) is plain


def test_construct_and_me(tmp_path: Path) -> None:
    gw = TelethonGateway(tmp_path / "telegram", 12345, API_HASH)
    assert gw.client.api_id == 12345
    me = _me(type("U", (), {"id": 5, "first_name": "Oriol", "last_name": None, "username": "u",
                            "phone": "34", "premium": True})())
    assert me.name == "Oriol" and me.premium
