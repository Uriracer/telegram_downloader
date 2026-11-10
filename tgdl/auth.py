"""Authentication for server mode: password, session cookies, API key.

* The password is stored as ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` in
  ``settings.password_hash`` (or given by ``TGDL_PASSWORD``, never stored).
* Sessions are stateless signed cookies ``<expiry>.<nonce>.<hmac>``. The HMAC
  key lives in ``<data_dir>/secret.key``; rotating it (password change) logs
  every session out.
* The API key (``X-Api-Key`` header or ``apikey`` query parameter) gives full
  access — it is what Sonarr/Radarr and scripts use.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

COOKIE_NAME = "tgdl_session"
SESSION_SECONDS = 30 * 24 * 3600
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
SECRET_FILE = "secret.key"

#: Failed logins allowed per client within ``THROTTLE_WINDOW`` seconds.
THROTTLE_MAX_FAILURES = 5
THROTTLE_WINDOW = 600.0
THROTTLE_LOCKOUT = 60.0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against :func:`hash_password` output."""
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iterations))
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> Optional[str]:
    """Catalan reason why ``password`` is not acceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"La contrasenya ha de tenir com a mínim {MIN_PASSWORD_LENGTH} caràcters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return "La contrasenya és massa llarga."
    return None


class LoginThrottle:
    """In-memory limit of failed logins per client address."""

    def __init__(self, max_failures: int = THROTTLE_MAX_FAILURES, window: float = THROTTLE_WINDOW,
                 lockout: float = THROTTLE_LOCKOUT, clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max_failures
        self._window = window
        self._lockout = lockout
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def retry_after(self, client: str) -> int:
        """Seconds the client must still wait (0 = may try)."""
        with self._lock:
            until = self._blocked_until.get(client, 0.0)
            remaining = until - self._clock()
            if remaining <= 0:
                self._blocked_until.pop(client, None)
                return 0
            return max(1, int(remaining + 0.999))

    def failure(self, client: str) -> None:
        now = self._clock()
        with self._lock:
            times = self._failures.setdefault(client, deque())
            times.append(now)
            while times and now - times[0] > self._window:
                times.popleft()
            if len(times) >= self._max:
                self._blocked_until[client] = now + self._lockout
                times.clear()
            if len(self._failures) > 10_000:  # bound memory on a hostile network
                self._failures.clear()

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
            self._blocked_until.pop(client, None)


class AuthManager:
    """Password/session/API-key checks shared by the middleware and endpoints.

    ``settings_get``/``settings_set`` read and persist the settings object
    (``password_hash`` and ``api_key`` fields).
    """

    def __init__(self, data_dir: Path | str | None = None, *, required: bool,
                 settings_get: Callable[[], Any], settings_set: Callable[[Any], Any],
                 password_env: Optional[str] = None) -> None:
        self.required = required
        self.password_env = password_env
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._get = settings_get
        self._set = settings_set
        self._lock = threading.Lock()
        self._secret: Optional[bytes] = None
        self.throttle = LoginThrottle()

    # ------------------------------------------------------------- password
    @property
    def password_from_env(self) -> bool:
        return bool(self.password_env)

    @property
    def configured(self) -> bool:
        return self.password_from_env or bool(getattr(self._get(), "password_hash", ""))

    def check_password(self, password: str) -> bool:
        if self.password_env:
            return hmac.compare_digest(password.encode("utf-8"), self.password_env.encode("utf-8"))
        encoded = getattr(self._get(), "password_hash", "")
        return bool(encoded) and verify_password(password, encoded)

    def set_password(self, password: str) -> None:
        """Store a new password hash and log every existing session out."""
        import dataclasses

        encoded = hash_password(password)
        with self._lock:
            self._set(dataclasses.replace(self._get(), password_hash=encoded))
            self._rotate_secret()

    # -------------------------------------------------------------- api key
    def check_api_key(self, key: Optional[str]) -> bool:
        expected = str(getattr(self._get(), "api_key", "") or "")
        return bool(key) and bool(expected) and hmac.compare_digest(key.encode("utf-8"), expected.encode("utf-8"))

    # ------------------------------------------------------------- sessions
    def _secret_path(self) -> Path:
        if self._data_dir is None:
            from .config import app_data_dir  # lazy: honours TGDL_DATA_DIR

            self._data_dir = app_data_dir()
        return self._data_dir / SECRET_FILE

    def _load_secret(self) -> bytes:
        if self._secret is not None:
            return self._secret
        path = self._secret_path()
        try:
            data = path.read_bytes().strip()
            if len(data) >= 32:
                self._secret = data
                return data
        except OSError:
            pass
        return self._rotate_secret()

    def _rotate_secret(self) -> bytes:
        secret = secrets.token_hex(32).encode("ascii")
        path = self._secret_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(secret)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:  # sessions then only last until restart
            log.warning("No s'ha pogut desar %s: %s", path, exc)
        self._secret = secret
        return secret

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._load_secret(), payload.encode("ascii"), hashlib.sha256).digest())

    def issue_session(self, now: Optional[float] = None) -> str:
        expiry = int((now if now is not None else time.time()) + SESSION_SECONDS)
        payload = f"{expiry}.{secrets.token_urlsafe(12)}"
        return f"{payload}.{self._sign(payload)}"

    def check_session(self, token: Optional[str], now: Optional[float] = None) -> bool:
        if not token or len(token) > 200:
            return False
        try:
            expiry_text, nonce, signature = token.split(".")
            expiry = int(expiry_text)
        except ValueError:
            return False
        if expiry < (now if now is not None else time.time()):
            return False
        expected = self._sign(f"{expiry_text}.{nonce}")
        return hmac.compare_digest(signature.encode("ascii", "replace"), expected.encode("ascii"))
