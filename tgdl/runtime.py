"""Runtime configuration derived from the command line and environment.

Two modes:

* **local** — bound to a loopback address (development, ``127.0.0.1``): no
  login unless ``TGDL_AUTH=required``, strict Host/Origin checks.
* **server** — bound to a non-loopback address (Docker, Proxmox…): login
  required, any Host accepted unless restricted, CSRF protection by
  same-origin checks.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

ENV_HOST = "TGDL_HOST"
ENV_PORT = "TGDL_PORT"
ENV_AUTH = "TGDL_AUTH"
ENV_PASSWORD = "TGDL_PASSWORD"
ENV_API_KEY = "TGDL_API_KEY"
ENV_DOWNLOAD_DIR = "TGDL_DOWNLOAD_DIR"
ENV_ALLOWED_HOSTS = "TGDL_ALLOWED_HOSTS"
ENV_TRUST_PROXY = "TGDL_TRUST_PROXY"
ENV_7ZZ = "TGDL_7ZZ"
ENV_LOG_LEVEL = "TGDL_LOG_LEVEL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
LOOPBACK_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Settings fields that an environment variable can force (field -> variable).
LOCKABLE_FIELDS: dict[str, str] = {
    "download_dir": ENV_DOWNLOAD_DIR,
    "temp_dir": "TGDL_TEMP_DIR",
    "library_tv_dir": "TGDL_LIBRARY_TV",
    "library_movies_dir": "TGDL_LIBRARY_MOVIES",
    "storage_sentinel": "TGDL_STORAGE_SENTINEL",
    "telegram_api_id": "TGDL_API_ID",
    "telegram_api_hash": "TGDL_API_HASH",
    "api_key": ENV_API_KEY,
}


def env(name: str) -> Optional[str]:
    """Environment value with surrounding whitespace removed; None if empty."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_flag(name: str) -> bool:
    return (env(name) or "").lower() in ("1", "true", "yes", "on")


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 and "localhost"."""
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class RuntimeConfig:
    """How this process serves the UI/API (immutable once the server starts)."""
    mode: str = "local"                                  # "local" | "server"
    auth_required: bool = False
    #: Host names accepted in the Host header; None = any host.
    allowed_hosts: Optional[frozenset[str]] = LOOPBACK_NAMES
    password_env: Optional[str] = field(default=None, repr=False)
    trust_proxy: bool = False

    @property
    def server(self) -> bool:
        return self.mode == "server"


def build_runtime(host: str, *, no_gui: bool = False) -> RuntimeConfig:
    """Derive the runtime config from the bind host and environment."""
    auth_setting = (env(ENV_AUTH) or "auto").lower()
    if auth_setting not in ("auto", "required", "none"):
        log.warning("%s=%r no és vàlid (auto|required|none); s'utilitza 'auto'.", ENV_AUTH, auth_setting)
        auth_setting = "auto"

    server = not is_loopback(host) or (no_gui and auth_setting == "required")
    if not server:
        return RuntimeConfig(mode="local", auth_required=auth_setting == "required",
                             allowed_hosts=LOOPBACK_NAMES, password_env=env(ENV_PASSWORD))

    allowed: Optional[frozenset[str]] = None
    raw_hosts = env(ENV_ALLOWED_HOSTS)
    if raw_hosts and raw_hosts != "*":
        names = {h.strip().lower() for h in raw_hosts.split(",") if h.strip()}
        if "*" not in names:
            allowed = frozenset(names | LOOPBACK_NAMES)
    auth_required = auth_setting != "none"
    if not auth_required:
        log.warning("ATENCIÓ: %s=none — qualsevol persona de la xarxa pot fer servir l'aplicació.", ENV_AUTH)
    return RuntimeConfig(mode="server", auth_required=auth_required, allowed_hosts=allowed,
                         password_env=env(ENV_PASSWORD), trust_proxy=env_flag(ENV_TRUST_PROXY))


# --------------------------------------------------------------------------- 7-Zip

#: Names of the official 7-Zip console binary (7zz opens RAR5; Debian's 7z does not).
SEVEN_ZIP_NAMES = ("7zz", "7z", "7za")


def find_7zz() -> tuple[Optional[str], Optional[str]]:
    """Locate 7-Zip: ``TGDL_7ZZ`` → ``7zz``/``7z`` on ``PATH`` → the usual Windows folder.

    Returns ``(path, None)`` or ``(None, catalan_error)``.
    """
    import shutil

    forced = env(ENV_7ZZ)
    if forced:
        path = forced if os.path.isfile(forced) else shutil.which(forced)
        return (path, None) if path else (None, f"No s'ha trobat 7-Zip a «{forced}» (variable {ENV_7ZZ}).")
    for name in SEVEN_ZIP_NAMES:
        path = shutil.which(name)
        if path:
            return path, None
    for folder in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        candidate = os.path.join(folder, "7-Zip", "7z.exe") if folder else ""
        if candidate and os.path.isfile(candidate):
            return candidate, None
    return None, ("No s'ha trobat 7-Zip (7zz). La imatge Docker ja l'inclou; en una instal·lació pròpia, "
                  f"baixa'l de https://www.7-zip.org/ o indica'n la ruta amb {ENV_7ZZ}.")
