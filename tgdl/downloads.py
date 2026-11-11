"""The download queue: robust, resumable, parallel downloads from Telegram.

What went wrong with ``tdl`` and how this avoids it:

* **Resumes that did not resume.** Every file is downloaded in fixed blocks
  (:data:`BLOCK`, the largest MTProto request) written at their offset in a
  ``.part`` file; a small ``.part.json`` next to it says which blocks are
  there. After a stop, a crash or a power cut only the missing blocks are
  fetched. The state in the database (``downloading`` → back to ``queued`` on
  start) never depends on what is on disk.
* **Stalled connections.** Every block request has a deadline
  (``stall_seconds``): the *watchdog*. A request that does not answer is
  abandoned and asked again (Telethon reconnects underneath), with growing
  pauses; after :data:`MAX_FAILURES` failures in a row the file goes to
  ``error`` with the reason, and the rest of the queue goes on.
* **Speed.** ``chunks_per_file`` blocks of one file are fetched at once, and
  ``max_parallel_files`` files at once. ``FLOOD_WAIT`` is waited out (it does not
  count as a failure).
* **The NAS that was not mounted.** :func:`tgdl.storage.storage_problem` is
  checked before every write: if the share disappears, the downloads go back to
  the queue (keeping what they have) and nothing is written until it is back.

Files land in ``<temp dir>/src-<source id>/<original name>`` (all the volumes
of a multipart archive together, for the extraction of phase 4) and become
``downloaded``. The manager runs on the Telegram service's event loop: the
gateway belongs to it. Progress goes to the UI (``downloads`` events), never to
the log.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import storage
from .config import atomic_write_json
from .db import DOWNLOADING, ERROR, NEW, QUEUED, Database
from .telegram.gateway import (ChatUnavailable, FileGone, FloodWait, NotAuthorized, NotConfigured, RemoteFile,
                               TelegramError)

log = logging.getLogger(__name__)

#: Bytes per request: the largest Telegram allows (upload.getFile, 512 KiB through Telethon).
BLOCK = 512 * 1024
#: Failed requests of one block in a row before the file goes to ``error``.
MAX_FAILURES = 8
#: Pause before asking again after a failure (seconds; the last value repeats).
RETRY_DELAYS = (1, 3, 10, 30, 60)
#: How often the ``.part.json`` and the progress in the database are saved (seconds).
SAVE_EVERY = 5.0
#: How often progress is pushed to the UI (seconds).
PUBLISH_EVERY = 1.0
#: The manager looks at the queue at least this often (seconds).
POLL_SECONDS = 5.0
#: Free space kept on the disk beyond what the downloads need.
FREE_MARGIN = 1024 ** 3
#: Seconds of history used for the speed.
SPEED_WINDOW = 10.0
PAUSED_KEY = "downloads_paused"


class Requeue(Exception):
    """Stop this file for now and put it back in the queue (storage gone, session closed…)."""


class DownloadFailed(Exception):
    """This file cannot be downloaded (Catalan message): it goes to ``error``."""


# --------------------------------------------------------------------------- the .part.json

@dataclass
class PartState:
    """Which blocks of a ``.part`` file are already there."""
    size: int
    block: int = BLOCK
    done: set[int] = field(default_factory=set)

    @property
    def blocks(self) -> int:
        return max(1, math.ceil(self.size / self.block)) if self.size else 0

    def block_len(self, index: int) -> int:
        return max(0, min(self.block, self.size - index * self.block))

    @property
    def bytes_done(self) -> int:
        return sum(self.block_len(i) for i in self.done)

    @property
    def complete(self) -> bool:
        return len(self.done) >= self.blocks

    def missing(self) -> list[int]:
        return [i for i in range(self.blocks) if i not in self.done]

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "size": self.size, "block": self.block, "done": _ranges(sorted(self.done))}

    @classmethod
    def load(cls, path: Path, size: int) -> Optional["PartState"]:
        """The saved state, or None if missing, unreadable or for another file size."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("size") != size or data.get("block") != BLOCK:
                return None
            state = cls(size=size)
            for first, last in data.get("done", []):
                state.done.update(range(int(first), int(last) + 1))
            state.done &= set(range(state.blocks))
            return state
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def save(self, path: Path) -> None:
        atomic_write_json(path, self.to_dict(), compact=True)


def _ranges(numbers: list[int]) -> list[list[int]]:
    """[0, 1, 2, 5, 6] -> [[0, 2], [5, 6]] (keeps the file small for big downloads)."""
    out: list[list[int]] = []
    for n in numbers:
        if out and out[-1][1] == n - 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def safe_name(name: str, msg_id: int) -> str:
    """A file name that is safe on any filesystem (Telegram names are untrusted)."""
    cleaned = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in name).strip().lstrip(".")
    cleaned = cleaned.rstrip(". ")
    if len(cleaned.encode("utf-8")) > 240:
        stem, dot, ext = cleaned.rpartition(".")
        ext = ext if dot and len(ext) <= 10 else ""
        base = (stem if ext else cleaned).encode("utf-8")[:230].decode("utf-8", "ignore")
        cleaned = f"{base}.{ext}" if ext else base
    return cleaned or f"{msg_id}.bin"


def target_path(folder: Path, file_name: str, msg_id: int) -> Path:
    """Where a finished file goes: its own name, unless another file already has it."""
    name = safe_name(file_name, msg_id)
    path = folder / name
    if not path.exists():
        return path
    stem, dot, ext = name.rpartition(".")
    return folder / (f"{stem} [{msg_id}].{ext}" if dot and stem else f"{name} [{msg_id}]")


# --------------------------------------------------------------------------- one active file

@dataclass
class Job:
    source_id: int
    msg_id: int
    chat_id: int
    file_name: str
    source_title: str
    size: int
    bytes_done: int = 0
    started: float = field(default_factory=time.time)
    #: ``starting`` | ``downloading`` | ``waiting`` (FLOOD_WAIT) | ``retrying`` (watchdog/errors) | ``finishing``
    status: str = "starting"
    note: Optional[str] = None
    connections: int = 0
    task: Optional[asyncio.Task] = None
    #: State to leave the file in when its task is cancelled (None = shutdown: stays ``downloading``).
    cancel_to: Optional[str] = None
    history: deque = field(default_factory=lambda: deque(maxlen=200))

    @property
    def key(self) -> tuple[int, int]:
        return (self.source_id, self.msg_id)

    def progress(self, now: Optional[float] = None) -> None:
        """Record the progress (service loop only; :meth:`speed` reads it from other threads)."""
        now = now or time.monotonic()
        self.history.append((now, self.bytes_done))
        while len(self.history) > 2 and now - self.history[0][0] > SPEED_WINDOW:
            self.history.popleft()

    def speed(self, now: Optional[float] = None) -> float:
        now = now or time.monotonic()
        points = list(self.history)
        if len(points) < 2:
            return 0.0
        (t0, b0), (t1, b1) = points[0], points[-1]
        if t1 - t0 < 0.5:
            return 0.0
        # Idle since the last block: let the speed fall instead of showing the old one forever.
        return max(0.0, (b1 - b0) / max(now - t0, t1 - t0))

    def to_dict(self) -> dict[str, Any]:
        speed = self.speed()
        remaining = max(0, self.size - self.bytes_done)
        return {"source_id": self.source_id, "msg_id": self.msg_id, "file_name": self.file_name,
                "source_title": self.source_title, "size": self.size, "bytes_done": self.bytes_done,
                "status": self.status, "note": self.note, "connections": self.connections,
                "speed": round(speed), "eta": round(remaining / speed) if speed > 0 else None,
                "started": self.started}


# --------------------------------------------------------------------------- the manager

class DownloadManager:
    def __init__(self, settings_getter: Callable[[], Any], db: Database, telegram: Any,
                 publish: Callable[[str, Any], None]) -> None:
        self._settings = settings_getter
        self.db = db
        self.telegram = telegram
        self._publish = publish
        self._jobs: dict[tuple[int, int], Job] = {}
        self._lock = threading.Lock()
        self._wake: Optional[asyncio.Event] = None
        self._paused = bool(db.get_value(PAUSED_KEY, False))
        self._blocked: Optional[str] = None
        self._last_publish = 0.0
        self._started = False
        self._stopping = False
        #: Called with (source_id, msg_id) after a file is downloaded (phase 4: extraction).
        self.after_download: Optional[Callable[[int, int], None]] = None

    # ------------------------------------------------------------------ control (any thread)
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.telegram.submit(self._run)

    def stop(self, timeout: float = 10.0) -> None:
        """Before Telegram disconnects: start nothing new, and stop the downloads where they are.

        They stay ``downloading`` and are resumed on the next start.
        """
        self._stopping = True
        self._cancel(lambda _job: True, None, timeout=timeout)

    def wake(self) -> None:
        loop = self.telegram.loop
        if loop is not None and self._wake is not None:
            loop.call_soon_threadsafe(self._wake.set)

    @property
    def paused(self) -> bool:
        return self._paused

    def status(self) -> dict[str, Any]:
        with self._lock:
            jobs = [j.to_dict() for j in self._jobs.values()]
        stats = self.db.queue_stats()
        speed = sum(j["speed"] for j in jobs)
        remaining = (stats["queued_bytes"] - stats["queued_done"]) + sum(j["size"] - j["bytes_done"] for j in jobs)
        me = (self.telegram.status().get("me") or {}) if hasattr(self.telegram, "status") else {}
        return {"paused": self._paused, "blocked": self._blocked, "active": jobs, **stats,
                "speed": speed, "remaining": remaining, "eta": round(remaining / speed) if speed > 0 else None,
                "premium": me.get("premium")}

    def pause(self) -> dict[str, Any]:
        """Stop every download (they keep what they have) and start no new ones."""
        self._paused = True
        self.db.set_value(PAUSED_KEY, True)
        self._cancel(lambda _job: True, QUEUED)
        log.info("Baixades en pausa.")
        self._publish_now()
        return self.status()

    def resume(self) -> dict[str, Any]:
        self._paused = False
        self.db.set_value(PAUSED_KEY, False)
        log.info("Baixades represes.")
        self.wake()
        self._publish_now()
        return self.status()

    def unqueue(self, source_id: int, msg_ids: list[int]) -> int:
        """Take files out of the queue (active ones stop; what they have is kept for later)."""
        ids = set(msg_ids)
        changed = self.db.set_state(source_id, ids, NEW, only_from=(QUEUED,))
        changed += self._cancel(lambda j: j.source_id == source_id and j.msg_id in ids, NEW)
        self._publish_now()
        return changed

    def forget_source(self, source_id: int) -> None:
        """Before a source is deleted: stop its downloads."""
        self._cancel(lambda j: j.source_id == source_id, NEW)

    def active(self, source_id: Optional[int] = None) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if source_id is None or j.source_id == source_id)

    def _cancel(self, match: Callable[[Job], bool], to_state: Optional[str], timeout: float = 10.0) -> int:
        """Stop the matching downloads and wait until they have recorded where they stopped."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if match(j)]
        loop = self.telegram.loop
        tasks = [j.task for j in jobs if j.task is not None]
        if not tasks or loop is None:
            return len(jobs)
        for job in jobs:
            job.cancel_to = to_state
        for task in tasks:
            loop.call_soon_threadsafe(task.cancel)

        async def settled() -> None:
            await asyncio.wait(tasks, timeout=timeout)

        try:
            asyncio.run_coroutine_threadsafe(settled(), loop).result(timeout + 1)
        except Exception:   # pragma: no cover - the state is still right, only later
            log.debug("Les baixades aturades no han acabat a temps")
        return len(jobs)

    # ------------------------------------------------------------------ the loop (service loop)
    async def _run(self) -> None:
        self._wake = asyncio.Event()
        again = await asyncio.to_thread(self.db.requeue_interrupted)
        if again:
            log.info("Baixades: %d fitxers interromputs tornen a la cua i es reprendran.", again)
        while True:
            timeout = PUBLISH_EVERY if self._jobs else POLL_SECONDS
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._fill()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error a la cua de baixades")
            if self._jobs or time.monotonic() - self._last_publish > 30:
                await asyncio.to_thread(self._publish_status)

    async def _fill(self) -> None:
        settings = self._settings()
        blocked: Optional[str] = None
        if self._paused:
            blocked = None
        elif getattr(self.telegram, "state", "ready") != "ready":
            blocked = "Esperant la sessió de Telegram."
        else:
            blocked = storage.storage_problem(settings)
            if blocked is None:
                free = storage.free_bytes(settings.effective_temp_dir)
                if free is not None and free < FREE_MARGIN:
                    blocked = "Queda menys d'1 GB lliure al disc: no es comença cap baixada més."
        if blocked != self._blocked:
            self._blocked = blocked
            if blocked:
                log.warning("Baixades aturades: %s", blocked)
            await asyncio.to_thread(self._publish_status)
        if self._paused or blocked or self._stopping:
            return
        slots = int(settings.max_parallel_files) - len(self._jobs)
        if slots <= 0:
            return
        rows = await asyncio.to_thread(self.db.next_queued, slots, list(self._jobs))
        for row in rows:
            if not await asyncio.to_thread(self.db.start_download, row["source_id"], row["msg_id"]):
                continue
            job = Job(source_id=row["source_id"], msg_id=row["msg_id"], chat_id=row["chat_id"],
                      file_name=row["file_name"], source_title=row["source_title"], size=row["size"],
                      bytes_done=row["bytes_done"])
            with self._lock:
                self._jobs[job.key] = job
            job.task = asyncio.ensure_future(self._download(job))
            self._publish_source(job.source_id)

    def _publish_status(self) -> None:
        self._last_publish = time.monotonic()
        try:
            self._publish("downloads", self.status())
        except Exception:   # pragma: no cover - never kill the loop for the UI
            log.exception("No s'ha pogut publicar l'estat de les baixades")

    def _publish_now(self) -> None:
        loop = self.telegram.loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(asyncio.to_thread(self._publish_status)))

    def _publish_source(self, source_id: int) -> None:
        payload_fn = getattr(self.telegram, "source_payload", None)
        if payload_fn is None:
            return
        try:
            payload = payload_fn(source_id)
        except Exception:   # pragma: no cover
            return
        if payload is not None:
            self._publish("source", payload)

    # ------------------------------------------------------------------ one file
    async def _download(self, job: Job) -> None:
        outcome = "requeue"
        error: Optional[str] = None
        path: Optional[Path] = None
        try:
            path = await self._transfer(job)
            outcome = "done"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Requeue as exc:
            error = str(exc) or None
        except DownloadFailed as exc:
            outcome, error = "error", str(exc)
        except (FileGone, ChatUnavailable) as exc:
            outcome, error = "error", str(exc)
        except (NotAuthorized, NotConfigured) as exc:
            error = str(exc)
        except Exception as exc:
            log.exception("Error inesperat baixant «%s»", job.file_name)
            outcome, error = "error", f"Error inesperat: {exc}"
        finally:
            with self._lock:
                self._jobs.pop(job.key, None)
            try:
                self._finish(job, outcome, error, path)
            except Exception:   # pragma: no cover
                log.exception("No s'ha pogut desar l'estat de «%s»", job.file_name)
            if self._wake is not None:
                self._wake.set()

    def _finish(self, job: Job, outcome: str, error: Optional[str], path: Optional[Path]) -> None:
        """Record how a download ended (runs on the loop, also while it shuts down: quick and synchronous)."""
        if outcome == "done" and path is not None:
            self.db.finish_download(job.source_id, job.msg_id, str(path))
            log.info("Baixat: «%s» (%s)", job.file_name, job.source_title)
            hook = self.after_download
            if hook is not None:
                try:
                    hook(job.source_id, job.msg_id)
                except Exception:
                    log.exception("Error després de baixar «%s»", job.file_name)
        elif outcome == "error":
            self.db.set_state(job.source_id, [job.msg_id], ERROR, error=error, only_from=(DOWNLOADING,))
            log.warning("No s'ha pogut baixar «%s»: %s", job.file_name, error)
        elif outcome == "cancelled":
            if job.cancel_to is None:
                return   # shutting down: stays "downloading", resumed on the next start
            self.db.set_state(job.source_id, [job.msg_id], job.cancel_to, only_from=(DOWNLOADING,))
        else:   # requeue
            self.db.set_state(job.source_id, [job.msg_id], QUEUED, only_from=(DOWNLOADING,))
            if error:
                log.info("«%s» torna a la cua: %s", job.file_name, error)
        self.db.set_progress(job.source_id, job.msg_id, job.bytes_done)
        self._publish_source(job.source_id)
        try:
            self._publish("downloads", self.status())
        except Exception:   # pragma: no cover
            pass

    def _gateway(self) -> Any:
        gw = self.telegram.gateway
        if gw is None or getattr(self.telegram, "state", "ready") != "ready":
            raise Requeue("La sessió de Telegram no està iniciada.")
        return gw

    def _check_storage(self, settings: Any) -> None:
        problem = storage.storage_problem(settings)
        if problem:
            raise Requeue(problem)

    async def _transfer(self, job: Job) -> Path:
        settings = self._settings()
        self._check_storage(settings)
        folder = Path(settings.effective_temp_dir) / f"src-{job.source_id}"
        part = folder / f".{job.msg_id}.part"
        sidecar = folder / f".{job.msg_id}.part.json"
        stall = float(settings.stall_seconds)

        remote: RemoteFile = await self._with_retries(
            job, stall, lambda gw: gw.remote_file(job.chat_id, job.msg_id))
        if remote.size != job.size:
            log.info("«%s»: Telegram diu %d bytes (l'índex en tenia %d).", job.file_name, remote.size, job.size)
            job.size = remote.size

        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        state = await asyncio.to_thread(_load_state, part, sidecar, remote.size)
        job.bytes_done = state.bytes_done
        missing = state.missing()
        needed = sum(state.block_len(i) for i in missing)
        free = await asyncio.to_thread(storage.free_bytes, str(folder))
        if free is not None and free < needed + FREE_MARGIN // 4:
            raise DownloadFailed(f"No hi ha prou espai al disc: calen {needed / 1024 ** 3:.1f} GB i en queden "
                                 f"{free / 1024 ** 3:.1f}.")

        fh = await asyncio.to_thread(_open_part, part, remote.size)
        write_lock = threading.Lock()
        last_save = time.monotonic()
        try:
            queue: asyncio.Queue[int] = asyncio.Queue()
            for index in missing:
                queue.put_nowait(index)
            job.status = "downloading"
            job.progress()

            async def worker() -> None:
                nonlocal last_save
                while True:
                    try:
                        index = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    offset, length = index * BLOCK, state.block_len(index)
                    data = await self._with_retries(job, stall, lambda gw: gw.read_block(remote, offset, BLOCK),
                                                    expect=length)
                    self._check_storage(settings)      # never write to the wrong disk
                    await asyncio.to_thread(_write_at, fh, write_lock, offset, data)
                    state.done.add(index)
                    job.bytes_done += len(data)
                    job.status, job.note = "downloading", None
                    job.progress()
                    if time.monotonic() - last_save >= SAVE_EVERY:
                        last_save = time.monotonic()
                        await asyncio.to_thread(_checkpoint, fh, write_lock, state, sidecar)
                        await asyncio.to_thread(self.db.set_progress, job.source_id, job.msg_id, job.bytes_done)

            count = max(1, min(int(settings.chunks_per_file), len(missing)))
            job.connections = count
            workers = [asyncio.ensure_future(worker()) for _ in range(count)]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
            job.status, job.connections = "finishing", 0
            await asyncio.to_thread(_flush, fh, write_lock)
        finally:
            # Synchronous on purpose: this also runs while the app shuts down (task cancelled).
            if not state.complete and storage.storage_problem(settings) is None:
                try:
                    _checkpoint(fh, write_lock, state, sidecar)
                except OSError as exc:   # pragma: no cover - the disk went away
                    log.warning("No s'ha pogut desar el progrés de «%s»: %s", job.file_name, exc)
            with write_lock:
                fh.close()

        if not state.complete or part.stat().st_size != remote.size:
            raise DownloadFailed("El fitxer baixat no té la mida que diu Telegram.")
        self._check_storage(settings)
        final = await asyncio.to_thread(_promote, part, sidecar, folder, job.file_name, job.msg_id)
        return final

    async def _with_retries(self, job: Job, stall: float, request: Callable[[Any], Any], *,
                            expect: Optional[int] = None) -> Any:
        """One Telegram request with the watchdog: deadline, retries with pauses, FLOOD_WAIT waited out."""
        failures = 0
        while True:
            gw = self._gateway()
            try:
                result = await asyncio.wait_for(request(gw), timeout=stall)
            except FloodWait as exc:
                job.status, job.note = "waiting", f"Telegram demana esperar {exc.seconds} s."
                log.info("«%s»: FLOOD_WAIT de %d s", job.file_name, exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            except asyncio.TimeoutError:
                failures += 1
                reason = f"Telegram no ha respost en {int(stall)} s"
            except (FileGone, ChatUnavailable, NotAuthorized, NotConfigured):
                raise
            except (TelegramError, OSError, ConnectionError) as exc:
                failures += 1
                reason = str(exc) or exc.__class__.__name__
            else:
                if expect is None or len(result) == expect:
                    return result
                failures += 1
                reason = f"Telegram ha enviat {len(result)} bytes en lloc de {expect}"
            if failures >= MAX_FAILURES:
                raise DownloadFailed(f"{reason} ({failures} intents seguits).")
            delay = RETRY_DELAYS[min(failures - 1, len(RETRY_DELAYS) - 1)]
            job.status, job.note = "retrying", f"{reason}; es torna a provar ({failures}/{MAX_FAILURES})."
            log.debug("«%s»: %s; nou intent d'aquí a %d s", job.file_name, reason, delay)
            await asyncio.sleep(delay)


# --------------------------------------------------------------------------- blocking file helpers

def _load_state(part: Path, sidecar: Path, size: int) -> PartState:
    """What is already downloaded; anything inconsistent starts again from zero."""
    if part.exists():
        state = PartState.load(sidecar, size)
        if state is not None and part.stat().st_size == size:
            return state
        log.info("No es pot reprendre %s (falta o no quadra el %s): es comença de nou.", part.name, sidecar.name)
    return PartState(size=size)


def _open_part(part: Path, size: int) -> Any:
    fh = open(part, "r+b" if part.exists() else "w+b")
    try:
        if os.fstat(fh.fileno()).st_size != size:
            fh.truncate(size)       # sparse where the filesystem allows it
    except BaseException:
        fh.close()
        raise
    return fh


def _write_at(fh: Any, lock: threading.Lock, offset: int, data: bytes) -> None:
    with lock:
        fh.seek(offset)
        fh.write(data)


def _flush(fh: Any, lock: threading.Lock) -> None:
    with lock:
        fh.flush()
        os.fsync(fh.fileno())


def _checkpoint(fh: Any, lock: threading.Lock, state: PartState, sidecar: Path) -> None:
    """Make the written blocks durable, *then* record them (never the other way round)."""
    _flush(fh, lock)
    state.save(sidecar)


def _promote(part: Path, sidecar: Path, folder: Path, file_name: str, msg_id: int) -> Path:
    final = target_path(folder, file_name, msg_id)
    os.replace(part, final)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        pass
    return final
