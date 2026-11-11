"""What the app needs from Telegram, independent of the MTProto library.

:class:`Gateway` is implemented by :class:`~tgdl.telegram.telethon_gateway.TelethonGateway`
(the real thing) and :class:`~tgdl.telegram.fake.FakeGateway` (tests, the E2E
test and UI development without an account). Every method is a coroutine and
runs on the service's event loop (see :mod:`tgdl.telegram.service`).

Errors are translated into the exceptions below, with Catalan messages, so
the rest of the app never imports Telethon.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Optional, Protocol

from ..db import IndexedMessage


# --------------------------------------------------------------------------- data

@dataclass
class Me:
    id: int
    name: str
    username: str = ""
    phone: str = ""
    premium: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Dialog:
    """A chat, group or channel, with its **full** name (``tdl chat ls`` truncated them)."""
    id: int                 # "marked" id: channels -100…, groups -…, users positive
    title: str
    kind: str               # channel | group | user | bot | self
    username: str = ""
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QrToken:
    url: str                # tg://login?token=…  (what the QR code encodes)
    expires: datetime


# --------------------------------------------------------------------------- errors

class TelegramError(Exception):
    """Base class; ``str(exc)`` is a Catalan message for the user."""


class NotConfigured(TelegramError):
    def __init__(self) -> None:
        super().__init__("Falten l'api_id i l'api_hash de Telegram (Configuració).")


class NotAuthorized(TelegramError):
    def __init__(self) -> None:
        super().__init__("No hi ha cap sessió de Telegram iniciada.")


class PasswordNeeded(TelegramError):
    def __init__(self, hint: str = "") -> None:
        self.hint = hint
        super().__init__("Aquest compte té verificació en dos passos: cal la contrasenya.")


class LoginError(TelegramError):
    """Wrong phone, code or password, expired code, banned number…"""


class QrExpired(TelegramError):
    def __init__(self) -> None:
        super().__init__("El codi QR ha caducat.")


class FloodWait(TelegramError):
    def __init__(self, seconds: int) -> None:
        self.seconds = int(seconds)
        super().__init__(f"Telegram demana esperar {self.seconds} s abans de tornar-ho a provar.")


class ChatUnavailable(TelegramError):
    """The chat does not exist any more, or the account has no access to it."""


class FileGone(TelegramError):
    """The message was deleted, or it no longer has a file."""

    def __init__(self, message: str = "El missatge ja no existeix o ja no té cap fitxer.") -> None:
        super().__init__(message)


@dataclass
class RemoteFile:
    """A file that can be read in blocks; ``handle`` is gateway-specific (Telethon: the message media)."""
    chat_id: int
    msg_id: int
    size: int
    handle: Any = None


# --------------------------------------------------------------------------- protocol

class Gateway(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_authorized(self) -> bool: ...
    async def me(self) -> Me: ...

    # login
    async def qr_start(self) -> QrToken: ...
    async def qr_wait(self, timeout: float) -> Me:
        """Me once the QR is scanned; ``TimeoutError``, :class:`PasswordNeeded` or :class:`QrExpired`."""
    async def send_code(self, phone: str) -> None: ...
    async def sign_in_code(self, code: str) -> Me: ...
    async def sign_in_password(self, password: str) -> Me: ...
    async def log_out(self) -> None: ...

    # chats and files
    async def dialogs(self) -> list[Dialog]: ...
    def iter_files(self, chat_id: int, min_id: int = 0) -> AsyncIterator[IndexedMessage]:
        """Messages with a file (document, video, audio, photo), oldest first, ``msg_id > min_id``."""

    # downloads
    async def remote_file(self, chat_id: int, msg_id: int) -> RemoteFile:
        """The file of a message (fresh file reference); :class:`FileGone` if it is not there any more."""
    async def read_block(self, remote: RemoteFile, offset: int, limit: int) -> bytes:
        """``limit`` bytes from ``offset`` (fewer at the end of the file).

        ``offset`` is a multiple of ``limit`` and ``limit`` divides 1 MiB (MTProto's rules for upload.getFile).
        """


def message_kind(mime: str, is_photo: bool = False) -> str:
    from ..db import KIND_AUDIO, KIND_DOCUMENT, KIND_PHOTO, KIND_VIDEO

    if is_photo or mime.startswith("image/"):
        return KIND_PHOTO
    if mime.startswith("video/"):
        return KIND_VIDEO
    if mime.startswith("audio/"):
        return KIND_AUDIO
    return KIND_DOCUMENT


def strip_marked_id(chat_id: int) -> Optional[int]:
    """-1001234567890 -> 1234567890 (channel id as shown in t.me/c/… links)."""
    text = str(chat_id)
    return int(text[4:]) if text.startswith("-100") and len(text) > 4 else None
