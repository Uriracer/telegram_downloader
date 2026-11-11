"""Sonarr/Radarr, part 1: a Newznab indexer over the indexed Telegram sources.

Sonarr searches ``/arr/newznab/api`` (``t=caps|search|tvsearch|movie``) and gets
one *release* per episode found in the sources: all the volumes of its best
copy, named the way Sonarr parses (``Detective.Conan.S26E01.Title.CATALAN.720p.
WEB-DL-TGDL``) and with the real size. Each release links to a pseudo-NZB
(``/arr/nzb/<token>.nzb``) that only carries a **token**: which messages of
which source to download. Sonarr hands it to our SABnzbd API (:mod:`tgdl.sabnzbd`).

Sources are found by the TheTVDB id Sonarr sends (``tvdbid``), which is the id
in the source options, or by the series title. Films (Radarr) come from sources
of kind «movies» with a TMDB collection (:mod:`tgdl.tmdb`): ``DC-Movie 22`` is
the 22nd film of the collection, searched by its TMDB or IMDb id.

Episodes and films that are already in the library are **never offered**:
Sonarr/Radarr would otherwise "upgrade" them (an SDTV file → our WEB-DL name)
by downloading the very same file again.

**Scene numbering.** For anime like Detective Conan, Sonarr searches with the
scene numbers of TheXEM (``season=1&ep=887`` for TheTVDB's S26E01). An episode
whose absolute number is the ``ep`` asked for is returned named with the
numbers Sonarr asked (``S01E887``), so it maps it back to the right episode.

Same formats as 3Cat Downloader's integration, which works with Sonarr 4 and
Radarr 6.
"""
from __future__ import annotations

import base64
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

from . import review
from .db import EXTRACTED, IN_LIBRARY, Database

#: Message states that mean "this episode is already in the library (or on its way to Sonarr)".
_DONE = frozenset({IN_LIBRARY, EXTRACTED})

GROUP = "TGDL"
NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"
TOKEN_META = "x-tgdl-token"
APP_TITLE = "Telegram Downloader"

#: Newznab categories.
CAT_TV, CAT_TV_SD, CAT_TV_HD = 5000, 5030, 5040
CAT_MOVIES, CAT_MOVIES_SD, CAT_MOVIES_HD = 2000, 2030, 2040

#: Audio language of a source (options.language) as Sonarr/Radarr name it, and its release tag.
LANGUAGES = {"ca": ("Catalan", "CATALAN"), "es": ("Spanish", "SPANISH"), "en": ("English", ""),
             "ja": ("Japanese", "JAPANESE"), "fr": ("French", "FRENCH"), "it": ("Italian", "ITALIAN"),
             "de": ("German", "GERMAN"), "pt": ("Portuguese", "PORTUGUESE")}

RSS_LIMIT = 100
_RESOLUTION = re.compile(r"(?<!\d)(2160|1080|720|576|480)[pi](?![a-z])", re.I)


# --------------------------------------------------------------------------- names

def ascii_fold(text: str) -> str:
    """"Merlí · l'Estiu" → "Merli l'Estiu" (accents and l·l removed)."""
    text = (text or "").replace("l·l", "ll").replace("L·L", "LL").replace("·", "")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Lower-case ASCII words: "El detectiu Conan (1996)" → "el detectiu conan 1996"."""
    return " ".join(re.findall(r"[a-z0-9]+", ascii_fold(text or "").lower().replace("&", " and ")))


def dotted(text: str, max_len: int = 80) -> str:
    """Scene-style name part: ASCII words joined with dots."""
    words = re.findall(r"[A-Za-z0-9]+", ascii_fold(text).replace("'", ""))
    out = ".".join(words)
    if len(out) > max_len:
        out = out[:max_len].rsplit(".", 1)[0] or out[:max_len]
    return out


def resolution(file_names: list[str]) -> str:
    """``720p``, ``1080p``… from the file names (Telegram backups seldom say: 720p by default)."""
    for name in file_names:
        m = _RESOLUTION.search(name)
        if m:
            return f"{m[1]}p"
    return "720p"


def episode_release_name(series: str, season: int, episode: int, title: str, language: str, res: str) -> str:
    """``Detective.Conan.S26E01.Kaitou.Kid.and.the.Trick.Box.Part.One.CATALAN.720p.WEB-DL-TGDL``."""
    parts = [dotted(series) or "Telegram", f"S{season:02d}E{episode:02d}"]
    ep_title = dotted(title, 60)
    if ep_title:
        parts.append(ep_title)
    tag = LANGUAGES.get(language, ("", ""))[1]
    if tag:
        parts.append(tag)
    parts += [res, f"WEB-DL-{GROUP}"]
    return ".".join(parts)


# --------------------------------------------------------------------------- tokens and NZB

@dataclass
class Token:
    """What a release downloads: these messages (in volume order) of this source."""
    source_id: int
    msg_ids: list[int]
    title: str
    category: str = "tv"

    def encode(self) -> str:
        data = {"s": self.source_id, "m": self.msg_ids, "t": self.title, "c": self.category}
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, text: str) -> "Token":
        try:
            text = text.strip()
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            data: Any = json.loads(raw.decode("utf-8"))
            msg_ids = [int(m) for m in data["m"]]
            title = str(data["t"]).strip()
            if not msg_ids or not title or len(msg_ids) > 500:
                raise ValueError("buit")
            return cls(source_id=int(data["s"]), msg_ids=msg_ids, title=title,
                       category=str(data.get("c") or "tv"))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("Testimoni de llançament no vàlid.") from exc


def build_nzb(token: Token) -> bytes:
    """A syntactically valid NZB (one ``file``) that carries the token; nothing to do with Usenet."""
    encoded = token.encode()
    title = escape(token.title)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
        f'<nzb xmlns="{NZB_NS}">\n'
        f'  <head>\n    <meta type="name">{title}</meta>\n'
        f'    <meta type="{TOKEN_META}">{encoded}</meta>\n  </head>\n'
        f'  <file poster="{APP_TITLE}" date="0" subject="{title} [telegram]">\n'
        '    <groups><group>alt.binaries.telegram</group></groups>\n'
        f'    <segments><segment bytes="1" number="1">{encoded[:200]}@telegram.invalid</segment></segments>\n'
        '  </file>\n</nzb>\n'
    ).encode("utf-8")


def token_from_nzb(data: bytes) -> Token:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("El fitxer NZB no és vàlid.") from exc
    for meta in root.iter():
        if meta.tag.endswith("meta") and meta.get("type") == TOKEN_META and meta.text:
            return Token.decode(meta.text)
    raise ValueError("Aquest NZB no és de Telegram Downloader.")


# --------------------------------------------------------------------------- feed

@dataclass
class Item:
    title: str
    token: Token
    size: int
    category: int
    published: Optional[str] = None       # ISO date-time of the newest message
    season: Optional[int] = None
    episode: Optional[int] = None
    tvdb_id: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: str = ""                     # "tt0131479"
    absolute: Optional[int] = None
    language: str = ""
    description: str = ""
    guid: str = ""


def _rfc822(iso: Optional[str]) -> str:
    try:
        moment = datetime.fromisoformat(str(iso))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        moment = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def rss_feed(items: list[Item], *, base_url: str, api_key: str, offset: int = 0, total: Optional[int] = None) -> str:
    base = base_url.rstrip("/")
    out = []
    for it in items:
        link = f"{base}/arr/nzb/{it.token.encode()}.nzb?apikey={api_key}"
        attrs = [("category", str(it.category)), ("size", str(it.size))]
        if it.season is not None:
            attrs.append(("season", str(it.season)))
        if it.episode is not None:
            attrs.append(("episode", str(it.episode)))
        if it.tvdb_id:
            attrs.append(("tvdbid", str(it.tvdb_id)))
        if it.tmdb_id:
            attrs.append(("tmdbid", str(it.tmdb_id)))
        if it.imdb_id.lower().removeprefix("tt").isdigit():
            attrs.append(("imdb", it.imdb_id.lower().removeprefix("tt")))     # Radarr reads digits only
        if it.language:
            attrs.append(("language", it.language))
        attr_xml = "".join(f'<newznab:attr name="{n}" value="{escape(v)}"/>' for n, v in attrs)
        out.append(
            "<item>"
            f"<title>{escape(it.title)}</title>"
            f'<guid isPermaLink="false">{escape(it.guid or it.title)}</guid>'
            f"<link>{escape(link)}</link>"
            f"<pubDate>{_rfc822(it.published)}</pubDate>"
            f"<category>{it.category}</category>"
            f"<description>{escape(it.description or it.title)}</description>"
            f'<enclosure url="{escape(link)}" length="{it.size}" type="application/x-nzb"/>'
            f"{attr_xml}</item>")
    count = len(items) if total is None else total
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
            'xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            f"<channel><title>{APP_TITLE}</title><description>Fitxers dels teus xats de Telegram</description>"
            f"<link>{escape(base)}/</link><language>ca</language>"
            f'<newznab:response offset="{offset}" total="{count}"/>'
            + "".join(out) + "</channel></rss>")


def caps_xml() -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><caps>'
            f'<server version="1.0" title="{APP_TITLE}" strapline="Fitxers dels teus xats de Telegram"/>'
            '<limits max="200" default="100"/><registration available="no" open="no"/>'
            "<searching>"
            '<search available="yes" supportedParams="q"/>'
            '<tv-search available="yes" supportedParams="q,season,ep,tvdbid"/>'
            '<movie-search available="yes" supportedParams="q,imdbid,tmdbid"/>'
            "</searching><categories>"
            f'<category id="{CAT_MOVIES}" name="Movies"><subcat id="{CAT_MOVIES_SD}" name="SD"/>'
            f'<subcat id="{CAT_MOVIES_HD}" name="HD"/></category>'
            f'<category id="{CAT_TV}" name="TV"><subcat id="{CAT_TV_SD}" name="SD"/>'
            f'<subcat id="{CAT_TV_HD}" name="HD"/></category>'
            "</categories></caps>")


def error_xml(code: int, description: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><error code="{code}" description="{escape(description)}"/>'


# --------------------------------------------------------------------------- search

#: Name of the placeholder release (a series nobody has, so Sonarr never grabs it).
PLACEHOLDER_SERIES = "Telegram Downloader Feed"


def movie_release_name(title: str, year: int, language: str, res: str) -> str:
    """``Case.Closed.Zero.the.Enforcer.2018.CATALAN.1080p.WEB-DL-TGDL``."""
    parts = [dotted(title) or "Telegram"]
    if year:
        parts.append(str(year))
    tag = LANGUAGES.get(language, ("", ""))[1]
    if tag:
        parts.append(tag)
    parts += [res, f"WEB-DL-{GROUP}"]
    return ".".join(parts)


def placeholder_item(movies: bool = False) -> Item:
    """What the feed holds when there is nothing to offer.

    Sonarr/Radarr refuse to save an indexer whose feed is empty ("no results in the configured
    categories"), and here it is empty whenever every episode is already in the library. This
    release belongs to no real series: Sonarr's RSS sync skips it as an unknown series, and the
    pseudo-NZB names no messages, so even a manual grab fails harmlessly.
    """
    if movies:
        title = movie_release_name(PLACEHOLDER_SERIES, 1900, "", "720p")
        return Item(title=title, token=Token(0, [0], title, "movies"), size=1024 ** 2, category=CAT_MOVIES_HD,
                    published=datetime.now(timezone.utc).isoformat(),
                    description="Telegram Downloader: no hi ha res de nou", guid="tgdl-placeholder-movies")
    title = episode_release_name(PLACEHOLDER_SERIES, 1, 1, "Nothing new", "", "720p")
    return Item(title=title, token=Token(0, [0], title), size=1024 ** 2, category=CAT_TV_HD,
                published=datetime.now(timezone.utc).isoformat(), season=1, episode=1,
                description="Telegram Downloader: no hi ha res de nou", guid="tgdl-placeholder")


def renumbered(item: Item, season: int, episode: int) -> Item:
    """The same release named with other numbers (``S26E01`` → ``S01E887``)."""
    old = f".S{item.season:02d}E{item.episode:02d}."
    title = item.title.replace(old, f".S{season:02d}E{episode:02d}.", 1)
    return Item(title=title, token=Token(item.token.source_id, item.token.msg_ids, title, item.token.category),
                size=item.size, category=item.category, published=item.published, season=season,
                episode=episode, tvdb_id=item.tvdb_id, absolute=item.absolute, language=item.language,
                description=item.description, guid=f"{item.guid}-s{season}e{episode}")


@dataclass
class Indexer:
    db: Database
    show_getter: Callable[[Any], Any]                 # source -> tvdb.Show | None
    collection_getter: Callable[[Any], Any] = lambda src: None   # source -> tmdb.Collection | None

    def _series_sources(self) -> list[Any]:
        return [s for s in self.db.sources() if s.options.kind == "series"]

    def find_sources(self, q: str = "", tvdb_id: Optional[int] = None) -> list[Any]:
        """Series sources for a search: by TheTVDB id, else by title (source or TheTVDB title)."""
        sources = self._series_sources()
        if tvdb_id:
            return [s for s in sources if s.options.tvdb_id == tvdb_id]
        wanted = normalize(q)
        if not wanted:
            return sources
        out = []
        for src in sources:
            names = {normalize(src.title)}
            show = self.show_getter(src) if src.options.tvdb_id else None
            if show is not None:
                names.add(normalize(show.title))
            if wanted in names or any(wanted in n.split(" (")[0] for n in names if len(wanted) >= 4):
                out.append(src)
        return out

    def episodes(self, src: Any) -> list[Item]:
        """One release per episode of a source: the copy to download (complete, highest version)."""
        show = self.show_getter(src) if src.options.tvdb_id else None
        messages = [m for m in self.db.all_messages(src.id) if m.get("kind") != "photo"]
        releases, _others = review.build_releases(messages, src.options.numbering, show)
        by_key: dict[str, list[review.Release]] = {}
        for rel in releases:
            if rel.ref is not None and rel.ref.season is not None and rel.ref.episode is not None:
                by_key.setdefault(rel.ref.key, []).append(rel)
        series = show.title if show is not None else src.title
        language = src.options.language
        items = []
        for rels in by_key.values():
            if any(m["state"] in _DONE for rel in rels for m in rel.messages):
                continue                       # already in the library: never offered again
            best = review.best_release(rels)
            if not best.complete:
                continue
            ref = best.ref
            assert ref is not None and ref.season is not None and ref.episode is not None
            chosen = [max(best.volumes[n], key=lambda m: m["msg_id"]) for n in sorted(best.volumes)]
            title = ref.tvdb.title if ref.tvdb is not None else best.parsed.title
            res = resolution([m["file_name"] for m in chosen])
            name = episode_release_name(series, ref.season, ref.episode, title, language, res)
            items.append(Item(
                title=name, token=Token(src.id, [m["msg_id"] for m in chosen], name, "tv"),
                size=sum(int(m["size"] or 0) for m in chosen),
                category=CAT_TV_HD if res in ("720p", "1080p", "2160p") else CAT_TV_SD,
                published=max((m.get("date") or "" for m in chosen), default="") or None,
                season=ref.season, episode=ref.episode, tvdb_id=src.options.tvdb_id or None, absolute=ref.absolute,
                language=LANGUAGES.get(language, ("", ""))[0],
                description=f"{src.title}: {best.name}", guid=f"tgdl-{src.id}-{chosen[0]['msg_id']}"))
        return items

    def tv_search(self, q: str = "", season: Optional[str] = None, ep: Optional[str] = None,
                  tvdb_id: Optional[int] = None) -> list[Item]:
        out: list[Item] = []
        for src in self.find_sources(q, tvdb_id):
            out += self.episodes(src)
        try:
            want_season = int(str(season)) if season not in (None, "") else None
            want_ep = int(str(ep)) if ep not in (None, "") else None
        except ValueError:
            return []
        if want_season is not None or want_ep is not None:
            exact = [i for i in out if (want_season is None or i.season == want_season)
                     and (want_ep is None or i.episode == want_ep)]
            if not exact and want_ep is not None:          # scene numbering: ep = absolute number
                exact = [renumbered(i, want_season if want_season is not None else 1, want_ep)
                         for i in out if i.absolute == want_ep]
            out = exact
        out.sort(key=lambda i: (i.season or 0, i.episode or 0))
        return out

    # -------------------------------------------------------------- films
    def films(self, src: Any) -> list[Item]:
        """One release per film of a source of films (the copy to download), named after TMDB."""
        collection = self.collection_getter(src) if src.options.tmdb_collection else None
        if collection is None:
            return []
        messages = [m for m in self.db.all_messages(src.id) if m.get("kind") != "photo"]
        releases, _others = review.build_releases(messages, src.options.numbering, None)
        by_number: dict[int, list[review.Release]] = {}
        for rel in releases:
            if rel.parsed.movie is not None:
                by_number.setdefault(rel.parsed.movie, []).append(rel)
        language = src.options.language
        items = []
        for number, rels in by_number.items():
            film = collection.film(number)
            if film is None or any(m["state"] in _DONE for rel in rels for m in rel.messages):
                continue
            best = review.best_release(rels)
            if not best.complete:
                continue
            chosen = [max(best.volumes[n], key=lambda m: m["msg_id"]) for n in sorted(best.volumes)]
            res = resolution([m["file_name"] for m in chosen])
            name = movie_release_name(film.title, film.year, language, res)
            items.append(Item(
                title=name, token=Token(src.id, [m["msg_id"] for m in chosen], name, "movies"),
                size=sum(int(m["size"] or 0) for m in chosen),
                category=CAT_MOVIES_HD if res in ("720p", "1080p", "2160p") else CAT_MOVIES_SD,
                published=max((m.get("date") or "" for m in chosen), default="") or None,
                tmdb_id=film.tmdb_id, imdb_id=film.imdb_id, language=LANGUAGES.get(language, ("", ""))[0],
                description=f"{src.title}: {best.name}", guid=f"tgdl-{src.id}-{chosen[0]['msg_id']}"))
        return items

    def _film_sources(self) -> list[Any]:
        return [s for s in self.db.sources() if s.options.kind == "movies"]

    def movie_search(self, q: str = "", tmdb_id: Optional[int] = None, imdb_id: Optional[str] = None) -> list[Item]:
        items = [i for src in self._film_sources() for i in self.films(src)]
        if tmdb_id or imdb_id:
            imdb = (imdb_id or "").lower().removeprefix("tt")
            return [i for i in items if (tmdb_id and i.tmdb_id == tmdb_id)
                    or (imdb and i.imdb_id.lower().removeprefix("tt") == imdb)]
        wanted = normalize(q)
        if not wanted:
            return items
        out = []
        for i in items:
            name = normalize(" ".join(i.title.split(".")[:-3]))        # title + year, without tags
            if wanted == name or wanted in name or name in wanted:
                out.append(i)
        return out

    def movie_rss(self) -> list[Item]:
        items = [i for src in self._film_sources() for i in self.films(src)]
        items.sort(key=lambda i: i.published or "", reverse=True)
        return items[:RSS_LIMIT]

    def rss(self) -> list[Item]:
        """The newest episodes of every series source (what Sonarr's RSS sync looks at)."""
        items = [i for src in self._series_sources() for i in self.episodes(src)]
        items.sort(key=lambda i: i.published or "", reverse=True)
        return items[:RSS_LIMIT]
