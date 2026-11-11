"""HTTP server: REST API under ``/api``, SSE at ``/api/events`` and the web UI.

Blocking work (SQLite, waiting on the Telegram loop) runs in the threadpool
(``run_in_threadpool``) so the event loop stays responsive.

Downloads (``/api/downloads``, ``/api/sources/{id}/queue``) are run by
:class:`~tgdl.downloads.DownloadManager` on the Telegram service's loop.

Telegram errors become HTTP errors with a Catalan ``detail``:
``LoginError`` 400, ``NotConfigured``/``NotAuthorized`` 409, ``ChatUnavailable``
404, ``FloodWait`` 429 (+ ``Retry-After``), other ``TelegramError`` 502.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__ as APP_VERSION
from . import files, review, storage
from .auth import COOKIE_NAME, SESSION_SECONDS, AuthManager, password_problem
from .config import Settings, validation_errors
from .db import DOWNLOADED, ERROR, IN_LIBRARY, NEW, QUEUED, STATES, Database, SourceOptions
from .downloads import DownloadManager
from .placement import Placer
from .events import CLOSE, EventBroadcaster, sse
from .guard import LocalGuardMiddleware
from .naming import parse as parse_name
from .runtime import LOCKABLE_FIELDS, RuntimeConfig
from .telegram.gateway import (ChatUnavailable, FloodWait, LoginError, NotAuthorized, NotConfigured,
                               TelegramError)
from .telegram.service import TelegramService
from .tvdb import Show, TvdbClient, TvdbError

log = logging.getLogger(__name__)

APP_ID = "telegram-downloader"
KEEPALIVE_SECONDS = 15.0


# --------------------------------------------------------------------------- settings store

class SettingsStore:
    """Thread-safe holder of the current :class:`Settings`, with environment overrides."""

    def __init__(self, loader: Optional[Callable[[], Settings]] = None,
                 saver: Optional[Callable[[Settings], None]] = None,
                 overrides: Optional[dict[str, Any]] = None) -> None:
        from . import config

        self._load = loader or config.load_settings
        self._saver = saver or config.save_settings
        self._overrides = dict(env_overrides() if overrides is None else overrides)
        self._lock = threading.Lock()
        self._stored = self._load()
        self._settings = self._overlay(self._stored)

    def _overlay(self, settings: Settings) -> Settings:
        if not self._overrides:
            return settings
        return settings.updated(self._overrides)

    @property
    def locked(self) -> list[str]:
        return sorted(self._overrides)

    def get(self) -> Settings:
        with self._lock:
            return self._settings

    def set(self, settings: Settings) -> Settings:
        with self._lock:
            stored = settings
            if self._overrides:   # keep the user's own values on disk for forced fields
                stored = dataclasses.replace(settings, **{k: getattr(self._stored, k) for k in self._overrides
                                                          if hasattr(self._stored, k)})
            self._saver(stored)
            self._stored = stored
            self._settings = self._overlay(stored)
            return self._settings


def env_overrides() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, var in LOCKABLE_FIELDS.items():
        value = (os.environ.get(var) or "").strip()
        if value:
            out[name] = value
    return out


PRIVATE_SETTINGS = frozenset({"password_hash", "telegram_api_hash"})


def public_settings(settings: Settings, locked: list[str]) -> dict[str, Any]:
    data = {k: v for k, v in dataclasses.asdict(settings).items() if k not in PRIVATE_SETTINGS}
    data["telegram_api_hash_set"] = bool(settings.telegram_api_hash)
    data["effective_temp_dir"] = settings.effective_temp_dir
    data["locked"] = list(locked)
    return data


# --------------------------------------------------------------------------- bodies

class PasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current: str = Field(default="", max_length=1024)
    new: str = Field(min_length=1, max_length=1024)


class PhoneRequest(BaseModel):
    phone: str = Field(min_length=5, max_length=32)


class CodeRequest(BaseModel):
    code: str = Field(min_length=3, max_length=16)


class TwoFactorRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SourceRequest(BaseModel):
    chat_id: int
    options: dict[str, Any] = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    options: dict[str, Any]


class IndexRequest(BaseModel):
    full: bool = False


class QueueRequest(BaseModel):
    #: Files to queue; empty with ``missing`` = what is missing from the library (see review.wanted).
    msg_ids: list[int] = Field(default_factory=list, max_length=100_000)
    missing: bool = False


class PlaceRequest(BaseModel):
    #: At most this many episodes (a first try with a few); None = every one that is ready.
    limit: Optional[int] = Field(default=None, ge=1, le=100_000)


class UnqueueRequest(BaseModel):
    msg_ids: list[int] = Field(min_length=1, max_length=100_000)


class ImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    dry_run: bool = False


def resolve_web_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


_MISSING_UI_HTML = """<!doctype html><html lang="ca"><meta charset="utf-8">
<title>Telegram Downloader</title><body style="font-family:sans-serif;padding:2rem">
<h1>Telegram Downloader</h1><p>No s'ha trobat la interfície (carpeta <code>web/</code>).</p></body></html>"""


# --------------------------------------------------------------------------- app

def create_app(settings_store: Optional[SettingsStore] = None, *, db: Database, telegram: TelegramService,
               events: EventBroadcaster, runtime: Optional[RuntimeConfig] = None,
               auth: Optional[AuthManager] = None, web_dir: Optional[Path] = None,
               data_dir: Optional[Path] = None, manage_telegram: bool = False,
               tvdb: Optional[TvdbClient] = None, downloads: Optional[DownloadManager] = None) -> FastAPI:
    """Build the app. With ``manage_telegram`` the lifespan starts/stops the Telegram service and the downloads."""
    rt = runtime or RuntimeConfig()
    store = settings_store or SettingsStore()
    if auth is None:
        auth = AuthManager(data_dir, required=rt.auth_required, settings_get=store.get, settings_set=store.set,
                           password_env=rt.password_env)
    authm: AuthManager = auth
    tg = telegram
    tvdb_client = tvdb or TvdbClient(db)
    dl = downloads or DownloadManager(store.get, db, tg, events.publish)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_telegram:
            await run_in_threadpool(tg.start)
            dl.start()
        try:
            yield
        finally:
            events.close()
            if manage_telegram:
                await run_in_threadpool(dl.stop)
                await run_in_threadpool(tg.stop)

    app = FastAPI(title="Telegram Downloader", version=APP_VERSION, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(LocalGuardMiddleware, allowed_hosts=rt.allowed_hosts, runtime=rt, auth=authm)
    app.state.settings_store = store
    app.state.db = db
    app.state.telegram = tg
    app.state.events = events
    app.state.auth = authm
    app.state.runtime = rt
    app.state.tvdb = tvdb_client
    app.state.downloads = dl

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Any, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        field = ".".join(str(p) for p in errors[0].get("loc", [])[1:]) if errors else ""
        message = f"El camp «{field}» no és vàlid." if field else "La petició no és vàlida."
        return JSONResponse({"detail": message}, status_code=422)

    @app.exception_handler(TelegramError)
    async def telegram_error(_request: Any, exc: TelegramError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(exc, LoginError):
            status = 400
        elif isinstance(exc, (NotConfigured, NotAuthorized)):
            status = 409
        elif isinstance(exc, ChatUnavailable):
            status = 404
        elif isinstance(exc, FloodWait):
            status, headers = 429, {"Retry-After": str(exc.seconds)}
        else:
            status = 502
        return JSONResponse({"detail": str(exc)}, status_code=status, headers=headers)

    async def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(fn, *args, **kwargs)

    # ----------------------------------------------------------------- health
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        base = {"ok": True, "version": APP_VERSION, "app": APP_ID}
        if rt.server and not request.state.authenticated:
            return base
        from .runtime import find_7zz

        seven, seven_error = await call(find_7zz)
        return {**base, "mode": rt.mode, "telegram": tg.state,
                "storage": (await call(storage.status, store.get())).to_dict(),
                "seven_zip": seven, "seven_zip_error": seven_error, "db_version": db.schema_version}

    # ------------------------------------------------------------------- auth
    def auth_status(request: Request) -> dict[str, Any]:
        return {"mode": rt.mode, "auth_required": authm.required, "configured": authm.configured,
                "authenticated": bool(request.state.authenticated), "password_from_env": authm.password_from_env}

    def set_session_cookie(request: Request, response: Response) -> None:
        response.set_cookie(COOKIE_NAME, authm.issue_session(), max_age=SESSION_SECONDS, path="/",
                            httponly=True, samesite="strict", secure=bool(request.state.https))

    def throttled(request: Request) -> None:
        wait = authm.throttle.retry_after(request.state.client_address)
        if wait:
            raise HTTPException(status_code=429, detail="Massa intents. Espera un minut i torna-ho a provar.",
                                headers={"Retry-After": str(wait)})

    def require_auth_enabled() -> None:
        if not authm.required:
            raise HTTPException(status_code=400, detail="L'inici de sessió no està activat.")

    @app.get("/api/auth/status")
    async def get_auth_status(request: Request) -> dict[str, Any]:
        return auth_status(request)

    @app.post("/api/auth/setup")
    async def auth_setup(body: PasswordRequest, request: Request, response: Response) -> dict[str, Any]:
        require_auth_enabled()
        if authm.configured:
            raise HTTPException(status_code=409, detail="La contrasenya ja està configurada.")
        problem = password_problem(body.password)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        await call(authm.set_password, body.password)
        log.info("Contrasenya inicial configurada des de %s", request.state.client_address)
        set_session_cookie(request, response)
        request.state.authenticated = True
        return auth_status(request)

    @app.post("/api/auth/login")
    async def auth_login(body: PasswordRequest, request: Request, response: Response) -> dict[str, Any]:
        require_auth_enabled()
        if not authm.configured:
            raise HTTPException(status_code=409, detail="Primer cal crear una contrasenya.")
        throttled(request)
        if not await call(authm.check_password, body.password):
            authm.throttle.failure(request.state.client_address)
            log.warning("Inici de sessió fallit des de %s", request.state.client_address)
            raise HTTPException(status_code=401, detail="Contrasenya incorrecta.")
        authm.throttle.success(request.state.client_address)
        set_session_cookie(request, response)
        request.state.authenticated = True
        return auth_status(request)

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        request.state.authenticated = False
        return auth_status(request)

    @app.put("/api/auth/password")
    async def auth_change_password(body: ChangePasswordRequest, request: Request,
                                   response: Response) -> dict[str, Any]:
        require_auth_enabled()
        if authm.password_from_env:
            raise HTTPException(status_code=400,
                                detail="La contrasenya està definida per la variable d'entorn TGDL_PASSWORD.")
        throttled(request)
        if authm.configured and not await call(authm.check_password, body.current):
            authm.throttle.failure(request.state.client_address)
            raise HTTPException(status_code=400, detail="La contrasenya actual no és correcta.")
        problem = password_problem(body.new)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        await call(authm.set_password, body.new)
        set_session_cookie(request, response)
        return {"ok": True}

    # --------------------------------------------------------------- settings
    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return public_settings(store.get(), store.locked)

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any]) -> dict[str, Any]:
        current = store.get()
        known = {f.name for f in dataclasses.fields(Settings)} - {"password_hash"}
        changes = {k: v for k, v in body.items() if k in known and k not in store.locked}
        if changes.get("telegram_api_hash", None) in ("", None):
            changes.pop("telegram_api_hash", None)       # empty = keep the stored one
        if changes.get("api_key", None) in ("", None):
            changes.pop("api_key", None)
        errors = validation_errors(changes)
        if errors:
            name, message = next(iter(errors.items()))
            raise HTTPException(status_code=422, detail=f"«{name}»: {message}")
        new = await call(store.set, current.updated(changes))
        if (new.telegram_api_id, new.telegram_api_hash) != (current.telegram_api_id, current.telegram_api_hash):
            log.info("Credencials de l'API de Telegram canviades; es reinicia el client.")
            await call(tg.reconfigure)
        dl.wake()      # more parallel files, another folder…
        return public_settings(new, store.locked)

    @app.get("/api/storage")
    async def get_storage() -> dict[str, Any]:
        return (await call(storage.status, store.get())).to_dict()

    # --------------------------------------------------------------- telegram
    @app.get("/api/telegram/status")
    async def telegram_status() -> dict[str, Any]:
        return tg.status()

    @app.post("/api/telegram/qr")
    async def telegram_qr() -> dict[str, Any]:
        return await call(tg.start_qr)

    @app.get("/api/telegram/qr.svg")
    async def telegram_qr_svg() -> Response:
        url = tg.status().get("qr_url")
        if not url:
            raise HTTPException(status_code=404, detail="No hi ha cap codi QR actiu.")
        import io

        import segno

        buf = io.BytesIO()   # a standalone SVG document (svg_inline() lacks xmlns and won't load as <img>)
        segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff",
                                        xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.post("/api/telegram/code")
    async def telegram_send_code(body: PhoneRequest) -> dict[str, Any]:
        return await call(tg.send_code, body.phone)

    @app.post("/api/telegram/code/verify")
    async def telegram_verify_code(body: CodeRequest) -> dict[str, Any]:
        return await call(tg.submit_code, body.code)

    @app.post("/api/telegram/password")
    async def telegram_password(body: TwoFactorRequest) -> dict[str, Any]:
        return await call(tg.submit_password, body.password)

    @app.post("/api/telegram/cancel")
    async def telegram_cancel() -> dict[str, Any]:
        return await call(tg.cancel_login)

    @app.post("/api/telegram/logout")
    async def telegram_logout() -> dict[str, Any]:
        return await call(tg.logout)

    @app.get("/api/telegram/dialogs")
    async def telegram_dialogs() -> dict[str, Any]:
        dialogs = await call(tg.dialogs)
        sources = {s.chat_id: s.id for s in await call(db.sources)}
        for d in dialogs:
            d["source_id"] = sources.get(d["id"])
        return {"dialogs": dialogs}

    # ---------------------------------------------------------------- sources
    def require_source(source_id: int) -> None:
        if db.source(source_id) is None:
            raise HTTPException(status_code=404, detail="Aquesta font no existeix.")

    @app.get("/api/sources")
    async def list_sources() -> dict[str, Any]:
        sources = await call(db.sources)
        return {"sources": [tg.source_payload(s.id) for s in sources]}

    @app.post("/api/sources")
    async def add_source(body: SourceRequest) -> dict[str, Any]:
        dialogs = await call(tg.dialogs)
        dialog = next((d for d in dialogs if d["id"] == body.chat_id), None)
        if dialog is None:
            raise HTTPException(status_code=404, detail="Aquest xat no és a la llista de xats del compte.")
        options = SourceOptions.from_dict(body.options)
        src = await call(db.add_source, body.chat_id, dialog["title"], dialog["kind"], dialog["username"], options)
        log.info("Font afegida: «%s» (%s)", src.title, src.chat_id)
        await call(tg.index_source, src.id)
        return tg.source_payload(src.id) or {}

    @app.get("/api/sources/{source_id}")
    async def get_source(source_id: int) -> dict[str, Any]:
        payload = await call(tg.source_payload, source_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Aquesta font no existeix.")
        return payload

    @app.patch("/api/sources/{source_id}")
    async def update_source(source_id: int, body: SourceUpdate) -> dict[str, Any]:
        await call(require_source, source_id)
        src = db.source(source_id)
        assert src is not None
        merged = SourceOptions.from_dict({**src.options.to_dict(), **body.options})
        await call(db.set_source_options, source_id, merged)
        if (merged.tvdb_id, merged.numbering, merged.library_folder, merged.kind) != (
                src.options.tvdb_id, src.options.numbering, src.options.library_folder, src.options.kind):
            await call(auto_reconcile, source_id)
        return tg.source_payload(source_id) or {}

    @app.delete("/api/sources/{source_id}")
    async def delete_source(source_id: int) -> dict[str, Any]:
        await call(require_source, source_id)
        if tg.indexing(source_id):
            raise HTTPException(status_code=409, detail="S'està indexant aquesta font; espera que acabi.")
        if placer.busy(source_id):
            raise HTTPException(status_code=409, detail="S'estan col·locant fitxers d'aquesta font; espera que acabi.")
        dl.forget_source(source_id)
        await call(db.delete_source, source_id)
        events.publish("source_deleted", {"id": source_id})
        return {"ok": True}

    @app.post("/api/sources/{source_id}/index")
    async def index_source(source_id: int, body: Optional[IndexRequest] = None) -> dict[str, Any]:
        await call(require_source, source_id)
        started = await call(tg.index_source, source_id, full=bool(body and body.full))
        return {"started": started, "source": tg.source_payload(source_id)}

    @app.get("/api/sources/{source_id}/messages")
    async def source_messages(source_id: int, q: str = Query("", max_length=200),
                              state: str = Query("", max_length=20),
                              offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
                              photos: bool = False) -> dict[str, Any]:
        await call(require_source, source_id)
        if state and state not in STATES:
            raise HTTPException(status_code=422, detail="Estat desconegut.")
        total, items = await call(db.messages, source_id, q=q.strip(), state=state, offset=offset, limit=limit,
                                  include_photos=photos)
        src = db.source(source_id)
        numbering = src.options.numbering if src else "seasons"
        for item in items:
            parsed = parse_name(item["file_name"] or "", numbering)
            item["episode"] = review.episode_label(parsed.season, parsed.episode, parsed.absolute)
            item["volume"] = parsed.volume if parsed.is_archive else None
        return {"total": total, "offset": offset, "items": items}

    # ------------------------------------------------------------- check / import
    def source_show(src: Any, refresh: bool = False) -> tuple[Optional[Show], Optional[str]]:
        if not src.options.tvdb_id:
            return None, None
        try:
            return tvdb_client.show(src.options.tvdb_id, refresh=refresh), None
        except TvdbError as exc:
            return None, str(exc)

    def source_library(src: Any, show: Optional[Show]) -> tuple[dict[str, str], dict[str, Any]]:
        settings = store.get()
        info: dict[str, Any] = {"root": settings.library_tv_dir, "folder": None, "problem": None, "episodes": 0}
        if src.options.kind != "series" or not (src.options.tvdb_id or src.options.library_folder):
            return {}, info
        folder, problem, chosen = review.resolve_series_dir(settings, src.options)
        if chosen:
            info["chosen"] = True
        if folder is None:
            info["problem"] = problem
            return {}, info
        found = review.scan_library(folder, show, src.options.numbering)
        info.update(folder=str(folder), episodes=len(found))
        return found, info

    def run_review(source_id: int, refresh: bool) -> dict[str, Any]:
        src = db.source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Aquesta font no existeix.")
        show, tvdb_error = source_show(src, refresh)
        library, library_info = source_library(src, show)
        report = review.analyze(db.all_messages(source_id), numbering=src.options.numbering, show=show,
                                library=library)
        report["tvdb_error"] = tvdb_error
        report["library"] = library_info
        return report

    @app.get("/api/sources/{source_id}/review")
    async def source_review(source_id: int, refresh: bool = False) -> dict[str, Any]:
        return await call(run_review, source_id, refresh)

    def run_reconcile(source_id: int) -> dict[str, Any]:
        src = db.source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Aquesta font no existeix.")
        show, tvdb_error = source_show(src)
        library, info = source_library(src, show)
        if info["problem"] or tvdb_error or not info["folder"]:
            raise HTTPException(status_code=409, detail=info["problem"] or tvdb_error
                                or "Posa l'id de TheTVDB o tria la carpeta de la sèrie a «Opcions de la font».")
        releases, _others = review.build_releases(db.all_messages(source_id), src.options.numbering, show)
        marked = 0
        for rel in releases:
            if rel.ref is not None and rel.ref.key in library:
                marked += db.set_state(source_id, rel.msg_ids, IN_LIBRARY,
                                       only_from=(NEW, QUEUED, ERROR, DOWNLOADED))
        log.info("Biblioteca: %d episodis a %s; %d fitxers de «%s» marcats com a ja a la biblioteca",
                 len(library), info["folder"], marked, src.title)
        return {"folder": info["folder"], "episodes": len(library), "marked": marked,
                "source": tg.source_payload(source_id)}

    def auto_reconcile(source_id: int) -> None:
        """After indexing or changing the options: quietly mark what the library already has."""
        try:
            result = run_reconcile(source_id)
        except HTTPException:
            return
        if result.get("source"):
            events.publish("source", result["source"])

    tg.after_index = auto_reconcile

    # ------------------------------------------------------------- library (phase 4)
    placer = Placer(store.get, db, show_getter=lambda src: source_show(src)[0], publish=events.publish,
                    source_payload=tg.source_payload)
    app.state.placer = placer
    tg.payload_extra = lambda source_id: {"placing": placer.status(source_id), "placed": placer.last(source_id)}

    def after_download(source_id: int, _msg_id: int) -> None:
        """A file finished: sources placed directly get their complete episodes into the library."""
        src = db.source(source_id)
        if src is not None and src.options.kind == "series" and src.options.mode == "library":
            placer.place(source_id)

    dl.after_download = after_download

    @app.post("/api/sources/{source_id}/place")
    async def source_place(source_id: int, body: Optional[PlaceRequest] = None) -> dict[str, Any]:
        await call(require_source, source_id)
        src = db.source(source_id)
        assert src is not None
        if src.options.kind != "series":
            raise HTTPException(status_code=422, detail="De moment només es col·loquen sèries a la biblioteca.")
        started = placer.place(source_id, body.limit if body else None)
        return {"started": started, "source": tg.source_payload(source_id)}

    @app.post("/api/sources/{source_id}/reconcile")
    async def source_reconcile(source_id: int) -> dict[str, Any]:
        result = await call(run_reconcile, source_id)
        if result.get("source"):
            events.publish("source", result["source"])
        return result

    def run_import(source_id: int, body: ImportRequest) -> dict[str, Any]:
        src = db.source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Aquesta font no existeix.")
        settings = store.get()
        folder = Path(body.path.strip())
        if not folder.is_absolute():
            raise HTTPException(status_code=422, detail="Cal un camí absolut (per exemple /data/_conan/zips).")
        problem = storage.storage_problem(settings)
        if problem:
            raise HTTPException(status_code=409, detail=problem)
        if settings.storage_sentinel and not storage.is_within(folder, Path(settings.storage_sentinel).parent):
            raise HTTPException(status_code=422,
                                detail=f"La carpeta ha de ser dins de {Path(settings.storage_sentinel).parent}.")
        try:
            files = review.scan_tdl_dir(folder)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result: dict[str, Any] = {"folder": str(folder), "dry_run": body.dry_run, "found": 0, "imported": 0,
                                  "already": 0, "other_chat": 0, "unknown": [], "size_mismatch": []}
        for f in files:
            if not review.tdl_chat_matches(f.chat, src.chat_id):
                result["other_chat"] += 1
                continue
            result["found"] += 1
            msg = db.message(source_id, f.msg_id)
            if msg is None or msg["deleted"]:
                result["unknown"].append({"msg_id": f.msg_id, "name": f.name})
                continue
            if msg["size"] != f.size:
                result["size_mismatch"].append({"msg_id": f.msg_id, "name": f.name, "size": f.size,
                                                "expected": msg["size"]})
                continue
            if msg["state"] not in (NEW, QUEUED, ERROR):
                result["already"] += 1
                continue
            if body.dry_run or db.mark_downloaded(source_id, f.msg_id, f.path):
                result["imported"] += 1
        if not body.dry_run:
            log.info("Importació de tdl (%s): %d fitxers de «%s» importats, %d ja hi eren, %d de mida diferent",
                     folder, result["imported"], src.title, result["already"], len(result["size_mismatch"]))
        result["source"] = tg.source_payload(source_id)
        return result

    @app.post("/api/sources/{source_id}/import")
    async def source_import(source_id: int, body: ImportRequest) -> dict[str, Any]:
        result = await call(run_import, source_id, body)
        if not body.dry_run and result.get("source"):
            events.publish("source", result["source"])
        return result

    # -------------------------------------------------------------- downloads
    def run_queue(source_id: int, body: QueueRequest) -> dict[str, Any]:
        src = db.source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Aquesta font no existeix.")
        result: dict[str, Any] = {"queued": 0, "skipped_unrecognized": 0, "skipped_incomplete": 0, "in_library": 0}
        ids = list(body.msg_ids)
        if body.missing:
            show, tvdb_error = source_show(src)
            if tvdb_error:
                raise HTTPException(status_code=502, detail=tvdb_error)
            library, info = source_library(src, show)
            if info["problem"] and src.options.kind == "series":
                raise HTTPException(status_code=409, detail=info["problem"])
            want = review.wanted(db.all_messages(source_id), numbering=src.options.numbering, show=show,
                                 library=library, kind=src.options.kind)
            ids += want.pop("msg_ids")
            result.update(want)
        if not ids and not body.missing:
            raise HTTPException(status_code=422, detail="No hi ha cap fitxer per posar a la cua.")
        result["queued"] = db.set_state(source_id, sorted(set(ids)), QUEUED, only_from=(NEW, ERROR))
        if result["queued"]:
            log.info("«%s»: %d fitxers a la cua de baixades", src.title, result["queued"])
        result["source"] = tg.source_payload(source_id)
        return result

    @app.post("/api/sources/{source_id}/queue")
    async def source_queue(source_id: int, body: QueueRequest) -> dict[str, Any]:
        result = await call(run_queue, source_id, body)
        if result.get("source"):
            events.publish("source", result["source"])
        dl.wake()
        events.publish("downloads", await call(dl.status))
        return result

    @app.post("/api/sources/{source_id}/unqueue")
    async def source_unqueue(source_id: int, body: UnqueueRequest) -> dict[str, Any]:
        await call(require_source, source_id)
        changed = await call(dl.unqueue, source_id, body.msg_ids)
        payload = tg.source_payload(source_id)
        if payload:
            events.publish("source", payload)
        events.publish("downloads", await call(dl.status))
        return {"unqueued": changed, "source": payload}

    @app.get("/api/downloads")
    async def downloads_status() -> dict[str, Any]:
        return await call(dl.status)

    @app.post("/api/downloads/pause")
    async def downloads_pause() -> dict[str, Any]:
        return await call(dl.pause)

    @app.post("/api/downloads/resume")
    async def downloads_resume() -> dict[str, Any]:
        return await call(dl.resume)

    @app.get("/api/fs")
    async def browse_folders(path: str = Query("", max_length=4096)) -> dict[str, Any]:
        try:
            return await call(files.browse, path, store.get())
        except files.BrowseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.get("/api/tvdb/{tvdb_id}")
    async def tvdb_show(tvdb_id: int) -> dict[str, Any]:
        try:
            show = await call(tvdb_client.show, tvdb_id)
        except TvdbError as exc:
            raise HTTPException(status_code=404 if "cap sèrie" in str(exc) else 502, detail=str(exc)) from exc
        return {"id": show.tvdb_id, "title": show.title, "episodes": len(show.regular()), "stale": show.stale}

    # ----------------------------------------------------------------- events
    @app.get("/api/events")
    async def stream_events() -> StreamingResponse:
        sub = events.open(asyncio.get_running_loop())

        async def stream() -> AsyncIterator[str]:
            import json

            try:
                yield "retry: 3000\n\n"
                yield sse("telegram", json.dumps(tg.status(), ensure_ascii=False, default=str))
                yield sse("downloads", json.dumps(await run_in_threadpool(dl.status), ensure_ascii=False,
                                                  default=str))
                while True:
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is CLOSE:
                        break
                    name, data = item  # type: ignore[misc]
                    yield sse(name, data)
            finally:
                events.discard(sub)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ static UI (LAST)
    ui_dir = Path(web_dir) if web_dir is not None else resolve_web_dir()
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="web")
    else:
        log.warning("No s'ha trobat la interfície web: %s", ui_dir)

        @app.get("/", include_in_schema=False)
        async def missing_ui() -> HTMLResponse:
            return HTMLResponse(_MISSING_UI_HTML)

    return app
