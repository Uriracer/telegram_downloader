"""The Telegram session, its login flows and the indexing of sources.

Telethon is asyncio-only and its client belongs to the loop that created it,
so the service owns **one event loop in its own thread**; the web server (and
later the download manager) call into it with :meth:`TelegramService.call`
(``run_coroutine_threadsafe``). Nothing else touches the gateway.

Session states (``status()["state"]``, pushed as ``telegram`` events)::

    unconfigured ─(api_id/api_hash)─▶ connecting ─▶ logged_out ─┬─▶ qr ─────────┬─▶ ready
                                          ▲                     ├─▶ code ──────┤
                                          └──── error ◀─────────┘   password ◀─┘

On start the saved session is reused, so after a reboot (power cut) it goes
straight to ``ready`` and indexes the sources that are due.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..db import Database, IndexedMessage
from .gateway import (FloodWait, Gateway, LoginError, Me, NotAuthorized, NotConfigured, PasswordNeeded,
                      TelegramError)

log = logging.getLogger(__name__)

SESSION_NAME = "telegram"            # -> <data dir>/telegram.session
FAKE_ENV = "TGDL_FAKE_TELEGRAM"
INDEX_BATCH = 200
QR_RENEWALS = 20                     # ~10 minutes of QR codes, then back to logged_out
CONNECT_RETRY = (5, 10, 30, 60, 120, 300)
PERIODIC_SECONDS = 60
ME_REFRESH_SECONDS = 15 * 60

UNCONFIGURED, CONNECTING, LOGGED_OUT, QR, CODE, PASSWORD, READY, ERROR = (
    "unconfigured", "connecting", "logged_out", "qr", "code", "password", "ready", "error")

GatewayFactory = Callable[[Any, Path], Gateway]


def default_gateway_factory(settings: Any, session_path: Path) -> Gateway:
    fake = os.environ.get(FAKE_ENV, "").strip()
    if fake:
        from .fake import FakeGateway

        log.warning("ATENCIÓ: Telegram de prova (%s=%s); no es connecta a Telegram.", FAKE_ENV, fake)
        return FakeGateway.from_file(fake)
    from .telethon_gateway import TelethonGateway

    return TelethonGateway(session_path, int(settings.telegram_api_id), str(settings.telegram_api_hash))


def fake_mode() -> bool:
    return bool(os.environ.get(FAKE_ENV, "").strip())


class TelegramService:
    def __init__(self, settings_getter: Callable[[], Any], db: Database, publish: Callable[[str, Any], None],
                 *, data_dir: Path | str, gateway_factory: Optional[GatewayFactory] = None) -> None:
        self._settings = settings_getter
        self.db = db
        self._publish = publish
        self.session_path = Path(data_dir) / SESSION_NAME
        self._factory = gateway_factory or default_gateway_factory
        self.gateway: Optional[Gateway] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"state": UNCONFIGURED, "me": None, "error": None, "qr_url": None,
                                       "qr_expires": None, "phone": None, "password_hint": None}
        self._qr_task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._indexing: dict[int, dict[str, Any]] = {}
        self._boot_task: Optional[asyncio.Task] = None
        self._last_me_refresh = 0.0
        #: Called (in a worker thread) with the source id after each index run, e.g. to reconcile the library.
        self.after_index: Optional[Callable[[int], None]] = None

    # =================================================================== thread / loop
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.call_soon(self._ready.set)
        self._boot_task = loop.create_task(self._boot())
        self._spawn(self._periodic())
        try:
            loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def stop(self, timeout: float = 10.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._disconnect(), loop).result(timeout)
        except Exception as exc:   # pragma: no cover - best effort
            log.debug("Desconnexió de Telegram: %s", exc)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        self._thread = None

    def call(self, factory: Callable[[], Awaitable[Any]], timeout: float = 60.0) -> Any:
        """Run ``factory()`` on the service loop and wait for its result (from any other thread)."""
        if self._loop is None:
            raise RuntimeError("El servei de Telegram no està en marxa.")

        async def runner() -> Any:
            return await factory()

        future = asyncio.run_coroutine_threadsafe(runner(), self._loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise TelegramError("Telegram no ha respost a temps.") from None

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # =================================================================== state
    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._state)
        settings = self._settings()
        out["configured"] = bool(getattr(settings, "telegram_configured", False)) or fake_mode()
        out["fake"] = fake_mode()
        return out

    def _set(self, **changes: Any) -> None:
        with self._lock:
            if changes.get("state") not in (None, QR):
                changes.setdefault("qr_url", None)
                changes.setdefault("qr_expires", None)
            self._state.update(changes)
        self._publish("telegram", self.status())

    @property
    def state(self) -> str:
        with self._lock:
            return str(self._state["state"])

    def _require_gateway(self) -> Gateway:
        if self.gateway is None:
            raise NotConfigured()
        return self.gateway

    def _require_ready(self) -> Gateway:
        gw = self._require_gateway()
        if self.state != READY:
            raise NotAuthorized()
        return gw

    # =================================================================== connection
    async def _boot(self) -> None:
        settings = self._settings()
        if not (getattr(settings, "telegram_configured", False) or fake_mode()):
            self._set(state=UNCONFIGURED, me=None, error=None)
            return
        try:
            self.gateway = self._factory(settings, self.session_path)
        except Exception as exc:
            log.exception("No s'ha pogut crear el client de Telegram")
            self._set(state=ERROR, error=str(exc))
            return
        attempt = 0
        while True:
            self._set(state=CONNECTING, error=None if attempt == 0 else self._state.get("error"))
            try:
                await self.gateway.connect()
                authorized = await self.gateway.is_authorized()
                break
            except LoginError as exc:      # bad api_id/api_hash: retrying won't help
                self._set(state=ERROR, error=str(exc))
                return
            except Exception as exc:
                delay = CONNECT_RETRY[min(attempt, len(CONNECT_RETRY) - 1)]
                attempt += 1
                log.warning("No es pot connectar a Telegram (%s); es torna a provar d'aquí a %d s.", exc, delay)
                self._set(state=CONNECTING, error=f"No es pot connectar a Telegram: {exc}. "
                                                  f"Es torna a provar d'aquí a {delay} s.")
                await asyncio.sleep(delay)
        if authorized:
            try:
                me = await self.gateway.me()
            except NotAuthorized:
                self._set(state=LOGGED_OUT, me=None, error="La sessió desada ja no és vàlida; torna a iniciar-la.")
                return
            await self._logged_in(me, restored=True)
        else:
            self._set(state=LOGGED_OUT, me=None, error=None)

    async def _disconnect(self) -> None:
        self._cancel_qr()
        if self.gateway is not None:
            try:
                await self.gateway.disconnect()
            except Exception:   # pragma: no cover
                pass

    async def _reboot(self) -> None:
        if self._boot_task is not None and not self._boot_task.done():
            self._boot_task.cancel()
        await self._disconnect()
        self.gateway = None
        self._boot_task = asyncio.ensure_future(self._boot())

    def reconfigure(self) -> None:
        """The api_id/api_hash changed: drop the client and start again with the new ones."""
        self.call(self._reboot)

    async def _logged_in(self, me: Me, *, restored: bool = False) -> None:
        self._last_me_refresh = time.monotonic()
        self._set(state=READY, me=me.to_dict(), error=None, phone=None, password_hint=None)
        log.info("Telegram: %s %s%s (Premium: %s)", "sessió recuperada," if restored else "sessió iniciada com a",
                 me.name, f" @{me.username}" if me.username else "", "sí" if me.premium else "no")
        self._index_due()

    # =================================================================== login
    def _cancel_qr(self) -> None:
        if self._qr_task is not None and not self._qr_task.done():
            self._qr_task.cancel()
        self._qr_task = None

    def _check_can_login(self) -> Gateway:
        gw = self._require_gateway()
        if self.state == READY:
            raise LoginError("Ja hi ha una sessió iniciada.")
        if self.state in (CONNECTING, UNCONFIGURED):
            raise LoginError("Encara no s'ha pogut connectar a Telegram.")
        return gw

    def start_qr(self) -> dict[str, Any]:
        return self.call(self._start_qr)

    async def _start_qr(self) -> dict[str, Any]:
        gw = self._check_can_login()
        self._cancel_qr()
        token = await gw.qr_start()
        self._set(state=QR, qr_url=token.url, qr_expires=token.expires.isoformat(), error=None)
        self._qr_task = self._spawn(self._qr_loop(token.expires))
        return self.status()

    async def _qr_loop(self, expires: datetime) -> None:
        gw = self._require_gateway()
        renewals = 0
        while True:
            remaining = (expires - datetime.now(timezone.utc)).total_seconds()
            try:
                me = await gw.qr_wait(timeout=max(1.0, remaining))
            except asyncio.TimeoutError:
                renewals += 1
                if renewals > QR_RENEWALS:
                    self._set(state=LOGGED_OUT, error="El codi QR ha caducat. Torna-ho a provar.")
                    return
                try:
                    token = await gw.qr_start()
                except TelegramError as exc:
                    self._set(state=LOGGED_OUT, error=str(exc))
                    return
                expires = token.expires
                self._set(state=QR, qr_url=token.url, qr_expires=token.expires.isoformat())
                continue
            except PasswordNeeded as exc:
                self._set(state=PASSWORD, password_hint=exc.hint or None, error=None)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set(state=LOGGED_OUT, error=str(exc))
                return
            await self._logged_in(me)
            return

    def send_code(self, phone: str) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            gw = self._check_can_login()
            self._cancel_qr()
            cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
            await gw.send_code(cleaned)
            self._set(state=CODE, phone=cleaned, error=None)
            return self.status()
        return self.call(run)

    def submit_code(self, code: str) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            gw = self._check_can_login()
            try:
                me = await gw.sign_in_code("".join(c for c in code if c.isdigit()))
            except PasswordNeeded as exc:
                self._set(state=PASSWORD, password_hint=exc.hint or None, error=None)
                return self.status()
            await self._logged_in(me)
            return self.status()
        return self.call(run)

    def submit_password(self, password: str) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            gw = self._check_can_login()
            me = await gw.sign_in_password(password)
            await self._logged_in(me)
            return self.status()
        return self.call(run)

    def cancel_login(self) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            self._cancel_qr()
            if self.state in (QR, CODE, PASSWORD):
                self._set(state=LOGGED_OUT, error=None, phone=None, password_hint=None)
            return self.status()
        return self.call(run)

    def logout(self) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            gw = self._require_gateway()
            self._cancel_qr()
            if self.state == READY:
                await gw.log_out()
                log.info("Telegram: sessió tancada.")
            self._set(state=LOGGED_OUT, me=None, error=None)
            # Telethon disconnects on log_out; reconnect so a new login can start.
            await self._reboot()
            return self.status()
        return self.call(run)

    # =================================================================== chats
    def dialogs(self) -> list[dict[str, Any]]:
        async def run() -> list[dict[str, Any]]:
            gw = self._require_ready()
            return [d.to_dict() for d in await gw.dialogs()]
        return self.call(run, timeout=120)

    # =================================================================== indexing
    def indexing(self, source_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            info = self._indexing.get(source_id)
            return dict(info) if info else None

    def source_payload(self, source_id: int) -> Optional[dict[str, Any]]:
        src = self.db.source(source_id)
        if src is None:
            return None
        data = src.to_dict()
        data["indexing"] = self.indexing(source_id)
        return data

    def _publish_source(self, source_id: int) -> None:
        payload = self.source_payload(source_id)
        if payload is not None:
            self._publish("source", payload)

    def index_source(self, source_id: int, *, full: bool = False) -> bool:
        """Start indexing in the background; False if it is already running."""
        async def run() -> bool:
            self._require_ready()
            with self._lock:
                if source_id in self._indexing:
                    return False
                self._indexing[source_id] = {"full": full, "seen": 0, "new": 0, "started": time.time()}
            self._spawn(self._index(source_id, full))
            return True
        return self.call(run)

    async def _index(self, source_id: int, full: bool) -> None:
        gw = self._require_gateway()
        src = await asyncio.to_thread(self.db.source, source_id)
        if src is None:
            with self._lock:
                self._indexing.pop(source_id, None)
            return
        self._publish_source(source_id)
        min_id = 0 if full else src.last_msg_id
        batch: list[IndexedMessage] = []
        present: set[int] = set()
        max_id = src.last_msg_id
        seen = new = 0

        async def flush() -> None:
            nonlocal new
            if batch:
                new += await asyncio.to_thread(self.db.upsert_messages, source_id, list(batch))
                batch.clear()
            with self._lock:
                if source_id in self._indexing:
                    self._indexing[source_id].update(seen=seen, new=new)
            self._publish_source(source_id)

        error: Optional[str] = None
        try:
            async for m in gw.iter_files(src.chat_id, min_id):
                seen += 1
                present.add(m.msg_id)
                max_id = max(max_id, m.msg_id)
                batch.append(m)
                if len(batch) >= INDEX_BATCH:
                    await flush()
            await flush()
            if full:
                gone = await asyncio.to_thread(self.db.mark_deleted, source_id, present)
                if gone:
                    log.info("Índex «%s»: %d missatges ja no hi són", src.title, gone)
        except asyncio.CancelledError:
            raise
        except FloodWait as exc:
            error = str(exc)
        except TelegramError as exc:
            error = str(exc)
        except Exception as exc:   # never kill the loop
            log.exception("Error indexant «%s»", src.title)
            error = f"Error inesperat: {exc}"
        finally:
            try:
                await flush()
            except Exception:   # pragma: no cover
                log.exception("No s'han pogut desar els missatges indexats")
            await asyncio.to_thread(self.db.set_index_result, source_id,
                                    last_msg_id=None if error and not seen else max_id, error=error)
            with self._lock:
                self._indexing.pop(source_id, None)
            self._publish_source(source_id)
        if error:
            log.warning("Índex «%s»: %s", src.title, error)
        else:
            log.info("Índex «%s»: %d fitxers revisats, %d de nous", src.title, seen, new)
        hook = self.after_index
        if hook is not None:
            try:
                await asyncio.to_thread(hook, source_id)
            except Exception:
                log.exception("Error després d'indexar «%s»", src.title)

    def _index_due(self) -> None:
        """Start indexing every auto-indexed source whose last index is older than the interval."""
        minutes = int(getattr(self._settings(), "index_interval_minutes", 0) or 0)
        if self.state != READY:
            return
        now = datetime.now(timezone.utc)
        for src in self.db.sources():
            if not src.options.auto_index:
                continue
            if src.indexed_at and minutes <= 0:
                continue
            if src.indexed_at:
                try:
                    age = (now - datetime.fromisoformat(src.indexed_at)).total_seconds()
                except ValueError:
                    age = float("inf")
                if age < minutes * 60:
                    continue
            with self._lock:
                if src.id in self._indexing:
                    continue
                self._indexing[src.id] = {"full": False, "seen": 0, "new": 0, "started": time.time()}
            self._spawn(self._index(src.id, False))

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(PERIODIC_SECONDS)
            try:
                if self.state != READY:
                    continue
                self._index_due()
                if time.monotonic() - self._last_me_refresh >= ME_REFRESH_SECONDS:
                    await self._refresh_me()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error a la tasca periòdica de Telegram")

    async def _refresh_me(self) -> None:
        gw = self._require_ready()
        self._last_me_refresh = time.monotonic()
        try:
            me = await gw.me()
        except NotAuthorized:
            log.warning("Telegram: la sessió s'ha tancat des d'un altre dispositiu.")
            self._set(state=LOGGED_OUT, me=None, error="La sessió s'ha tancat des d'un altre dispositiu.")
            return
        old = (self.status().get("me") or {}).get("premium")
        if old is not None and bool(old) != me.premium:
            log.warning("Telegram: el compte %s Premium.", "ara és" if me.premium else "ja no és")
        self._set(me=me.to_dict())
