"""TheTVDB episode lists through SkyHook (the metadata proxy Sonarr uses, no API key).

``https://skyhook.sonarr.tv/v1/tvdb/shows/en/<tvdb id>`` returns every episode
with ``seasonNumber``, ``episodeNumber`` and ``absoluteEpisodeNumber``: that is
how a Japanese absolute number (Conan 501) becomes the season/episode Sonarr
and Jellyfin use (S17E11).

The answer is cached in the database (``kv``) and refreshed once a day; if
SkyHook is down, the cached copy is used.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

import httpx

from . import __version__

log = logging.getLogger(__name__)

SKYHOOK_URL = "https://skyhook.sonarr.tv/v1/tvdb/shows/en/{tvdb_id}"
USER_AGENT = f"TelegramDownloader/{__version__} (+https://github.com/Uriracer/telegram_downloader)"
CACHE_SECONDS = 24 * 3600
CACHE_KEY = "tvdb:{tvdb_id}"


class TvdbError(Exception):
    """Catalan message for the user."""


@dataclass(frozen=True)
class Episode:
    season: int
    episode: int
    absolute: Optional[int]
    title: str = ""
    air_date: str = ""          # ISO date, "" if unknown

    @property
    def special(self) -> bool:
        return self.season == 0

    def aired(self, today: Optional[date] = None) -> bool:
        if not self.air_date:
            return False
        return self.air_date[:10] <= (today or date.today()).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Show:
    tvdb_id: int
    title: str
    episodes: list[Episode]
    fetched_at: float = 0.0
    stale: bool = False             # SkyHook failed: this is the cached copy
    _by_abs: dict[int, Episode] = field(default_factory=dict, repr=False, compare=False)
    _by_se: dict[tuple[int, int], Episode] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for ep in self.episodes:
            self._by_se[(ep.season, ep.episode)] = ep
            if ep.absolute and not ep.special:
                self._by_abs.setdefault(ep.absolute, ep)

    def by_absolute(self, number: int) -> Optional[Episode]:
        return self._by_abs.get(number)

    def by_season(self, season: int, episode: int) -> Optional[Episode]:
        return self._by_se.get((season, episode))

    def regular(self) -> list[Episode]:
        return [e for e in self.episodes if not e.special]

    def to_dict(self) -> dict[str, Any]:
        return {"tvdb_id": self.tvdb_id, "title": self.title, "fetched_at": self.fetched_at,
                "episodes": [[e.season, e.episode, e.absolute, e.title, e.air_date] for e in self.episodes]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Show":
        return cls(tvdb_id=int(data["tvdb_id"]), title=str(data.get("title") or ""),
                   fetched_at=float(data.get("fetched_at") or 0),
                   episodes=[Episode(int(s), int(e), int(a) if a else None, t or "", d or "")
                             for s, e, a, t, d in data.get("episodes", [])])


def parse_skyhook(tvdb_id: int, data: dict[str, Any]) -> Show:
    episodes = []
    for raw in data.get("episodes") or []:
        try:
            episodes.append(Episode(season=int(raw["seasonNumber"]), episode=int(raw["episodeNumber"]),
                                    absolute=int(raw["absoluteEpisodeNumber"]) if raw.get("absoluteEpisodeNumber")
                                    else None,
                                    title=str(raw.get("title") or ""), air_date=str(raw.get("airDate") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    episodes.sort(key=lambda e: (e.season, e.episode))
    return Show(tvdb_id=tvdb_id, title=str(data.get("title") or ""), episodes=episodes, fetched_at=time.time())


class TvdbClient:
    """Episode lists by TVDB id, cached in the database."""

    def __init__(self, db: Any, *, transport: Optional[httpx.BaseTransport] = None,
                 cache_seconds: float = CACHE_SECONDS) -> None:
        self.db = db
        self._transport = transport
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._memory: dict[int, Show] = {}

    def _download(self, tvdb_id: int) -> Show:
        url = SKYHOOK_URL.format(tvdb_id=tvdb_id)
        try:
            with httpx.Client(transport=self._transport, timeout=30, headers={"User-Agent": USER_AGENT},
                              follow_redirects=True) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise TvdbError(f"No s'ha pogut consultar TheTVDB: {exc}") from exc
        if response.status_code == 404:
            raise TvdbError(f"TheTVDB no té cap sèrie amb l'id {tvdb_id}.")
        if response.status_code != 200:
            raise TvdbError(f"TheTVDB ha respost amb l'error {response.status_code}.")
        try:
            return parse_skyhook(tvdb_id, response.json())
        except ValueError as exc:
            raise TvdbError("La resposta de TheTVDB no és vàlida.") from exc

    def show(self, tvdb_id: int, *, refresh: bool = False) -> Show:
        if tvdb_id <= 0:
            raise TvdbError("Aquesta font no té l'id de TheTVDB.")
        key = CACHE_KEY.format(tvdb_id=tvdb_id)
        with self._lock:
            cached = self._memory.get(tvdb_id)
            if cached is None:
                stored = self.db.get_value(key)
                if stored:
                    try:
                        cached = Show.from_dict(stored)
                    except (KeyError, TypeError, ValueError):
                        cached = None
            if cached and not refresh and time.time() - cached.fetched_at < self.cache_seconds:
                self._memory[tvdb_id] = cached
                return cached
            try:
                show = self._download(tvdb_id)
            except TvdbError:
                if cached:
                    log.warning("TheTVDB no respon: es fa servir la còpia desada de %s", tvdb_id)
                    cached.stale = True
                    return cached
                raise
            self.db.set_value(key, show.to_dict())
            self._memory[tvdb_id] = show
            log.info("TheTVDB: «%s», %d episodis", show.title, len(show.episodes))
            return show
