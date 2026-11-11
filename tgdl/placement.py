"""Phase 4: extract downloaded episodes and place them in the library, the way Jellyfin reads it.

For every downloaded *release* (all the volumes of one archive, or a plain video)
of a series source:

1. the volumes get their original names again (symbolic links in a work folder:
   ``tdl`` prefixes every file with ``<chat>_<message>_``, and 7-Zip only finds
   ``.part2.rar`` next to ``.part1.rar`` when the names match);
2. ``7zz`` extracts them into the temporary folder (on the NAS, so the move is
   a rename);
3. the largest video becomes ``<series>/Season N/<Show> - S25E83 - 0886.mp4``
   (absolute number only with absolute numbering), next to an ``.nfo``;
4. the messages become ``in_library`` and, if everything went well and the
   setting allows it, the archives are deleted.

Nothing is overwritten: an episode that is already in the library with another
file is reported, not replaced. The ``.nfo`` title is the one in the file name
(``DC-1035 (Escac i mat)``) or TheTVDB's; it carries ``<!-- tgdl -->`` so the
Catalan titles of phase 6 can replace it.

Releases asked for by Sonarr/Radarr (:mod:`tgdl.sabnzbd`) are *delivered*
instead: extracted into ``<download dir>/<category>/<release>/`` for them to
import, and the library placement leaves them alone.

One release at a time, in a worker thread (7-Zip and NFS are the bottleneck).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from xml.sax.saxutils import escape

from . import review, storage
from .config import atomic_write_bytes
from .db import DOWNLOADED, EXTRACTED, EXTRACTING, Database
from .downloads import safe_name
from .naming import ABSOLUTE, VIDEO_EXTS

log = logging.getLogger(__name__)

#: Seconds 7-Zip may take for one archive.
EXTRACT_TIMEOUT = 6 * 3600
#: A second video at least this fraction of the largest one means the archive is not one episode.
SECOND_VIDEO_RATIO = 0.3
NFO_MARK = "<!-- tgdl -->"
WORK_DIR = "extract"


class PlacementError(Exception):
    """This release cannot be placed (Catalan message); its files stay downloaded."""


# --------------------------------------------------------------------------- names and .nfo

def show_name(series_dir: Path) -> str:
    """``Detectiu Conan (1996) [tvdbid-72454]`` → ``Detectiu Conan``."""
    import re

    name = re.sub(r"\s*[\[{][^\]}]*[\]}]", "", series_dir.name)
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip()
    return name or series_dir.name


def episode_basename(show: str, ref: review.EpisodeRef, numbering: str) -> str:
    """``Detectiu Conan - S25E83 - 0886`` (the format already in the library)."""
    if ref.season is None or ref.episode is None:
        raise PlacementError(f"No se sap la temporada de l'episodi {ref.label}: posa l'id de TheTVDB a la font.")
    base = f"{safe_name(show, 0)} - S{ref.season:02d}E{ref.episode:02d}"
    if numbering == ABSOLUTE and ref.absolute is not None:
        base += f" - {ref.absolute:04d}"
    return base


def episode_title(release: review.Release) -> str:
    """The title for the .nfo: the one in the file name (Catalan), else TheTVDB's; with the number."""
    title = release.parsed.title.strip()
    part = release.parsed.story_part.strip().strip("()")
    if title or release.ref is None or release.ref.tvdb is None:
        if part:                       # «DC-887 (I)»: half of a two-part story
            title = f"{title} ({part})".strip()
    else:
        title = release.ref.tvdb.title   # TheTVDB's already says «(Part One)»
    ref = release.ref
    number = ref.absolute if ref is not None and ref.absolute is not None else None
    if number is not None:
        if not title or title.startswith("("):
            return f"Episodi {number} {title}".strip()
        return f"{number}. {title}"
    return title or (ref.label if ref is not None else release.name)


def nfo_xml(show: str, release: review.Release) -> str:
    ref = release.ref
    assert ref is not None
    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>', "<episodedetails>",
             f"  <title>{escape(episode_title(release))}</title>",
             f"  <showtitle>{escape(show)}</showtitle>",
             f"  <season>{ref.season}</season>",
             f"  <episode>{ref.episode}</episode>"]
    if ref.tvdb is not None and ref.tvdb.air_date:
        lines.append(f"  <aired>{escape(ref.tvdb.air_date)}</aired>")
    if ref.absolute is not None:
        lines.append(f'  <uniqueid type="absolute">{ref.absolute}</uniqueid>')
    lines += [f"  {NFO_MARK}", "</episodedetails>", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- extraction

def link_volumes(volumes: list[tuple[str, Path]], folder: Path) -> list[Path]:
    """Give every volume its original name in ``folder`` (symlink, else hard link, else copy)."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, source in volumes:
        target = folder / safe_name(name, 0)
        if target.exists() or target.is_symlink():
            target.unlink()
        try:
            os.symlink(source, target)
        except (OSError, NotImplementedError):
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
        out.append(target)
    return out


def extract(first_volume: Path, out_dir: Path, seven_zip: Optional[str]) -> None:
    """Extract an archive (every volume next to ``first_volume``) into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if seven_zip:
        cmd = [seven_zip, "x", "-y", "-bd", "-bso0", "-bsp0", "-p-", f"-o{out_dir}", str(first_volume)]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="replace", timeout=EXTRACT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise PlacementError("7-Zip no ha acabat a temps.") from None
        if proc.returncode not in (0, 1):     # 1 = warnings
            detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
            if "Wrong password" in detail or "encrypted" in detail.lower():
                raise PlacementError("L'arxiu té contrasenya.")
            raise PlacementError(f"7-Zip no ha pogut descomprimir l'arxiu ({proc.returncode}): {detail}")
        return
    if zipfile.is_zipfile(first_volume):          # without 7-Zip (tests, Windows without it): plain ZIP only
        root = out_dir.resolve()
        with zipfile.ZipFile(first_volume) as zf:
            for member in zf.infolist():
                target = (out_dir / member.filename).resolve()
                if not storage.is_within(target, root):
                    raise PlacementError(f"L'arxiu té un camí perillós: {member.filename}")
            zf.extractall(out_dir)
        return
    raise PlacementError("No s'ha trobat 7-Zip (7zz): no es pot descomprimir.")


def pick_video(folder: Path) -> Path:
    """The episode inside an extracted archive: its largest video (a sample next to it is fine)."""
    videos = sorted((p for p in folder.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in VIDEO_EXTS),
                    key=lambda p: p.stat().st_size, reverse=True)
    if not videos:
        raise PlacementError("L'arxiu no conté cap vídeo.")
    if len(videos) > 1 and videos[1].stat().st_size >= SECOND_VIDEO_RATIO * videos[0].stat().st_size:
        raise PlacementError(f"L'arxiu conté {len(videos)} vídeos: no se sap quin és l'episodi.")
    return videos[0]


def move(source: Path, target: Path) -> None:
    """Rename when possible (same disk); otherwise copy to a temporary name and rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
        return
    except OSError:
        pass
    partial = target.with_name(f".{target.name}.tgdl-part")
    shutil.copyfile(source, partial)
    os.replace(partial, target)
    source.unlink()


# --------------------------------------------------------------------------- the worker

@dataclass
class Run:
    source_id: int
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str = ""
    started: float = field(default_factory=time.time)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"total": self.total, "done": self.done, "failed": self.failed, "current": self.current,
                "started": self.started, "errors": self.errors[-20:]}


@dataclass
class Ready:
    """A release whose files are all downloaded."""
    release: review.Release
    volumes: list[dict[str, Any]]           # one message per volume (the downloaded copy), in order


def ready_releases(messages: Iterable[dict[str, Any]], numbering: str, show: Any) -> list[Ready]:
    """Downloaded releases that can be placed (recognised; every volume downloaded and on disk)."""
    releases, _others = review.build_releases(messages, numbering, show)
    out = []
    for rel in releases:
        if rel.ref is None or not rel.complete:
            continue
        chosen = []
        for number in sorted(rel.volumes):
            copies = [m for m in rel.volumes[number] if m["state"] == DOWNLOADED and m.get("local_path")]
            if not copies:
                break
            chosen.append(max(copies, key=lambda m: m["msg_id"]))
        else:
            out.append(Ready(rel, chosen))
    out.sort(key=lambda r: (r.release.ref.season or 0, r.release.ref.episode or 0,   # type: ignore[union-attr]
                            r.release.ref.absolute or 0))                           # type: ignore[union-attr]
    return out


class Placer:
    def __init__(self, settings_getter: Callable[[], Any], db: Database, *,
                 show_getter: Callable[[Any], Any], publish: Callable[[str, Any], None],
                 source_payload: Optional[Callable[[int], Any]] = None,
                 seven_zip: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._settings = settings_getter
        self.db = db
        self._show = show_getter
        self._publish = publish
        self._payload = source_payload
        self._seven_zip = seven_zip or _find_seven_zip
        self._lock = threading.Lock()
        self._runs: dict[int, Run] = {}
        self._last: dict[int, Run] = {}                   # source id -> last finished run
        self._pending: dict[int, Optional[int]] = {}      # source id -> limit
        self._deliveries: list[str] = []                  # Sonarr/Radarr jobs (nzo ids) to extract
        self._delivering = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ control (any thread)
    def status(self, source_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            run = self._runs.get(source_id)
            return run.to_dict() if run else None

    def last(self, source_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            run = self._last.get(source_id)
            return run.to_dict() if run else None

    def busy(self, source_id: Optional[int] = None) -> bool:
        with self._lock:
            if source_id is None:
                return bool(self._runs or self._pending or self._deliveries or self._delivering)
            return source_id in self._runs or source_id in self._pending

    def deliver(self, nzo_id: str) -> None:
        """Extract the release of this Sonarr/Radarr job for them to import, in the background."""
        with self._lock:
            if nzo_id not in self._deliveries:
                self._deliveries.append(nzo_id)
            self._ensure_thread()

    def _ensure_thread(self) -> None:   # with the lock held
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, name="placement", daemon=True)
            self._thread.start()

    def place(self, source_id: int, limit: Optional[int] = None) -> bool:
        """Place what is ready in this source, in the background (``limit``: at most that many episodes).

        False if it was already running (it will look again when it finishes).
        """
        with self._lock:
            already = source_id in self._runs or source_id in self._pending
            self._pending[source_id] = limit
            self._ensure_thread()
        return not already

    def wait(self, timeout: float = 30.0) -> bool:
        """Tests: wait until nothing is pending."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.busy():
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------ worker thread
    def _work(self) -> None:
        while True:
            with self._lock:
                nzo_id = self._deliveries.pop(0) if self._deliveries else None
                if nzo_id is not None:
                    self._delivering = True
            if nzo_id is not None:
                try:
                    self._deliver(nzo_id)
                except Exception:
                    log.exception("Error preparant el llançament %s per a Sonarr/Radarr", nzo_id)
                finally:
                    with self._lock:
                        self._delivering = False
                continue
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return
                source_id, limit = next(iter(self._pending.items()))
                del self._pending[source_id]
                self._runs[source_id] = Run(source_id)
            try:
                self._place_source(source_id, limit)
            except Exception:
                log.exception("Error col·locant els fitxers de la font %s", source_id)
            finally:
                with self._lock:
                    run = self._runs.pop(source_id, None)
                    if run is not None and run.total:
                        self._last[source_id] = run
                self._publish_source(source_id)
                if run is not None and run.total:
                    log.info("Biblioteca: %d episodis col·locats, %d amb errors (font %s)",
                             run.done, run.failed, source_id)

    def _deliver(self, nzo_id: str) -> None:
        job = self.db.arr_job(nzo_id)
        if job is None or job["status"] != "active":
            return
        try:
            target = deliver_release(self.db, job, self._settings(), self._seven_zip())
        except storage.StorageUnavailable as exc:
            log.warning("Sonarr/Radarr: «%s» espera el disc: %s", job["title"], exc)
            return                          # stays active: tried again after the next download or restart
        except PlacementError as exc:
            self.db.set_arr_job(nzo_id, status="failed", error=str(exc), finished_at=time.time())
            log.warning("Sonarr/Radarr: «%s» no s'ha pogut preparar: %s", job["title"], exc)
        else:
            if target is not None:
                self.db.set_arr_job(nzo_id, status="completed", finished_at=time.time())
                log.info("Sonarr/Radarr: «%s» a punt per importar (%s)", job["title"], target.parent)
        self._publish_source(job["source_id"])

    def _publish_source(self, source_id: int) -> None:
        if self._payload is None:
            return
        try:
            payload = self._payload(source_id)
        except Exception:   # pragma: no cover
            return
        if payload is not None:
            self._publish("source", payload)

    def _place_source(self, source_id: int, limit: Optional[int]) -> None:
        src = self.db.source(source_id)
        if src is None or src.options.kind != "series":
            return
        settings = self._settings()
        show = self._show(src)
        series_dir, problem, _chosen = review.resolve_series_dir(settings, src.options)
        claimed = {mid for sid, mid in self.db.arr_claimed() if sid == source_id}
        messages = [m for m in self.db.all_messages(source_id) if m["msg_id"] not in claimed]
        ready = ready_releases(messages, src.options.numbering, show)
        if limit is not None:
            ready = ready[:max(0, limit)]
        run = self._runs[source_id]
        run.total = len(ready)
        if not ready:
            return
        if series_dir is None:
            run.failed = len(ready)
            run.errors.append({"name": src.title, "error": problem})
            log.warning("No es pot col·locar res de «%s»: %s", src.title, problem)
            return
        self._publish_source(source_id)
        for item in ready:
            run.current = item.release.name
            self._publish_source(source_id)
            try:
                self.place_release(src, item, series_dir, settings)
                run.done += 1
            except PlacementError as exc:
                run.failed += 1
                run.errors.append({"name": item.release.name, "error": str(exc)})
                log.warning("No s'ha pogut col·locar «%s»: %s", item.release.name, exc)
            except storage.StorageUnavailable as exc:
                run.errors.append({"name": item.release.name, "error": str(exc)})
                log.warning("Col·locació aturada: %s", exc)
                return
            run.current = ""

    def place_release(self, src: Any, item: Ready, series_dir: Path, settings: Any) -> Path:
        """Extract and place one release. Raises :class:`PlacementError` (files stay downloaded)."""
        rel, ref = item.release, item.release.ref
        assert ref is not None
        ids = [m["msg_id"] for m in item.volumes]
        storage.require_storage(settings)
        show = show_name(series_dir)
        base = episode_basename(show, ref, src.options.numbering)
        season = review.season_dir(series_dir, int(ref.season))       # type: ignore[arg-type]
        work = Path(settings.effective_temp_dir) / WORK_DIR / f"src-{src.id}-{ids[0]}"
        self.db.set_state(src.id, ids, EXTRACTING, only_from=(DOWNLOADED,))
        try:
            paths = [Path(m["local_path"]) for m in item.volumes]
            missing = [p for p in paths if not p.is_file()]
            if missing:
                raise PlacementError(f"Falta el fitxer {missing[0]} (s'ha mogut o esborrat?).")
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            if rel.parsed.is_video:
                video = paths[0]
                in_place = True
            else:
                need = sum(p.stat().st_size for p in paths)
                free = storage.free_bytes(str(Path(settings.effective_temp_dir)))
                if free is not None and free < need + 512 * 1024 ** 2:
                    raise PlacementError("No hi ha prou espai al disc per descomprimir.")
                links = link_volumes([(m["file_name"], p) for m, p in zip(item.volumes, paths)], work / "vols")
                extract(links[0], work / "out", self._seven_zip())
                video = pick_video(work / "out")
                in_place = False
            target = season / f"{base}{video.suffix.lower()}"
            storage.require_storage(settings)
            if target.exists():
                if target.stat().st_size != video.stat().st_size:
                    raise PlacementError(f"Ja hi ha un fitxer diferent a la biblioteca: {target.name}")
                log.info("«%s» ja era a la biblioteca (%s)", rel.name, target.name)
            else:
                move(video, target)
            nfo = target.with_suffix(".nfo")
            if not nfo.exists() or NFO_MARK in nfo.read_text(encoding="utf-8", errors="replace"):
                atomic_write_bytes(nfo, nfo_xml(show, rel).encode("utf-8"))
                _readable(nfo)
            self.db.mark_in_library(src.id, rel.msg_ids, str(target))
            log.info("A la biblioteca: %s/%s", season.name, target.name)
            if getattr(settings, "delete_archives", True) and not in_place:
                for p in paths:
                    try:
                        p.unlink()
                    except OSError as exc:
                        log.warning("No s'ha pogut esborrar %s: %s", p, exc)
            return target
        except (PlacementError, storage.StorageUnavailable) as exc:
            self.db.set_state(src.id, ids, DOWNLOADED, error=str(exc), only_from=(EXTRACTING,))
            raise
        except OSError as exc:
            self.db.set_state(src.id, ids, DOWNLOADED, error=f"Error de disc: {exc}", only_from=(EXTRACTING,))
            raise PlacementError(f"Error de disc: {exc}") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)


def deliver_release(db: Database, job: dict[str, Any], settings: Any, seven_zip: Optional[str]) -> Optional[Path]:
    """Extract a Sonarr/Radarr release into its job folder; None if its files are not all downloaded yet.

    The video is named after the release (what Sonarr/Radarr parse). Raises :class:`PlacementError`.
    """
    msgs = [db.message(job["source_id"], mid) for mid in job["msg_ids"]]
    if any(m is None or m["deleted"] for m in msgs):
        raise PlacementError("Algun fitxer d'aquest llançament ja no és a Telegram.")
    found = [m for m in msgs if m is not None]
    if not all(m["state"] in (DOWNLOADED, EXTRACTING) for m in found):
        return None
    ids = [m["msg_id"] for m in found]
    storage.require_storage(settings)
    folder = Path(job["folder"])
    work = Path(settings.effective_temp_dir) / WORK_DIR / f"arr-{job['nzo_id'][-12:]}"
    db.set_state(job["source_id"], ids, EXTRACTING, only_from=(DOWNLOADED,))
    try:
        paths = [Path(m["local_path"] or "") for m in found]
        missing = [p for p in paths if not p.is_file()]
        if missing:
            raise PlacementError(f"Falta el fitxer {missing[0]} (s'ha mogut o esborrat?).")
        shutil.rmtree(work, ignore_errors=True)
        first_ext = Path(found[0]["file_name"]).suffix.lower().lstrip(".")
        if len(paths) == 1 and first_ext in VIDEO_EXTS:
            video, in_place = paths[0], True
        else:
            links = link_volumes([(m["file_name"], p) for m, p in zip(found, paths)], work / "vols")
            extract(links[0], work / "out", seven_zip)
            video, in_place = pick_video(work / "out"), False
        storage.require_storage(settings)
        target = folder / f"{safe_name(job['title'], 0)}{video.suffix.lower()}"
        move(video, target)
        _readable(target)
        db.set_state(job["source_id"], ids, EXTRACTED, only_from=(EXTRACTING,))
        if getattr(settings, "delete_archives", True) and not in_place:
            for p in paths:
                try:
                    p.unlink()
                except OSError as exc:
                    log.warning("No s'ha pogut esborrar %s: %s", p, exc)
        return target
    except (PlacementError, storage.StorageUnavailable):
        db.set_state(job["source_id"], ids, DOWNLOADED, only_from=(EXTRACTING,))
        raise
    except OSError as exc:
        db.set_state(job["source_id"], ids, DOWNLOADED, only_from=(EXTRACTING,))
        raise PlacementError(f"Error de disc: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _readable(path: Path) -> None:
    """Like the videos: readable by everyone (Jellyfin may run as another user). Honours the umask."""
    mask = os.umask(0)
    os.umask(mask)
    try:
        os.chmod(path, 0o666 & ~mask)
    except OSError:
        pass


def _find_seven_zip() -> Optional[str]:
    from .runtime import find_7zz

    return find_7zz()[0]

