"""TMDB film collections through Radarr's metadata service (``api.radarr.video``, no API key).

A source of films (``DC-Movie 22``…) names a TMDB collection in its options
(``tmdb_collection``: 39199 for the *Detective Conan Collection*). Film *N* is
the *N*-th of the collection by release date: the Conan films are numbered in
the order they came out, one a year since 1997. That gives Radarr the TMDB and
IMDb ids it searches by.

Cached in the database (``kv``) for a week; if the service is down, the cached
copy is used.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

from .tvdb import USER_AGENT

log = logging.getLogger(__name__)

COLLECTION_URL = "https://api.radarr.video/v1/movie/collection/{collection_id}"
CACHE_SECONDS = 7 * 24 * 3600
CACHE_KEY = "tmdb:collection:{collection_id}"


class TmdbError(Exception):
    """Catalan message for the user."""


@dataclass
class Film:
    tmdb_id: int
    title: str
    year: int = 0
    released: str = ""                  # ISO date of the cinema release ("" if unknown)
    imdb_id: str = ""
    original_title: str = ""
    runtime: int = 0                    # minutes
    titles: dict[str, str] = field(default_factory=dict)   # ISO 639-1 -> translated title

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Collection:
    collection_id: int
    name: str
    films: list[Film]                   # in release order: films[0] is film 1
    fetched_at: float = 0.0
    stale: bool = False

    def film(self, number: int) -> Optional[Film]:
        return self.films[number - 1] if 1 <= number <= len(self.films) else None

    def number_of(self, tmdb_id: int) -> Optional[int]:
        return next((i + 1 for i, f in enumerate(self.films) if f.tmdb_id == tmdb_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {"collection_id": self.collection_id, "name": self.name, "fetched_at": self.fetched_at,
                "films": [f.to_dict() for f in self.films]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Collection":
        return cls(collection_id=int(data["collection_id"]), name=str(data.get("name") or ""),
                   films=[Film(**f) for f in data.get("films") or []], fetched_at=float(data.get("fetched_at") or 0))


def _translations(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        lang = str(t.get("Language") or t.get("language") or "").strip().lower()[:2]
        title = str(t.get("Title") or t.get("title") or "").strip()
        if lang and title and lang not in out:
            out[lang] = title
    return out


def parse_collection(collection_id: int, data: dict[str, Any]) -> Collection:
    films = []
    for raw in data.get("Parts") or []:
        try:
            released = str(raw.get("InCinema") or raw.get("DigitalRelease") or raw.get("PhysicalRelease") or "")[:10]
            films.append(Film(tmdb_id=int(raw["TmdbId"]), title=str(raw.get("Title") or ""),
                              year=int(raw.get("Year") or 0), released=released,
                              imdb_id=str(raw.get("ImdbId") or ""), original_title=str(raw.get("OriginalTitle") or ""),
                              runtime=int(raw.get("Runtime") or 0), titles=_translations(raw.get("Translations"))))
        except (KeyError, TypeError, ValueError):
            continue
    films.sort(key=lambda f: (f.released or f"{f.year:04d}-12-31", f.year, f.tmdb_id))
    return Collection(collection_id=collection_id, name=str(data.get("Name") or ""), films=films,
                      fetched_at=time.time())


class TmdbClient:
    """TMDB collections by id, cached in the database."""

    def __init__(self, db: Any, *, transport: Optional[httpx.BaseTransport] = None,
                 cache_seconds: float = CACHE_SECONDS) -> None:
        self.db = db
        self._transport = transport
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._memory: dict[int, Collection] = {}

    def _download(self, collection_id: int) -> Collection:
        try:
            with httpx.Client(transport=self._transport, timeout=30, headers={"User-Agent": USER_AGENT},
                              follow_redirects=True) as client:
                response = client.get(COLLECTION_URL.format(collection_id=collection_id))
        except httpx.HTTPError as exc:
            raise TmdbError(f"No s'ha pogut consultar TMDB: {exc}") from exc
        if response.status_code == 404:
            raise TmdbError(f"TMDB no té cap col·lecció amb l'id {collection_id}.")
        if response.status_code != 200:
            raise TmdbError(f"TMDB ha respost amb l'error {response.status_code}.")
        try:
            return parse_collection(collection_id, response.json())
        except ValueError as exc:
            raise TmdbError("La resposta de TMDB no és vàlida.") from exc

    def collection(self, collection_id: int, *, refresh: bool = False) -> Collection:
        if collection_id <= 0:
            raise TmdbError("Aquesta font no té la col·lecció de TMDB.")
        key = CACHE_KEY.format(collection_id=collection_id)
        with self._lock:
            cached = self._memory.get(collection_id)
            if cached is None:
                stored = self.db.get_value(key)
                if stored:
                    try:
                        cached = Collection.from_dict(stored)
                    except (KeyError, TypeError, ValueError):
                        cached = None
            if cached and not refresh and time.time() - cached.fetched_at < self.cache_seconds:
                self._memory[collection_id] = cached
                return cached
            try:
                col = self._download(collection_id)
            except TmdbError:
                if cached:
                    log.warning("TMDB no respon: es fa servir la còpia desada de la col·lecció %s", collection_id)
                    cached.stale = True
                    return cached
                raise
            self.db.set_value(key, col.to_dict())
            self._memory[collection_id] = col
            log.info("TMDB: «%s», %d pel·lícules", col.name, len(col.films))
            return col
