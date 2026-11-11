"""Sonarr/Radarr, part 2: a SABnzbd-compatible download client.

Sonarr sends the pseudo-NZB of a release (:mod:`tgdl.arr`) to ``/sabnzbd/api``
(``mode=addfile``). That creates a **job** (``nzo_id``, table ``arr_jobs``):

1. its messages go to the download queue (also ones that were already placed
   in the library and whose archives are gone: Sonarr asked for them);
2. once every volume is downloaded, :class:`tgdl.placement.Placer` extracts the
   video into ``<download dir>/<category>/<release>/<release>.mp4``;
3. Sonarr sees it as ``Completed`` in the history, imports it, and asks to
   delete it (``del_files=1``): the folder goes and the messages become
   ``in_library``.

The queue/history answers have the fields Sonarr 4 and Radarr 6 read (the same
as 3Cat Downloader's).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .arr import Token
from .db import (DOWNLOADED, DOWNLOADING, ERROR, EXTRACTED, EXTRACTING, IN_LIBRARY, NEW, QUEUED, Database)
from .downloads import safe_name

log = logging.getLogger(__name__)

SAB_VERSION = "4.5.0"
CATEGORIES = ("tv", "movies", "sonarr", "radarr")


class SabError(Exception):
    """Reported as ``{"status": false, "error": ...}``."""


def _timeleft(seconds: Optional[float]) -> str:
    s = max(0, int(seconds or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _folder_size(folder: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _inside(path: Path, root: Path) -> bool:
    try:
        rel = path.resolve().relative_to(root.resolve())
        return str(rel) not in ("", ".")
    except (ValueError, OSError):
        return False


class SabnzbdService:
    def __init__(self, db: Database, settings_getter: Callable[[], Any], *, downloads: Any, placer: Any) -> None:
        self.db = db
        self._settings = settings_getter
        self._downloads = downloads
        self._placer = placer

    # ------------------------------------------------------------------ config
    def complete_dir(self) -> str:
        return str(self._settings().download_dir)

    def config(self) -> dict[str, Any]:
        categories = [{"name": "*", "order": 0, "pp": "3", "script": "None", "dir": "", "priority": 0}]
        for i, name in enumerate(CATEGORIES, start=1):
            categories.append({"name": name, "order": i, "pp": "", "script": "Default", "dir": name,
                               "priority": -100})
        return {"config": {
            "misc": {"complete_dir": self.complete_dir(), "download_dir": self.complete_dir(),
                     "tv_categories": [], "enable_tv_sorting": False, "movie_categories": [],
                     "enable_movie_sorting": False, "date_categories": [], "enable_date_sorting": False,
                     "pre_check": False, "history_retention": "", "history_retention_option": "all",
                     "history_retention_number": 0},
            "categories": categories, "servers": [], "sorters": []}}

    # ------------------------------------------------------------------ add
    def add(self, token: Token, category: Optional[str]) -> str:
        src = self.db.source(token.source_id)
        if src is None:
            raise SabError("Aquesta font ja no existeix a Telegram Downloader.")
        msgs = [self.db.message(token.source_id, mid) for mid in token.msg_ids]
        if any(m is None or m["deleted"] for m in msgs):
            raise SabError("Algun fitxer d'aquest llançament ja no és a Telegram.")
        category = (category or token.category or "tv").strip() or "tv"
        if category == "*":
            category = token.category or "tv"
        title = token.title.strip()
        for job in self.db.arr_jobs("active"):                    # the same release, asked again
            if job["source_id"] == token.source_id and job["msg_ids"] == token.msg_ids:
                return str(job["nzo_id"])
        folder = Path(self.complete_dir()) / (safe_name(category, 0) or "tv") / (safe_name(title, 0) or "telegram")
        nzo_id = f"SABnzbd_nzo_tgdl{uuid.uuid4().hex[:12]}"
        self.db.add_arr_job({"nzo_id": nzo_id, "title": title, "category": category, "source_id": token.source_id,
                             "msg_ids": token.msg_ids, "folder": str(folder), "created_at": time.time()})
        again = []
        for m in msgs:
            assert m is not None
            has_file = m["state"] == DOWNLOADED and m["local_path"] and os.path.isfile(m["local_path"])
            if not has_file and m["state"] not in (QUEUED, DOWNLOADING, EXTRACTING):
                again.append(m["msg_id"])
        if again:
            self.db.set_state(token.source_id, again, QUEUED, only_from=(NEW, ERROR, DOWNLOADED, IN_LIBRARY, EXTRACTED))
            self._downloads.wake()
        self._placer.deliver(nzo_id)                              # when everything is already downloaded
        log.info("Sonarr/Radarr: «%s» (%d fitxers, %d a baixar) → %s", title, len(msgs), len(again), folder)
        return nzo_id

    # ------------------------------------------------------------------ state
    def _summary(self, job: dict[str, Any]) -> dict[str, Any]:
        msgs = [m for m in (self.db.message(job["source_id"], mid) for mid in job["msg_ids"]) if m is not None]
        total = max(1, sum(int(m["size"] or 0) for m in msgs))
        if job["status"] == "completed":
            return {"status": "Completed", "total": total, "done": total, "eta": 0, "active": False, "error": None}
        if job["status"] == "failed":
            return {"status": "Failed", "total": total, "done": 0, "eta": 0, "active": False,
                    "error": job["error"] or "No s'ha pogut preparar."}
        failed = next((m for m in msgs if m["state"] == ERROR), None)
        if failed is not None or len(msgs) < len(job["msg_ids"]):
            error = (failed or {}).get("error") or "Algun fitxer ja no és a Telegram."
            return {"status": "Failed", "total": total, "done": 0, "eta": 0, "active": False,
                    "error": f"{(failed or {}).get('file_name', '')}: {error}".strip(": ")}
        done = 0
        for m in msgs:
            done += int(m["size"] or 0) if m["state"] in (DOWNLOADED, EXTRACTING, EXTRACTED) else int(m["bytes_done"] or 0)
        speed = 0.0
        try:
            speed = float(self._downloads.status().get("speed") or 0)
        except Exception:   # pragma: no cover
            pass
        states = {m["state"] for m in msgs}
        if states <= {DOWNLOADED, EXTRACTING, EXTRACTED}:
            status = "Extracting"
        elif DOWNLOADING in states or EXTRACTING in states:
            status = "Downloading"
        else:
            status = "Queued"
        eta = (total - done) / speed if speed > 0 else 0
        return {"status": status, "total": total, "done": done, "eta": eta, "active": True, "error": None}

    def queue(self, category: Optional[str] = None, start: int = 0, limit: int = 0) -> dict[str, Any]:
        slots = []
        for index, job in enumerate(self.db.arr_jobs("active")):
            if category and category != "*" and job["category"] != category:
                continue
            info = self._summary(job)
            if not info["active"]:
                continue
            mb, left = info["total"] / 1048576, max(0.0, (info["total"] - info["done"]) / 1048576)
            slots.append({"status": info["status"], "index": index, "timeleft": _timeleft(info["eta"]),
                          "mb": f"{mb:.2f}", "mbleft": f"{left:.2f}", "filename": job["title"],
                          "priority": "Normal", "cat": job["category"],
                          "percentage": str(int(100 * info["done"] / info["total"])), "nzo_id": job["nzo_id"]})
        if limit:
            slots = slots[start:start + limit]
        return {"queue": {"status": "Downloading" if slots else "Idle", "paused": False,
                          "noofslots": len(slots), "slots": slots}}

    def history(self, category: Optional[str] = None, start: int = 0, limit: int = 0) -> dict[str, Any]:
        slots = []
        for job in reversed(self.db.arr_jobs()):
            if category and category != "*" and job["category"] != category:
                continue
            info = self._summary(job)
            if info["active"]:
                continue
            finished = job["finished_at"] or time.time()
            completed = info["status"] == "Completed"
            slots.append({"nzo_id": job["nzo_id"], "name": job["title"], "nzb_name": f"{job['title']}.nzb",
                          "category": job["category"], "status": info["status"],
                          "fail_message": "" if completed else (info["error"] or "La baixada ha fallat."),
                          "storage": job["folder"] if completed else "",
                          "bytes": _folder_size(job["folder"]) if completed else 0,
                          "download_time": int(finished - job["created_at"]), "completed": int(finished)})
        if limit:
            slots = slots[start:start + limit]
        return {"history": {"paused": False, "noofslots": len(slots), "slots": slots}}

    # ------------------------------------------------------------------ actions
    def delete(self, nzo_ids: list[str], delete_files: bool) -> bool:
        removed = False
        root = Path(self.complete_dir())
        for nzo_id in nzo_ids:
            job = self.db.arr_job(nzo_id)
            if job is None:
                continue
            removed = True
            if job["status"] == "active":                         # Sonarr gave up on it
                waiting = [mid for mid in job["msg_ids"]
                           if (m := self.db.message(job["source_id"], mid)) and m["state"] in (QUEUED, DOWNLOADING)]
                if waiting:
                    self._downloads.unqueue(job["source_id"], waiting)
            elif job["status"] == "completed":                    # imported by Sonarr/Radarr
                self.db.mark_in_library(job["source_id"], job["msg_ids"], job["folder"])
            if delete_files and _inside(Path(job["folder"]), root):
                shutil.rmtree(job["folder"], ignore_errors=True)
            self.db.delete_arr_job(nzo_id)
            log.info("Sonarr/Radarr: «%s» esborrat de l'historial", job["title"])
        return removed

    def retry(self, nzo_id: str) -> Optional[str]:
        job = self.db.arr_job(nzo_id)
        if job is None:
            return None
        self.db.set_arr_job(nzo_id, status="active", error=None, finished_at=None)
        self.db.set_state(job["source_id"], job["msg_ids"], QUEUED, only_from=(ERROR, NEW))
        self._downloads.wake()
        self._placer.deliver(nzo_id)
        return nzo_id

    def resume(self) -> None:
        """At start-up: extract what finished downloading while the app was stopped."""
        for job in self.db.arr_jobs("active"):
            self._placer.deliver(job["nzo_id"])

    def claims(self, source_id: int, msg_id: int) -> list[str]:
        """Active jobs that include this message."""
        return [j["nzo_id"] for j in self.db.arr_jobs("active")
                if j["source_id"] == source_id and msg_id in j["msg_ids"]]
