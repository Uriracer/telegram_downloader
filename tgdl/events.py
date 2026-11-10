"""Server-Sent Events fan-out (from any thread to every open ``/api/events`` stream).

Same design as 3Cat Downloader's: one ``asyncio.Queue`` per connection, fed
with ``loop.call_soon_threadsafe``; slow consumers lose their oldest events.
Events are ``(name, json)`` pairs: ``telegram`` (session state), ``source``
(index progress), later ``job`` (downloads).

Progress goes here, **not** to the log: running ``tdl`` as a service filled the
journal with a progress line every few milliseconds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

EVENT_QUEUE_SIZE = 1000
CLOSE = object()   # pushed to subscriber queues to end their stream


class Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self.alive = True

    def push(self, item: object) -> None:
        if not self.alive:
            return
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:   # loop already closed
            self.alive = False

    def _put(self, item: object) -> None:   # runs on self.loop
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:   # pragma: no cover
                pass
        self.queue.put_nowait(item)


class EventBroadcaster:
    def __init__(self, maxsize: int = EVENT_QUEUE_SIZE) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._subs: set[Subscriber] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: str, payload: Any) -> None:
        """Serialise now and deliver everywhere (never raises)."""
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            log.exception("No s'ha pogut serialitzar l'esdeveniment %s", event)
            return
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.push((event, data))

    def open(self, loop: asyncio.AbstractEventLoop) -> Subscriber:
        sub = Subscriber(loop, self._maxsize)
        with self._lock:
            if self._closed:
                sub.push(CLOSE)
            self._subs.add(sub)
        return sub

    def discard(self, sub: Subscriber) -> None:
        sub.alive = False
        with self._lock:
            self._subs.discard(sub)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            sub.push(CLOSE)


def sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"
