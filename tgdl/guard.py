"""HTTP guard shared by every route (copied from 3Cat Downloader's server).

* ``Host`` header restricted to known names (local mode: loopback only, against
  DNS rebinding).
* State-changing requests must be same-origin (CSRF), unless they come from a
  non-browser client (Sonarr, curl), which still needs the API key.
* ``/api/*`` needs a session cookie or the API key when login is required.
* ``Cache-Control`` on every response.

Pure ASGI (no ``BaseHTTPMiddleware``) so SSE responses are not buffered.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlsplit

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import COOKIE_NAME, AuthManager
from .runtime import RuntimeConfig


def _hostname(netloc: str) -> str:
    try:
        return (urlsplit("//" + netloc).hostname or "").lower()
    except ValueError:
        return ""


#: ``/api`` paths reachable without logging in.
AUTH_EXEMPT_PATHS = frozenset({
    "/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/logout",
})


def _client_address(scope: Scope, headers: Headers, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64] or "?"
    client = scope.get("client")
    return str(client[0]) if client else "?"


def _is_https(scope: Scope, headers: Headers, trust_proxy: bool) -> bool:
    if scope.get("scheme") == "https":
        return True
    return trust_proxy and headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def _parse_cookies(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and name not in out:
            out[name] = value.strip().strip('"')
    return out


def _request_authenticated(scope: Scope, headers: Headers, auth: AuthManager) -> bool:
    key = headers.get("x-api-key")
    if key is None:
        values = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("apikey")
        key = values[0] if values else None
    if key is not None and auth.check_api_key(key):
        return True
    return auth.check_session(_parse_cookies(headers.get("cookie", "")).get(COOKIE_NAME))


class LocalGuardMiddleware:
    """Pure ASGI middleware: host/origin/auth checks and Cache-Control headers.

    Implemented without ``BaseHTTPMiddleware`` so streaming (SSE) responses
    are not buffered or wrapped. Fills ``scope["state"]`` with
    ``authenticated``, ``client_address`` and ``https``.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Optional[frozenset[str]],
                 runtime: Optional[RuntimeConfig] = None, auth: Optional[AuthManager] = None) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.runtime = runtime or RuntimeConfig()
        self.auth = auth

    async def _deny(self, scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
        await JSONResponse({"detail": detail}, status_code=status,
                           headers={"Cache-Control": "no-store"})(scope, receive, send)

    def _same_origin_ok(self, headers: Headers) -> bool:
        """CSRF rule for state-changing requests."""
        source = headers.get("origin")
        if source is None:
            source = headers.get("referer")
            if source is None:
                return True  # not a browser (curl, Sonarr...): auth still applies
        if source.strip().lower() == "null":
            return False
        try:
            netloc = urlsplit(source.strip()).netloc.lower()
        except ValueError:
            return False
        if not self.runtime.server:
            return _hostname(netloc) in (self.allowed_hosts or frozenset())
        return bool(netloc) and netloc == headers.get("host", "").strip().lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if self.allowed_hosts is not None and _hostname(headers.get("host", "")) not in self.allowed_hosts:
            await self._deny(scope, receive, send, 403, "Host no permès.")
            return
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and not self._same_origin_ok(headers):
            await self._deny(scope, receive, send, 403, "Origen no permès.")
            return

        path: str = scope.get("path", "")
        authenticated = True
        if self.auth is not None and self.auth.required:
            authenticated = _request_authenticated(scope, headers, self.auth)
            is_api = path == "/api" or path.startswith("/api/")
            if is_api and path not in AUTH_EXEMPT_PATHS and not authenticated:
                await self._deny(scope, receive, send, 401, "Cal iniciar sessió.")
                return
        state = scope.setdefault("state", {})
        state["authenticated"] = authenticated
        state["client_address"] = _client_address(scope, headers, self.runtime.trust_proxy)
        state["https"] = _is_https(scope, headers, self.runtime.trust_proxy)

        if path.startswith("/api/") or path == "/" or path.endswith((".html", "/")):
            cache_control = "no-store"
        else:
            cache_control = "no-cache"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if "cache-control" not in response_headers:
                    response_headers["Cache-Control"] = cache_control
            await send(message)

        await self.app(scope, receive, send_wrapper)
