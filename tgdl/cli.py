"""Command line entry point (``tgdl`` / ``telegram-downloader``).

Starts the Telegram service and the HTTP server (FastAPI + uvicorn)::

    tgdl                              # http://127.0.0.1:8766/ (local mode, no login)
    tgdl --host 0.0.0.0               # server mode for the LAN (Docker…): login required
    tgdl --port 9000 --data-dir /srv/tgdl-config

Only one instance runs per data folder (see ``tgdl/instance.py``). SIGTERM and
SIGINT stop it gracefully; everything resumes on the next start.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import __version__

log = logging.getLogger("tgdl.run")

HEALTH_TIMEOUT = 30.0
STOP_TIMEOUT = 8.0      # docker stop grace period is 10 s by default

_shutdown = threading.Event()


def _log_level(debug: bool) -> int:
    if debug:
        return logging.DEBUG
    from .runtime import ENV_LOG_LEVEL, env

    level = logging.getLevelName((env(ENV_LOG_LEVEL) or "INFO").upper())
    return level if isinstance(level, int) else logging.INFO


def _setup_logging(debug: bool) -> Optional[Path]:
    """Log to stderr and ``<data dir>/logs/tgdl.log`` (events only: progress goes to the UI)."""
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file: Optional[Path] = None
    try:
        from .config import app_data_dir

        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "tgdl.log"
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3,
                                                             encoding="utf-8"))
    except Exception as exc:   # logging must never prevent start-up
        print(f"No s'ha pogut crear el fitxer de registre: {exc}", file=sys.stderr)
    level = _log_level(debug)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    if level > logging.DEBUG:
        for chatty in ("httpx", "telethon", "uvicorn.error"):
            logging.getLogger(chatty).setLevel(logging.WARNING)
    return log_file


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    from .runtime import DEFAULT_HOST, ENV_HOST, ENV_PORT, env

    parser = argparse.ArgumentParser(prog="tgdl", description="Recupera els fitxers dels teus xats de Telegram.")
    parser.add_argument("--host", default=None,
                        help=f"adreça on escoltar (per defecte {DEFAULT_HOST}; 0.0.0.0 = tota la xarxa) [{ENV_HOST}]")
    parser.add_argument("--port", type=int, default=None, help=f"port (per defecte 8766) [{ENV_PORT}]")
    parser.add_argument("--data-dir", default=None, help="carpeta de configuració i dades [TGDL_DATA_DIR]")
    parser.add_argument("--debug", action="store_true", help="registre detallat")
    parser.add_argument("--version", action="version", version=f"Telegram Downloader {__version__}")
    args = parser.parse_args(argv)
    if args.host is None:
        args.host = env(ENV_HOST) or DEFAULT_HOST
    if args.port is None and env(ENV_PORT):
        try:
            args.port = int(env(ENV_PORT) or "")
        except ValueError:
            parser.error(f"{ENV_PORT} ha de ser un número")
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("el port ha d'estar entre 0 i 65535")
    return args


def _install_signal_handlers() -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum: int, _frame: Any) -> None:
        log.info("Senyal %s rebut; aturant…", signum)
        _shutdown.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):   # pragma: no cover
                pass


def _connect_host(bind_host: str) -> str:
    return {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(bind_host, bind_host)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host.strip("[]"), port))
    except OSError:
        sock.close()
        raise
    return sock


def _healthy(host: str, port: int) -> bool:
    import httpx

    try:
        with httpx.Client(trust_env=False, timeout=0.5) as http:
            return http.get(f"http://{_url_host(host)}:{port}/api/health").status_code == 200
    except httpx.HTTPError:
        return False


def _lan_addresses() -> list[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # TEST-NET: nothing is sent
            address = s.getsockname()[0]
        return [] if address.startswith("127.") else [address]
    except OSError:
        return []


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    if args.data_dir:
        os.environ["TGDL_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    log_file = _setup_logging(args.debug)
    _shutdown.clear()
    _install_signal_handlers()

    from .config import app_data_dir
    from .instance import acquire_instance_lock, running_instance_port

    data_dir = app_data_dir()
    instance = acquire_instance_lock(data_dir)
    if instance is None:
        port = running_instance_port(data_dir)
        message = (f"Telegram Downloader ja s'està executant amb aquesta carpeta de dades ({data_dir})"
                   + (f", al port {port}." if port else "."))
        log.error(message)
        return 1
    try:
        return _serve(args, log_file, instance, data_dir)
    finally:
        instance.release()


def _serve(args: argparse.Namespace, log_file: Optional[Path], instance: Any, data_dir: Path) -> int:
    import uvicorn

    from . import storage
    from .db import open_database
    from .downloads import DownloadManager
    from .events import EventBroadcaster
    from .runtime import build_runtime, find_7zz
    from .server import SettingsStore, create_app
    from .telegram.service import TelegramService

    runtime = build_runtime(args.host, no_gui=True)
    store = SettingsStore()
    preferred = args.port if args.port is not None else int(store.get().server_port)
    try:
        sock = _bind(args.host, preferred)
    except OSError as exc:
        log.error("No es pot escoltar a %s:%s (%s). Hi ha un altre programa al port?", args.host, preferred, exc)
        return 2
    port = sock.getsockname()[1]
    connect = _connect_host(args.host)
    instance.publish(port)

    db = open_database(data_dir)
    events = EventBroadcaster()
    telegram = TelegramService(store.get, db, events.publish, data_dir=data_dir)
    telegram.start()
    downloads = DownloadManager(store.get, db, telegram, events.publish)
    downloads.start()
    app = create_app(store, db=db, telegram=telegram, events=events, runtime=runtime, data_dir=data_dir,
                     downloads=downloads)

    config = uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=3, lifespan="on",
                            proxy_headers=runtime.trust_proxy,
                            forwarded_allow_ips="*" if runtime.trust_proxy else None)
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="uvicorn", daemon=True)
    server_thread.start()

    settings = store.get()
    seven, seven_error = find_7zz()
    problem = storage.storage_problem(settings)
    urls = ", ".join(f"http://{ip}:{port}/" for ip in _lan_addresses()) if runtime.server else ""
    log.info("Telegram Downloader %s — mode %s, %s | dades: %s | baixades: %s | 7-Zip: %s | inici de sessió: %s",
             __version__, runtime.mode, urls or f"http://{_url_host(connect)}:{port}/", data_dir,
             settings.download_dir, seven or f"NO TROBAT ({seven_error})",
             "obligatori" if runtime.auth_required else "desactivat")
    if problem:
        log.warning(problem)
    exit_code = 0
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while not _healthy(connect, port):
            if not server_thread.is_alive() or _shutdown.is_set() or time.monotonic() > deadline:
                if not _shutdown.is_set():
                    log.error("El servidor no ha arrencat. Consulta el registre: %s", log_file or "(consola)")
                    exit_code = 1
                break
            time.sleep(0.1)
        else:
            log.info("A punt (Ctrl+C per aturar).")
            while server_thread.is_alive() and not _shutdown.wait(0.5):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Tancant…")
        events.close()
        server.should_exit = True
        try:
            telegram.stop(timeout=STOP_TIMEOUT)
        except Exception:
            log.exception("Error en aturar Telegram")
        server_thread.join(timeout=5)
        db.close()
        try:
            sock.close()
        except OSError:
            pass
        log.info("Aturat.")
    return exit_code


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
