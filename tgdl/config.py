"""User settings: load/save as JSON in the data folder.

* ``app_data_dir()`` -> ``$TGDL_DATA_DIR`` if set (Docker: ``/config``), else
  ``%APPDATA%/TelegramDownloader`` on Windows, ``~/.telegram-downloader``
  elsewhere.
* Loading is forward compatible: unknown keys are ignored, missing or invalid
  values fall back to their defaults.
* Writes are atomic (temporary file + ``os.replace``).
* ``api_key`` (Sonarr/Radarr and scripts) is generated on first run.

The Telegram session itself is not here: it lives in ``telegram.session``
next to this file (see :mod:`tgdl.telegram.service`).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

APP_DIR_NAME = "TelegramDownloader"
FALLBACK_DIR_NAME = ".telegram-downloader"
SETTINGS_FILE = "settings.json"
DATA_DIR_ENV = "TGDL_DATA_DIR"

MAX_PARALLEL_FILES = 8
MAX_CHUNKS_PER_FILE = 16

_save_lock = threading.Lock()


def _new_api_key() -> str:
    return secrets.token_hex(16)


def _default_download_dir() -> str:
    return str(Path.home() / "Videos" / "Telegram")


@dataclass
class Settings:
    """Persisted preferences (field names are part of the REST API)."""
    # ---- Telegram (https://my.telegram.org -> API development tools)
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    # ---- folders
    #: Where finished downloads go (Sonarr/Radarr import from ``<dir>/<category>/<release>/``).
    download_dir: str = field(default_factory=_default_download_dir)
    #: ``.part`` files and extraction; empty = ``<download_dir>/.tmp`` (same disk: moves are instant).
    temp_dir: str = ""
    #: Library roots for sources placed directly (without Sonarr/Radarr).
    library_tv_dir: str = ""
    library_movies_dir: str = ""
    #: A file that only exists when the network share is mounted (e.g. ``/data/.tgdl-nas``).
    #: Nothing is written anywhere while it is missing.
    storage_sentinel: str = ""
    # ---- indexing
    index_interval_minutes: int = 60
    # ---- downloads
    max_parallel_files: int = 2
    chunks_per_file: int = 4
    #: Seconds without progress before a download is reconnected and resumed.
    stall_seconds: int = 90
    # ---- server
    server_port: int = 8766
    api_key: str = field(default_factory=_new_api_key)
    #: Login password (see tgdl/auth.py); never sent to clients.
    password_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Settings":
        """Build validated settings from untrusted data (unknown keys ignored)."""
        defaults = cls()
        data = data if isinstance(data, Mapping) else {}
        values = {f.name: _coerce(f.name, data.get(f.name, getattr(defaults, f.name)), getattr(defaults, f.name))
                  for f in fields(cls)}
        return cls(**values)

    def updated(self, changes: Mapping[str, Any]) -> "Settings":
        """A validated copy with ``changes`` applied (partial update)."""
        merged = self.to_dict()
        merged.update({k: v for k, v in dict(changes).items() if k in merged})
        if not merged.get("api_key"):
            merged["api_key"] = self.api_key
        return Settings.from_dict(merged)

    # ---- derived values
    @property
    def telegram_configured(self) -> bool:
        return self.telegram_api_id > 0 and len(self.telegram_api_hash) == 32

    @property
    def effective_temp_dir(self) -> str:
        return self.temp_dir or str(Path(self.download_dir) / ".tmp")


def _int_range(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool):
            raise TypeError("bool")
        number = int(value)
        if not low <= number <= high:
            raise ValueError(f"{number} fora de [{low}, {high}]")
        return number
    return check


def _path(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("text")
    return value.strip()


def _api_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text and (len(text) != 32 or any(c not in "0123456789abcdef" for c in text)):
        raise ValueError("l'api_hash són 32 caràcters hexadecimals")
    return text


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "telegram_api_id": _int_range(0, 2**31 - 1),
    "telegram_api_hash": _api_hash,
    "download_dir": _path,
    "temp_dir": _path,
    "library_tv_dir": _path,
    "library_movies_dir": _path,
    "storage_sentinel": _path,
    "index_interval_minutes": _int_range(0, 7 * 24 * 60),
    "max_parallel_files": _int_range(1, MAX_PARALLEL_FILES),
    "chunks_per_file": _int_range(1, MAX_CHUNKS_PER_FILE),
    "stall_seconds": _int_range(15, 3600),
    "server_port": _int_range(1024, 65535),
}


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Validate one value, returning ``default`` if unusable."""
    try:
        if name == "api_key":
            return value.strip() if isinstance(value, str) and value.strip() else _new_api_key()
        if name == "password_hash":
            return value if isinstance(value, str) and value.startswith("pbkdf2_sha256$") else ""
        if name == "download_dir":
            return _path(value) or default
        validator = _VALIDATORS.get(name)
        if validator is not None:
            return validator(value)
        return value if isinstance(value, type(default)) else default
    except (TypeError, ValueError) as exc:
        log.warning("Es descarta el valor de %s=%r (%s)", name, value, exc)
        return default


def validation_errors(changes: Mapping[str, Any]) -> dict[str, str]:
    """Catalan message per invalid field in ``changes`` (used by the API, which must not silently drop values)."""
    errors: dict[str, str] = {}
    for name, value in changes.items():
        validator = _VALIDATORS.get(name)
        if validator is None:
            continue
        try:
            validator(value)
        except (TypeError, ValueError) as exc:
            errors[name] = str(exc) or "valor no vàlid"
    return errors


def app_data_dir() -> Path:
    """Data folder (created if needed)."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        path = Path(override)
    elif os.environ.get("APPDATA"):
        path = Path(os.environ["APPDATA"]) / APP_DIR_NAME
    else:
        path = Path.home() / FALLBACK_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("No es pot crear la carpeta de dades %s: %s", path, exc)
    return path


def settings_path() -> Path:
    return app_data_dir() / SETTINGS_FILE


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temporary file in the same folder + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: target briefly locked (antivirus, indexer...).
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data: Any, *, compact: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else \
        json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_bytes(path, text.encode("utf-8"))


def load_settings(path: Path | str | None = None) -> Settings:
    """Load settings, creating/repairing the file when needed.

    A corrupt file is kept as ``settings.json.bak`` and defaults are used.
    """
    path = Path(path) if path is not None else settings_path()
    raw: dict[str, Any] | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        raw = loaded if isinstance(loaded, dict) else None
        if raw is None:
            raise ValueError("l'arrel no és un objecte")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("No es pot llegir %s (%s); s'usen els valors per defecte", path, exc)
        try:
            os.replace(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass

    settings = Settings.from_dict(raw or {})
    needs_save = raw is None or any(f.name not in raw for f in fields(Settings)) or (
        raw.get("api_key") != settings.api_key)
    if needs_save:
        try:
            save_settings(settings, path)
        except OSError as exc:
            log.warning("No es pot desar %s: %s", path, exc)
    return settings


def save_settings(s: Settings, path: Path | str | None = None) -> None:
    path = Path(path) if path is not None else settings_path()
    with _save_lock:
        atomic_write_json(path, s.to_dict())
        try:
            os.chmod(path, 0o600)   # holds the Telegram api_hash and the password hash
        except OSError:
            pass
