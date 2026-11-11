"""Check a source before downloading anything, import tdl downloads, reconcile with the library.

* :func:`analyze` groups the files of a source into **releases** (all volumes
  of a multipart archive are one release), maps each one to a TheTVDB episode
  and reports what is missing, duplicated (V2/V3, re-uploads), unrecognized,
  not on TheTVDB or incomplete.
* :func:`scan_library` finds the episodes already in the Jellyfin folder of
  the series (``… [tvdbid-72454]/Season 17/… S17E11 …``): those are never
  downloaded again.
* :func:`scan_tdl_dir` reads the files ``tdl`` left (``<chat>_<msg>_<name>``)
  so they are imported instead of downloaded again.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .naming import ABSOLUTE, VIDEO_EXTS, ParsedName, parse, split_tdl_name
from .tvdb import Episode, Show

log = logging.getLogger(__name__)

#: Above this many problems of one kind, the lists are cut (the summary keeps the real counts).
LIST_LIMIT = 500


def episode_label(season: Optional[int], episode: Optional[int], absolute: Optional[int]) -> str:
    parts = []
    if season is not None and episode is not None:
        parts.append(f"S{season:02d}E{episode:02d}")
    if absolute is not None:
        parts.append(f"{absolute:04d}" if parts else str(absolute))
    return " · ".join(parts)


@dataclass
class EpisodeRef:
    """Where a parsed name points: a TheTVDB episode, or just the numbers when there is none."""
    key: str
    season: Optional[int]
    episode: Optional[int]
    absolute: Optional[int]
    tvdb: Optional[Episode] = None

    @property
    def label(self) -> str:
        return episode_label(self.season, self.episode, self.absolute)


def resolve(parsed: ParsedName, show: Optional[Show]) -> Optional[EpisodeRef]:
    """Map a parsed name to an episode (TheTVDB when available). None if it has no number."""
    if not parsed.recognized:
        return None
    if show is not None:
        ep = None
        if parsed.season is not None and parsed.episode is not None:
            ep = show.by_season(parsed.season, parsed.episode)
        if ep is None and parsed.absolute is not None:
            ep = show.by_absolute(parsed.absolute)
        if ep is not None:
            return EpisodeRef(f"S{ep.season}E{ep.episode}", ep.season, ep.episode, ep.absolute, ep)
    if parsed.season is not None and parsed.episode is not None:
        return EpisodeRef(f"S{parsed.season}E{parsed.episode}", parsed.season, parsed.episode, parsed.absolute)
    if parsed.absolute is not None:
        return EpisodeRef(f"A{parsed.absolute}", None, None, parsed.absolute)
    return None


@dataclass
class Release:
    """One downloadable thing: a video, or every volume of one archive."""
    archive_key: str
    name: str                                         # file name of the first volume
    parsed: ParsedName
    volumes: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    ref: Optional[EpisodeRef] = None
    keep: bool = True
    in_library: bool = False

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [m for vol in sorted(self.volumes) for m in self.volumes[vol]]

    @property
    def msg_ids(self) -> list[int]:
        return [m["msg_id"] for m in self.messages]

    @property
    def last_msg_id(self) -> int:
        return max(self.msg_ids)

    @property
    def complete(self) -> bool:
        return sorted(self.volumes) == list(range(1, max(self.volumes) + 1))

    @property
    def reuploaded(self) -> bool:
        return any(len(v) > 1 for v in self.volumes.values())

    @property
    def size(self) -> int:
        # A re-uploaded volume counts once.
        return sum(max(m["size"] for m in v) for v in self.volumes.values())

    def to_dict(self) -> dict[str, Any]:
        ref = self.ref
        return {
            "name": self.name, "key": ref.key if ref else None, "label": ref.label if ref else "",
            "season": ref.season if ref else None, "episode": ref.episode if ref else None,
            "absolute": ref.absolute if ref else None,
            "tvdb_title": ref.tvdb.title if ref and ref.tvdb else "",
            "title": self.parsed.title, "story_part": self.parsed.story_part, "version": self.parsed.version,
            "crc": self.parsed.crc, "rule": self.parsed.rule, "msg_ids": self.msg_ids,
            "volumes": sorted(self.volumes), "complete": self.complete, "reuploaded": self.reuploaded,
            "size": self.size, "keep": self.keep, "in_library": self.in_library,
            "states": sorted({m["state"] for m in self.messages}),
        }


def build_releases(messages: Iterable[dict[str, Any]], numbering: str, show: Optional[Show]
                   ) -> tuple[list[Release], list[dict[str, Any]]]:
    """Group messages into releases. Returns (releases, files that are neither video nor archive)."""
    groups: dict[str, Release] = {}
    others: list[dict[str, Any]] = []
    for m in sorted(messages, key=lambda r: r["msg_id"]):
        if m.get("kind") == "photo":
            continue
        parsed = parse(m["file_name"] or "", numbering)
        if not (parsed.is_archive or parsed.is_video):
            others.append(m)
            continue
        key = parsed.archive_key if parsed.is_archive else f"{parsed.archive_key}#{m['msg_id']}"
        rel = groups.get(key)
        if rel is None:
            rel = groups[key] = Release(archive_key=key, name=m["file_name"], parsed=parsed,
                                        ref=resolve(parsed, show))
        if parsed.volume == 1:
            rel.name, rel.parsed = m["file_name"], parsed
        rel.volumes.setdefault(parsed.volume, []).append(m)
    return list(groups.values()), others


def _ranges(items: list[Any], present: set[str], key: Any) -> list[list[Any]]:
    """Runs of consecutive missing items: [[first, last, count], …]."""
    out: list[list[Any]] = []
    run: list[Any] = []
    for item in items:
        if key(item) in present:
            if run:
                out.append([run[0], run[-1], len(run)])
                run = []
        else:
            run.append(item)
    if run:
        out.append([run[0], run[-1], len(run)])
    return out


#: States of a file that is already downloaded or on its way.
_HAVE = frozenset({"queued", "downloading", "downloaded", "extracting", "extracted", "in_library"})


def best_release(rels: list[Release]) -> Release:
    """Of several copies of one episode, the one to download: complete, highest version, newest."""
    return max(rels, key=lambda r: (r.complete, r.parsed.version, r.last_msg_id))


def wanted(messages: Iterable[dict[str, Any]], *, numbering: str, show: Optional[Show] = None,
           library: Optional[dict[str, str]] = None, kind: str = "series") -> dict[str, Any]:
    """What «download what is missing» queues: the best complete copy of each episode not in the library.

    Unrecognised and incomplete archives are left out (they are listed so the user can pick them by hand).
    Movies and other content: every file that is not a photo.
    """
    messages = list(messages)
    if kind != "series":
        ids = [m["msg_id"] for m in messages if m.get("kind") != "photo"]
        return {"msg_ids": ids, "skipped_unrecognized": 0, "skipped_incomplete": 0, "in_library": 0}
    library = library or {}
    releases, _others = build_releases(messages, numbering, show)
    by_key: dict[str, list[Release]] = {}
    unrecognized = 0
    for rel in releases:
        if rel.ref is None:
            unrecognized += 1
        else:
            by_key.setdefault(rel.ref.key, []).append(rel)
    ids: list[int] = []
    incomplete = in_library = 0
    for key, rels in by_key.items():
        if key in library:
            in_library += 1
            continue
        best = best_release(rels)
        if not best.complete:
            incomplete += 1
            continue
        for copies in best.volumes.values():
            # A volume uploaded twice is downloaded once: the newest copy, unless one is already on its way.
            if any(m.get("state") in _HAVE for m in copies):
                continue
            ids.append(max(copies, key=lambda m: m["msg_id"])["msg_id"])
    return {"msg_ids": sorted(ids), "skipped_unrecognized": unrecognized, "skipped_incomplete": incomplete,
            "in_library": in_library}


def analyze(messages: Iterable[dict[str, Any]], *, numbering: str = ABSOLUTE, show: Optional[Show] = None,
            library: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """The check report of a source (see module docstring)."""
    library = library or {}
    releases, others = build_releases(messages, numbering, show)

    by_key: dict[str, list[Release]] = {}
    unrecognized: list[dict[str, Any]] = []
    not_in_tvdb: list[Release] = []
    for rel in releases:
        if rel.ref is None:
            unrecognized.append({"name": rel.name, "msg_ids": rel.msg_ids})
            continue
        if show is not None and rel.ref.tvdb is None:
            not_in_tvdb.append(rel)
        rel.in_library = rel.ref.key in library
        by_key.setdefault(rel.ref.key, []).append(rel)

    duplicates = []
    for key, rels in by_key.items():
        best = best_release(rels)
        for r in rels:
            r.keep = r is best
        if len(rels) > 1 or best.reuploaded:
            duplicates.append({"key": key, "label": best.ref.label if best.ref else key,
                               "keep": best.name, "names": [r.name for r in rels],
                               "reuploaded": [r.name for r in rels if r.reuploaded]})

    have = set(by_key) | set(library)
    missing: list[dict[str, Any]] = []
    beyond = 0
    if show is not None:
        regular = [e for e in show.regular() if e.aired() or f"S{e.season}E{e.episode}" in have]
        if numbering == ABSOLUTE:
            regular.sort(key=lambda e: (e.absolute or 10**6, e.season, e.episode))
        keys = [f"S{e.season}E{e.episode}" for e in regular]
        last = max((i for i, k in enumerate(keys) if k in have), default=-1)
        beyond = len(regular) - last - 1
        for first, final, count in _ranges(regular[:last + 1], have, lambda e: f"S{e.season}E{e.episode}"):
            missing.append({"from": episode_label(first.season, first.episode, first.absolute),
                            "to": episode_label(final.season, final.episode, final.absolute), "count": count})
    else:
        absolutes = sorted({r.ref.absolute for rels in by_key.values() for r in rels
                            if r.ref and r.ref.absolute is not None and r.ref.season is None})
        if absolutes:
            numbers = list(range(1, absolutes[-1] + 1))
            for first, final, count in _ranges(numbers, have, lambda n: f"A{n}"):
                missing.append({"from": str(first), "to": str(final), "count": count})

    incomplete = [r for r in releases if not r.complete]
    ordered = sorted(releases, key=lambda r: (r.ref is None, r.ref.season if r.ref and r.ref.season is not None
                                              else 10**6, r.ref.episode if r.ref and r.ref.episode is not None
                                              else 0, r.ref.absolute if r.ref and r.ref.absolute else 0,
                                              r.last_msg_id))
    return {
        "numbering": numbering,
        "tvdb": ({"id": show.tvdb_id, "title": show.title, "stale": show.stale, "episodes": len(show.regular())}
                 if show is not None else None),
        "summary": {
            "files": sum(len(r.messages) for r in releases) + len(others),
            "releases": len(releases),
            "episodes": len(by_key),
            "unrecognized": len(unrecognized),
            "duplicates": len(duplicates),
            "missing": sum(m["count"] for m in missing),
            "beyond": beyond,
            "not_in_tvdb": len(not_in_tvdb),
            "incomplete": len(incomplete),
            "in_library": sum(1 for k in by_key if k in library),
            "library_only": len(set(library) - set(by_key)),
            "other_files": len(others),
        },
        "releases": [r.to_dict() for r in ordered][:5000],
        "missing": missing[:LIST_LIMIT],
        "duplicates": duplicates[:LIST_LIMIT],
        "unrecognized": unrecognized[:LIST_LIMIT],
        "not_in_tvdb": [r.to_dict() for r in not_in_tvdb][:LIST_LIMIT],
        "incomplete": [r.to_dict() for r in incomplete][:LIST_LIMIT],
        "other_files": [{"name": m["file_name"], "msg_id": m["msg_id"]} for m in others][:LIST_LIMIT],
    }


# --------------------------------------------------------------------------- library

_TVDB_TAG = re.compile(r"[\[{]tvdb(?:id)?-(\d+)[\]}]", re.I)


def find_series_dir(library_root: str | Path, tvdb_id: int) -> Optional[Path]:
    """The folder of the series in the library, by its ``[tvdbid-N]`` tag (Jellyfin) or ``{tvdb-N}`` (Plex)."""
    root = Path(library_root)
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return None
    for entry in entries:
        if entry.is_dir():
            m = _TVDB_TAG.search(entry.name)
            if m and int(m[1]) == tvdb_id:
                return Path(entry.path)
    return None


def resolve_series_dir(settings: Any, options: Any) -> tuple[Optional[Path], Optional[str], bool]:
    """The series folder of a source in the library: ``(folder, problem, chosen by hand)``.

    The folder chosen in the source options wins; otherwise the one tagged ``[tvdbid-N]`` inside
    the TV library. ``problem`` (Catalan) says why there is none.
    """
    from . import storage

    if not getattr(settings, "library_tv_dir", "") and not options.library_folder:
        return None, "No hi ha cap carpeta de la biblioteca de sèries a la configuració.", False
    problem = storage.storage_problem(settings)
    if problem:
        return None, problem, bool(options.library_folder)
    if options.library_folder:
        folder = Path(options.library_folder)
        if not folder.is_dir():
            return None, f"No existeix la carpeta de la sèrie {folder}.", True
        return folder, None, True
    found = find_series_dir(settings.library_tv_dir, options.tvdb_id) if options.tvdb_id else None
    if found is None:
        return None, (f"No hi ha cap carpeta amb «[tvdbid-{options.tvdb_id}]» a {settings.library_tv_dir}: "
                      "tria-la a «Opcions de la font»."), False
    return found, None, False


def season_dir(series_dir: Path, season: int) -> Path:
    """The folder of a season: the existing one (``Season 1``, ``Season 01``, ``Temporada 1``…) or ``Season N``."""
    try:
        for entry in os.scandir(series_dir):
            m = _SEASON_DIR.match(entry.name)
            if m and entry.is_dir() and int(m[1]) == season:
                return Path(entry.path)
    except OSError:
        pass
    return series_dir / f"Season {season}"


_SEASON_DIR = re.compile(r"^(?:Season|Temporada|Saison|Staffel|Stagione|Temp\.?|S)[ ._-]*(\d{1,3})$", re.I)


def library_ref(name: str, season_dir: Optional[int], numbering: str, show: Optional[Show]) -> Optional[EpisodeRef]:
    """The episode of a video already in the library.

    ``S17E11`` wins; otherwise, in a ``Season 17`` folder, a lone number is the
    episode of that season (``11 - Títol.mkv``) or, with absolute numbering,
    the absolute number (``501. Títol.mkv``, ``Detectiu Conan 501.mkv``).
    """
    parsed = parse(name, "seasons")
    if parsed.season is not None and parsed.episode is not None:
        return resolve(parsed, show)
    if numbering == ABSOLUTE:
        absolute = parse(name, ABSOLUTE)
        if absolute.absolute is not None:
            ref = resolve(absolute, show)
            # "11.mkv" inside "Season 17" is S17E11, not the absolute episode 11.
            if (season_dir is not None and show is not None and ref is not None and ref.season != season_dir
                    and show.by_season(season_dir, absolute.absolute) is not None):
                return resolve(ParsedName(stem=parsed.stem, ext=parsed.ext, season=season_dir,
                                          episode=absolute.absolute), show)
            return ref
    if season_dir is not None and parsed.episode is not None:
        return resolve(ParsedName(stem=parsed.stem, ext=parsed.ext, season=season_dir, episode=parsed.episode),
                       show)
    return None


def scan_library(series_dir: str | Path, show: Optional[Show], numbering: str = "seasons") -> dict[str, str]:
    """Episode key -> path of every video in the series folder (season folders included)."""
    found: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(series_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        m = _SEASON_DIR.match(os.path.basename(dirpath))
        season_dir = int(m[1]) if m else None
        for name in files:
            if name.startswith(".") or name.rsplit(".", 1)[-1].lower() not in VIDEO_EXTS:
                continue
            ref = library_ref(name, season_dir, numbering, show)
            if ref is not None:
                found.setdefault(ref.key, os.path.join(dirpath, name))
    return found


# --------------------------------------------------------------------------- tdl

@dataclass
class TdlFile:
    chat: int
    msg_id: int
    name: str
    size: int
    path: str


def scan_tdl_dir(directory: str | Path) -> list[TdlFile]:
    """Files named ``<chat>_<msg>_<original name>`` (finished ones: tdl writes ``.tmp`` while downloading)."""
    out = []
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise FileNotFoundError(f"No es pot llegir la carpeta {directory}: {exc.strerror or exc}") from exc
    for entry in entries:
        if not entry.is_file() or entry.name.endswith(".tmp"):
            continue
        split = split_tdl_name(entry.name)
        if split is None:
            continue
        chat, msg_id, name = split
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        out.append(TdlFile(chat, msg_id, name, size, entry.path))
    return sorted(out, key=lambda f: (f.chat, f.msg_id))


def tdl_chat_matches(tdl_chat: int, chat_id: int) -> bool:
    """tdl names channels by their bare id (1495790952 for -1001495790952)."""
    text = str(chat_id)
    bare = int(text[4:]) if text.startswith("-100") else abs(chat_id)
    return tdl_chat in (bare, abs(chat_id))
