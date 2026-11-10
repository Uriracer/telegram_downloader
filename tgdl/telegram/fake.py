"""An in-memory Telegram for tests, the E2E test and UI development.

Enable it with ``TGDL_FAKE_TELEGRAM=<scenario.json>`` (never in production);
the scenario describes the account and its chats::

    {
      "me": {"id": 1, "name": "Prova", "premium": true},
      "authorized": false,          // start logged out
      "code": "12345",              // phone login code
      "password": "secret",         // 2FA password ("" = no 2FA)
      "qr_auto_approve": 3,         // seconds until the QR counts as scanned (null = never)
      "dialogs": [
        {"id": -1001234, "title": "El Detectiu Conan (còpia)", "kind": "channel",
         "files": [{"msg_id": 10, "file_name": "DC-1004 (Nus) [5A052F3A].rar", "size": 524288000}]}
      ]
    }
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..db import IndexedMessage
from .gateway import (ChatUnavailable, Dialog, LoginError, Me, NotAuthorized, PasswordNeeded, QrExpired,
                      QrToken, message_kind)

QR_LIFETIME = 30.0


class FakeGateway:
    def __init__(self, scenario: Optional[dict[str, Any]] = None) -> None:
        s = scenario or {}
        me = s.get("me") or {}
        self._me = Me(id=int(me.get("id", 1)), name=str(me.get("name", "Usuari de prova")),
                      username=str(me.get("username", "")), phone=str(me.get("phone", "")),
                      premium=bool(me.get("premium", False)))
        self.authorized = bool(s.get("authorized", False))
        self.code = str(s.get("code", "12345"))
        self.password = str(s.get("password", ""))
        self.qr_auto_approve: Optional[float] = s.get("qr_auto_approve")
        self.dialog_list: list[Dialog] = []
        self.files: dict[int, list[IndexedMessage]] = {}
        for d in s.get("dialogs", []):
            self.add_dialog(Dialog(id=int(d["id"]), title=str(d["title"]), kind=str(d.get("kind", "channel")),
                                   username=str(d.get("username", "")), archived=bool(d.get("archived", False))),
                            [self.file(**f) for f in d.get("files", [])])
        self.connected = False
        self.calls: list[str] = []
        self._qr_scanned: Optional[asyncio.Event] = None   # created on the loop, see _event()
        self._awaiting_password = False
        self._phone: Optional[str] = None
        #: Raise this from the next iter_files call (tests of index errors).
        self.fail_next_index: Optional[Exception] = None

    @classmethod
    def from_file(cls, path: Path | str) -> "FakeGateway":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def file(msg_id: int, file_name: str, size: int = 1024, text: str = "", date: Optional[str] = None,
             mime: str = "", grouped_id: Optional[int] = None, **_: Any) -> IndexedMessage:
        if not mime:
            lower = file_name.lower()
            mime = ("image/jpeg" if lower.endswith((".jpg", ".jpeg", ".png")) else
                    "video/x-matroska" if lower.endswith(".mkv") else
                    "video/mp4" if lower.endswith(".mp4") else
                    "application/vnd.rar" if ".rar" in lower else "application/octet-stream")
        return IndexedMessage(msg_id=msg_id, date=date or "2024-01-01T00:00:00+00:00", text=text,
                              file_name=file_name, size=size, mime=mime, kind=message_kind(mime),
                              document_id=10_000 + msg_id, dc_id=4, grouped_id=grouped_id)

    def add_dialog(self, dialog: Dialog, files: Optional[list[IndexedMessage]] = None) -> None:
        self.dialog_list.append(dialog)
        self.files[dialog.id] = list(files or [])

    def _event(self) -> asyncio.Event:
        if self._qr_scanned is None:
            self._qr_scanned = asyncio.Event()
        return self._qr_scanned

    def scan_qr(self) -> None:
        """Simulate the user scanning the QR code (call on the gateway's loop)."""
        self._event().set()

    # ------------------------------------------------------------ connection
    async def connect(self) -> None:
        self.calls.append("connect")
        self.connected = True

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    async def is_authorized(self) -> bool:
        return self.authorized

    async def me(self) -> Me:
        if not self.authorized:
            raise NotAuthorized()
        return self._me

    def set_premium(self, premium: bool) -> None:
        self._me.premium = premium

    # ------------------------------------------------------------ login
    async def qr_start(self) -> QrToken:
        self.calls.append("qr_start")
        self._event().clear()
        return QrToken(url=f"tg://login?token={secrets.token_urlsafe(16)}",
                       expires=datetime.now(timezone.utc) + timedelta(seconds=QR_LIFETIME))

    async def qr_wait(self, timeout: float) -> Me:
        event = self._event()
        wait = timeout if self.qr_auto_approve is None else min(timeout, float(self.qr_auto_approve))
        try:
            await asyncio.wait_for(event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            if self.qr_auto_approve is None or wait < float(self.qr_auto_approve):
                raise
        if self.password:
            self._awaiting_password = True
            raise PasswordNeeded("pista de prova")
        self.authorized = True
        return self._me

    async def send_code(self, phone: str) -> None:
        self.calls.append(f"send_code {phone}")
        if not phone.startswith("+"):
            raise LoginError("El número de telèfon no és vàlid. Escriu-lo amb el prefix: +34…")
        self._phone = phone

    async def sign_in_code(self, code: str) -> Me:
        if not self._phone:
            raise LoginError("Primer cal demanar el codi.")
        if code != self.code:
            raise LoginError("El codi no és correcte.")
        if self.password:
            self._awaiting_password = True
            raise PasswordNeeded("pista de prova")
        self.authorized = True
        return self._me

    async def sign_in_password(self, password: str) -> Me:
        if not self._awaiting_password:
            raise QrExpired()
        if password != self.password:
            raise LoginError("La contrasenya de verificació en dos passos no és correcta.")
        self._awaiting_password = False
        self.authorized = True
        return self._me

    async def log_out(self) -> None:
        self.calls.append("log_out")
        self.authorized = False

    # ------------------------------------------------------------ chats and files
    async def dialogs(self) -> list[Dialog]:
        if not self.authorized:
            raise NotAuthorized()
        return list(self.dialog_list)

    async def iter_files(self, chat_id: int, min_id: int = 0) -> AsyncIterator[IndexedMessage]:
        if not self.authorized:
            raise NotAuthorized()
        if chat_id not in self.files:
            raise ChatUnavailable("Aquest xat no és a la llista de xats del compte.")
        if self.fail_next_index is not None:
            exc, self.fail_next_index = self.fail_next_index, None
            raise exc
        for m in sorted(self.files[chat_id], key=lambda x: x.msg_id):
            if m.msg_id > min_id:
                await asyncio.sleep(0)
                yield m
