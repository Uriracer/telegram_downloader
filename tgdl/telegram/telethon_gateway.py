"""The real :class:`~tgdl.telegram.gateway.Gateway`, on Telethon (MTProto, user account).

Telethon needs an ``api_id``/``api_hash`` pair from https://my.telegram.org
(``tdl`` doesn't ask for one because it embeds the official desktop app's).
The session (authorisation key) is a SQLite file next to the settings; whoever
has it has the account, so it is created with mode 0600.

Must be constructed and used on one event loop (the service's).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

from .. import __version__
from ..db import IndexedMessage
from .gateway import (ChatUnavailable, Dialog, FloodWait, LoginError, Me, NotAuthorized, PasswordNeeded,
                      QrExpired, QrToken, TelegramError, message_kind)

log = logging.getLogger(__name__)

DEVICE_MODEL = "Telegram Downloader"


def _me(user: object) -> Me:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    return Me(id=int(getattr(user, "id", 0)), name=f"{first} {last}".strip(),
              username=getattr(user, "username", "") or "", phone=getattr(user, "phone", "") or "",
              premium=bool(getattr(user, "premium", False)))


class TelethonGateway:
    def __init__(self, session_path: Path | str, api_id: int, api_hash: str) -> None:
        from telethon import TelegramClient

        self.session_path = Path(session_path)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.session_path), api_id, api_hash,
            device_model=DEVICE_MODEL, system_version="Linux", app_version=__version__,
            lang_code="ca", system_lang_code="ca",
            connection_retries=10, retry_delay=5, auto_reconnect=True,
            # Short waits are slept by Telethon itself; longer ones reach us as FloodWait.
            flood_sleep_threshold=120,
            request_retries=5,
        )
        self._qr = None
        self._phone: Optional[str] = None

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _translate(exc: Exception) -> Exception:
        from telethon import errors

        if isinstance(exc, errors.FloodWaitError):
            return FloodWait(exc.seconds)
        if isinstance(exc, errors.ApiIdInvalidError):
            return LoginError("L'api_id o l'api_hash no són vàlids. Revisa'ls a https://my.telegram.org.")
        if isinstance(exc, errors.PhoneNumberInvalidError):
            return LoginError("El número de telèfon no és vàlid. Escriu-lo amb el prefix: +34…")
        if isinstance(exc, errors.PhoneNumberBannedError):
            return LoginError("Telegram ha bloquejat aquest número.")
        if isinstance(exc, errors.PhoneCodeInvalidError):
            return LoginError("El codi no és correcte.")
        if isinstance(exc, errors.PhoneCodeExpiredError):
            return LoginError("El codi ha caducat. Demana'n un de nou.")
        if isinstance(exc, errors.PasswordHashInvalidError):
            return LoginError("La contrasenya de verificació en dos passos no és correcta.")
        if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.AuthKeyDuplicatedError,
                            errors.UserDeactivatedBanError)):
            return NotAuthorized()
        if isinstance(exc, (errors.ChannelPrivateError, errors.ChatIdInvalidError, errors.PeerIdInvalidError)):
            return ChatUnavailable("Aquest xat no existeix o el compte ja no hi té accés.")
        if isinstance(exc, errors.RPCError):
            return TelegramError(f"Telegram ha respost {exc.__class__.__name__}: {exc}")
        return exc

    async def _password_needed(self) -> PasswordNeeded:
        try:
            from telethon import functions

            info = await self.client(functions.account.GetPasswordRequest())
            return PasswordNeeded(getattr(info, "hint", "") or "")
        except Exception:
            return PasswordNeeded()

    def _protect_session(self) -> None:
        path = self.session_path.with_suffix(".session") if self.session_path.suffix != ".session" \
            else self.session_path
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ connection
    async def connect(self) -> None:
        try:
            await self.client.connect()
        except Exception as exc:
            raise self._translate(exc) from exc
        self._protect_session()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def is_authorized(self) -> bool:
        try:
            return bool(await self.client.is_user_authorized())
        except Exception as exc:
            raise self._translate(exc) from exc

    async def me(self) -> Me:
        try:
            user = await self.client.get_me()
        except Exception as exc:
            raise self._translate(exc) from exc
        if user is None:
            raise NotAuthorized()
        return _me(user)

    # ------------------------------------------------------------ login
    async def qr_start(self) -> QrToken:
        try:
            if self._qr is None:
                self._qr = await self.client.qr_login()
            else:
                await self._qr.recreate()
        except Exception as exc:
            raise self._translate(exc) from exc
        return QrToken(url=self._qr.url, expires=self._qr.expires)

    async def qr_wait(self, timeout: float) -> Me:
        from telethon import errors

        if self._qr is None:
            raise QrExpired()
        try:
            user = await self._qr.wait(timeout=timeout)
        except asyncio.TimeoutError:
            raise
        except errors.SessionPasswordNeededError:
            self._qr = None
            raise await self._password_needed() from None
        except Exception as exc:
            raise self._translate(exc) from exc
        self._qr = None
        self._protect_session()
        return _me(user)

    async def send_code(self, phone: str) -> None:
        try:
            await self.client.send_code_request(phone)
        except Exception as exc:
            raise self._translate(exc) from exc
        self._phone = phone

    async def sign_in_code(self, code: str) -> Me:
        from telethon import errors

        if not self._phone:
            raise LoginError("Primer cal demanar el codi.")
        try:
            user = await self.client.sign_in(phone=self._phone, code=code)
        except errors.SessionPasswordNeededError:
            raise await self._password_needed() from None
        except Exception as exc:
            raise self._translate(exc) from exc
        self._protect_session()
        return _me(user)

    async def sign_in_password(self, password: str) -> Me:
        try:
            user = await self.client.sign_in(password=password)
        except Exception as exc:
            raise self._translate(exc) from exc
        self._protect_session()
        return _me(user)

    async def log_out(self) -> None:
        try:
            await self.client.log_out()
        except Exception as exc:
            raise self._translate(exc) from exc

    # ------------------------------------------------------------ chats and files
    async def dialogs(self) -> list[Dialog]:
        from telethon.tl.types import Channel, Chat, User

        out: list[Dialog] = []
        try:
            async for d in self.client.iter_dialogs():
                entity = d.entity
                if isinstance(entity, User):
                    kind = "self" if entity.is_self else ("bot" if entity.bot else "user")
                    title = "Missatges desats" if entity.is_self else (d.name or "")
                elif isinstance(entity, Channel):
                    kind = "group" if entity.megagroup else "channel"
                    title = entity.title or d.name or ""
                elif isinstance(entity, Chat):
                    kind, title = "group", entity.title or d.name or ""
                else:
                    continue
                out.append(Dialog(id=int(d.id), title=title, kind=kind,
                                  username=getattr(entity, "username", "") or "",
                                  archived=getattr(d, "folder_id", None) == 1))
        except Exception as exc:
            raise self._translate(exc) from exc
        return out

    async def _entity(self, chat_id: int):
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            # Not in the session's entity cache yet (fresh session): listing the dialogs fills it.
            await self.client.get_dialogs()
            try:
                return await self.client.get_input_entity(chat_id)
            except ValueError:
                raise ChatUnavailable("Aquest xat no és a la llista de xats del compte.") from None

    async def iter_files(self, chat_id: int, min_id: int = 0) -> AsyncIterator[IndexedMessage]:
        from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

        try:
            entity = await self._entity(chat_id)
            async for msg in self.client.iter_messages(entity, reverse=True, min_id=min_id):
                if not isinstance(msg.media, (MessageMediaDocument, MessageMediaPhoto)) or msg.sticker:
                    continue
                f = msg.file
                if f is None:
                    continue
                is_photo = isinstance(msg.media, MessageMediaPhoto)
                mime = f.mime_type or ("image/jpeg" if is_photo else "")
                media = msg.photo if is_photo else msg.document
                yield IndexedMessage(
                    msg_id=msg.id, date=msg.date.isoformat() if msg.date else None, text=msg.message or "",
                    file_name=f.name or f"{msg.id}{f.ext or ''}", size=int(f.size or 0), mime=mime,
                    kind=message_kind(mime, is_photo), document_id=getattr(media, "id", None),
                    dc_id=getattr(media, "dc_id", None), grouped_id=msg.grouped_id)
        except TelegramError:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc
