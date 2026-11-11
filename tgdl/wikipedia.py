"""Phase 6: Catalan episode titles and plots from the Catalan Wikipedia.

A series source names the episode list in its options (``wikipedia_list``:
``Llista d'episodis d'El detectiu Conan``). That page and the pages it links
to whose titles continue it (``… (temporades 21-30)``) hold tables like::

    | style=… | 942          ← Catalan numbering
    | style=… | 887          ← Japanese numbering = TheTVDB's absolute number = the file names
    | style=… | 1            ← episode in the season
    | '''En Kaito Kid i la caixa secreta. Primera part.'''<br />''"Kaitō Kiddo…"''
    | … | … | …
    |-
    | colspan=7 | Després de la mort del seu marit, …    ← plot

Entries are keyed by the **Japanese** number (the second number cell). The
result is cached in the database for a week; if Wikipedia does not answer, the
cached copy is used.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

import httpx

from .tvdb import USER_AGENT

log = logging.getLogger(__name__)

API_URL = "https://ca.wikipedia.org/w/api.php"
CACHE_SECONDS = 7 * 24 * 3600
CACHE_KEY = "wikipedia:{page}"
MAX_PAGES = 12


class WikipediaError(Exception):
    """Catalan message for the user."""


@dataclass
class Entry:
    number: int                  # Japanese numbering (absolute)
    title: str
    plot: str = ""
    catalan_number: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- wikitext

_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_EXTERNAL = re.compile(r"\[https?://\S+\s*([^\]]*)\]")
_TAG = re.compile(r"<[^>]+>")
_CELL = re.compile(r"^\|\s*(?:[^|\[\]{}\n]*=[^|\[\]{}\n]*\|(?!\|))?\s*(?P<text>.*)$")
_ROW_SPLIT = re.compile(r"\n\|-[^\n]*")


def clean(text: str) -> str:
    """Wiki markup → plain text."""
    text = _REF.sub("", text)
    for _ in range(4):                       # nested templates, innermost first
        text, n = _TEMPLATE.subn("", text)
        if not n:
            break
    text = _LINK.sub(r"\1", text)
    text = _EXTERNAL.sub(r"\1", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = _TAG.sub("", text)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    return " ".join(text.split())


def _cell(line: str) -> str:
    m = _CELL.match(line)
    return m["text"] if m else line[1:]


def parse_wikitext(text: str) -> dict[int, Entry]:
    """Episode rows of the tables in a page, by Japanese number."""
    out: dict[int, Entry] = {}
    pending: Optional[Entry] = None
    for row in _ROW_SPLIT.split(text):
        lines = [ln for ln in row.split("\n") if ln.startswith("|") and not ln.startswith(("|}", "|+", "|-"))]
        if not lines:
            continue
        cells = [_cell(ln) for ln in lines]
        numbers = [clean(c) for c in cells[:3]]
        if len(cells) >= 4 and all(n.isdigit() for n in numbers):
            bold = re.search(r"'''(.+?)'''", cells[3])
            title = clean(bold[1] if bold else cells[3].split("<br")[0]).rstrip(" .")
            if title:
                entry = Entry(number=int(numbers[1]), title=title, catalan_number=int(numbers[0]))
                out.setdefault(entry.number, entry)
                pending = entry
            continue
        if pending is not None and re.match(r"^\|\s*colspan", lines[0]):    # the plot row under the episode
            pending.plot = clean(cells[0])
        pending = None
    return out


# --------------------------------------------------------------------------- client

class WikipediaClient:
    def __init__(self, db: Any, *, transport: Optional[httpx.BaseTransport] = None,
                 cache_seconds: float = CACHE_SECONDS) -> None:
        self.db = db
        self._transport = transport
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[float, dict[int, Entry]]] = {}

    def _get(self, client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = client.get(API_URL, params={"format": "json", "formatversion": 2, **params})
        except httpx.HTTPError as exc:
            raise WikipediaError(f"No s'ha pogut consultar la Viquipèdia: {exc}") from exc
        if response.status_code != 200:
            raise WikipediaError(f"La Viquipèdia ha respost amb l'error {response.status_code}.")
        try:
            data = response.json()
        except ValueError as exc:
            raise WikipediaError("La resposta de la Viquipèdia no és vàlida.") from exc
        if "error" in data:
            raise WikipediaError(f"La Viquipèdia diu: {data['error'].get('info', data['error'])}")
        return data

    def _download(self, page: str) -> dict[int, Entry]:
        with httpx.Client(transport=self._transport, timeout=60, headers={"User-Agent": USER_AGENT},
                          follow_redirects=True) as client:
            links = self._get(client, {"action": "query", "prop": "links", "titles": page, "pllimit": 500})
            pages = links.get("query", {}).get("pages", [])
            if not pages or pages[0].get("missing"):
                raise WikipediaError(f"La Viquipèdia no té cap pàgina «{page}».")
            subpages = [lk["title"] for lk in pages[0].get("links", []) if lk["title"].startswith(page + " (")]
            entries: dict[int, Entry] = {}
            for title in [page, *subpages][:MAX_PAGES]:
                text = self._get(client, {"action": "parse", "page": title, "prop": "wikitext"})["parse"]["wikitext"]
                for number, entry in parse_wikitext(text).items():
                    entries.setdefault(number, entry)
        if not entries:
            raise WikipediaError(f"No s'ha trobat cap taula d'episodis a «{page}».")
        return entries

    def episodes(self, page: str, *, refresh: bool = False) -> dict[int, Entry]:
        page = page.strip()
        if not page:
            raise WikipediaError("Aquesta font no té la llista d'episodis de la Viquipèdia.")
        key = CACHE_KEY.format(page=page)
        with self._lock:
            cached = self._memory.get(page)
            if cached is None:
                stored = self.db.get_value(key)
                if stored:
                    try:
                        cached = (float(stored["fetched_at"]),
                                  {int(e["number"]): Entry(**e) for e in stored["entries"]})
                    except (KeyError, TypeError, ValueError):
                        cached = None
            if cached and not refresh and time.time() - cached[0] < self.cache_seconds:
                self._memory[page] = cached
                return cached[1]
            try:
                entries = self._download(page)
            except WikipediaError:
                if cached:
                    log.warning("La Viquipèdia no respon: es fa servir la còpia desada de «%s»", page)
                    return cached[1]
                raise
            fetched = (time.time(), entries)
            self.db.set_value(key, {"fetched_at": fetched[0], "entries": [e.to_dict() for e in entries.values()]})
            self._memory[page] = fetched
            log.info("Viquipèdia: «%s», %d episodis amb títol en català", page, len(entries))
            return entries
