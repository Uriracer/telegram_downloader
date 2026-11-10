"""SQLite store: sources (chats/channels) and the files found in them.

Every message with a file has a **state** that does not depend on what is on
disk (``tdl --skip-same`` re-downloaded archives once they had been extracted
and deleted; this never does)::

    new ─▶ queued ─▶ downloading ─▶ downloaded ─▶ extracting ─▶ extracted ─▶ in_library
      ╲                                                     ╲
       ╰──▶ ignored                                          ╰──▶ error  (message kept)

* ``new``: indexed, nobody asked for it yet.
* ``in_library``: its video is in the library (placed by us, imported by
  Sonarr/Radarr, or found there when reconciling): never downloaded again.

One connection shared by all threads (``check_same_thread=False``) behind a
lock, WAL journal, ``PRAGMA user_version`` migrations.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

log = logging.getLogger(__name__)

DB_FILE = "tgdl.db"

# Message states.
NEW, QUEUED, DOWNLOADING, DOWNLOADED = "new", "queued", "downloading", "downloaded"
EXTRACTING, EXTRACTED, IN_LIBRARY = "extracting", "extracted", "in_library"
IGNORED, ERROR = "ignored", "error"
STATES = (NEW, QUEUED, DOWNLOADING, DOWNLOADED, EXTRACTING, EXTRACTED, IN_LIBRARY, IGNORED, ERROR)

#: Media kinds of indexed messages.
KIND_DOCUMENT, KIND_VIDEO, KIND_PHOTO, KIND_AUDIO = "document", "video", "photo", "audio"

#: Source content kinds and how their files reach the library.
SOURCE_KINDS = ("series", "movies", "other")
SOURCE_MODES = ("arr", "library")       # through Sonarr/Radarr, or placed directly
NUMBERINGS = ("seasons", "absolute")    # see naming.py


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


MIGRATIONS: list[str] = [
    # 1 — sources and messages
    """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL UNIQUE,
        title TEXT NOT NULL,
        chat_kind TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        options TEXT NOT NULL DEFAULT '{}',
        added_at TEXT NOT NULL,
        indexed_at TEXT,
        last_msg_id INTEGER NOT NULL DEFAULT 0,
        index_error TEXT
    );
    CREATE TABLE messages (
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        msg_id INTEGER NOT NULL,
        date TEXT,
        text TEXT NOT NULL DEFAULT '',
        file_name TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        mime TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'document',
        document_id INTEGER,
        dc_id INTEGER,
        grouped_id INTEGER,
        state TEXT NOT NULL DEFAULT 'new',
        bytes_done INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        local_path TEXT,
        deleted INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_id, msg_id)
    );
    CREATE INDEX messages_state ON messages(source_id, state);
    CREATE INDEX messages_name ON messages(source_id, file_name);
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]


@dataclass
class SourceOptions:
    """Per-source preferences (stored as JSON; unknown keys ignored, so it can grow)."""
    kind: str = "series"            # series | movies | other
    mode: str = "arr"               # arr | library
    language: str = "ca"            # ISO 639-1 of the audio: release tag CATALAN/SPANISH…, metadata language
    auto_index: bool = True
    ignore_photos: bool = True
    tvdb_id: int = 0                # TheTVDB series id (0 = none): episode list, library folder [tvdbid-N]
    numbering: str = "seasons"      # seasons (1x05 = S01E05) | absolute (anime: 13x501 = episode 501)
    library_folder: str = ""        # folder of the series in the library ("" = the one tagged [tvdbid-N])

    @classmethod
    def from_dict(cls, data: Any) -> "SourceOptions":
        data = data if isinstance(data, dict) else {}
        out = cls()
        if data.get("kind") in SOURCE_KINDS:
            out.kind = data["kind"]
        if data.get("mode") in SOURCE_MODES:
            out.mode = data["mode"]
        lang = str(data.get("language") or "").strip().lower()
        if len(lang) == 2 and lang.isalpha():
            out.language = lang
        for name in ("auto_index", "ignore_photos"):
            if isinstance(data.get(name), bool):
                setattr(out, name, data[name])
        tvdb = data.get("tvdb_id")
        if isinstance(tvdb, (int, str)) and not isinstance(tvdb, bool) and str(tvdb).strip().isdigit():
            out.tvdb_id = int(str(tvdb).strip())
        if data.get("numbering") in NUMBERINGS:
            out.numbering = data["numbering"]
        folder = data.get("library_folder")
        if isinstance(folder, str) and len(folder) <= 4096 and "\0" not in folder:
            out.library_folder = folder.strip()
        return out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Source:
    id: int
    chat_id: int
    title: str
    chat_kind: str
    username: str
    options: SourceOptions
    added_at: str
    indexed_at: Optional[str]
    last_msg_id: int
    index_error: Optional[str]
    counts: dict[str, int] = field(default_factory=dict)
    total_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = self.options.to_dict()
        data["files"] = sum(self.counts.values())
        return data


@dataclass
class IndexedMessage:
    """What the indexer stores for one message with a file."""
    msg_id: int
    date: Optional[str]
    text: str
    file_name: str
    size: int
    mime: str = ""
    kind: str = KIND_DOCUMENT
    document_id: Optional[int] = None
    dc_id: Optional[int] = None
    grouped_id: Optional[int] = None


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                                     timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # ----------------------------------------------------------------- core
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            log.info("Base de dades: migració %d", number)
            self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={number};\nCOMMIT;")

    @property
    def schema_version(self) -> int:
        return int(self.query("PRAGMA user_version")[0][0])

    # ------------------------------------------------------------------- kv
    def get_value(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_value(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))

    # -------------------------------------------------------------- sources
    def add_source(self, chat_id: int, title: str, chat_kind: str = "", username: str = "",
                   options: Optional[SourceOptions] = None) -> Source:
        opts = (options or SourceOptions()).to_dict()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sources(chat_id, title, chat_kind, username, options, added_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_kind=excluded.chat_kind, "
                "username=excluded.username",
                (chat_id, title, chat_kind, username, json.dumps(opts), utcnow()))
            row = conn.execute("SELECT id FROM sources WHERE chat_id=?", (chat_id,)).fetchone()
        source = self.source(int(row[0]))
        assert source is not None
        return source

    def _source_from_row(self, row: sqlite3.Row) -> Source:
        try:
            options = SourceOptions.from_dict(json.loads(row["options"] or "{}"))
        except ValueError:
            options = SourceOptions()
        return Source(id=row["id"], chat_id=row["chat_id"], title=row["title"], chat_kind=row["chat_kind"],
                      username=row["username"], options=options, added_at=row["added_at"],
                      indexed_at=row["indexed_at"], last_msg_id=row["last_msg_id"], index_error=row["index_error"])

    def _with_counts(self, sources: list[Source]) -> list[Source]:
        if not sources:
            return sources
        by_id = {s.id: s for s in sources}
        marks = ",".join("?" * len(by_id))
        for row in self.query(f"SELECT source_id, state, COUNT(*), COALESCE(SUM(size), 0) FROM messages "
                              f"WHERE deleted=0 AND kind!='photo' AND source_id IN ({marks}) "
                              "GROUP BY source_id, state", list(by_id)):
            src = by_id[row[0]]
            src.counts[row[1]] = row[2]
            src.total_size += row[3]
        return sources

    def sources(self) -> list[Source]:
        rows = self.query("SELECT * FROM sources ORDER BY title COLLATE NOCASE")
        return self._with_counts([self._source_from_row(r) for r in rows])

    def source(self, source_id: int) -> Optional[Source]:
        rows = self.query("SELECT * FROM sources WHERE id=?", (source_id,))
        return self._with_counts([self._source_from_row(rows[0])])[0] if rows else None

    def source_by_chat(self, chat_id: int) -> Optional[Source]:
        rows = self.query("SELECT id FROM sources WHERE chat_id=?", (chat_id,))
        return self.source(rows[0][0]) if rows else None

    def set_source_options(self, source_id: int, options: SourceOptions) -> None:
        self.execute("UPDATE sources SET options=? WHERE id=?", (json.dumps(options.to_dict()), source_id))

    def delete_source(self, source_id: int) -> bool:
        return self.execute("DELETE FROM sources WHERE id=?", (source_id,)) > 0

    def set_index_result(self, source_id: int, *, last_msg_id: Optional[int] = None,
                         error: Optional[str] = None) -> None:
        if last_msg_id is None:
            self.execute("UPDATE sources SET index_error=? WHERE id=?", (error, source_id))
        else:
            self.execute("UPDATE sources SET indexed_at=?, last_msg_id=MAX(last_msg_id, ?), index_error=? "
                         "WHERE id=?", (utcnow(), last_msg_id, error, source_id))

    # ------------------------------------------------------------- messages
    def upsert_messages(self, source_id: int, messages: Iterable[IndexedMessage]) -> int:
        """Insert new messages and refresh the metadata of known ones. Returns how many were new.

        The state, progress and local path of known messages are never touched.
        """
        now = utcnow()
        new = 0
        with self.transaction() as conn:
            for m in messages:
                cur = conn.execute(
                    "INSERT INTO messages(source_id, msg_id, date, text, file_name, size, mime, kind, document_id, "
                    "dc_id, grouped_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_id, msg_id) DO NOTHING",
                    (source_id, m.msg_id, m.date, m.text, m.file_name, m.size, m.mime, m.kind, m.document_id,
                     m.dc_id, m.grouped_id, now))
                if cur.rowcount:
                    new += 1
                else:
                    conn.execute(
                        "UPDATE messages SET date=?, text=?, file_name=?, size=?, mime=?, kind=?, document_id=?, "
                        "dc_id=?, grouped_id=?, deleted=0 WHERE source_id=? AND msg_id=?",
                        (m.date, m.text, m.file_name, m.size, m.mime, m.kind, m.document_id, m.dc_id,
                         m.grouped_id, source_id, m.msg_id))
        return new

    def mark_deleted(self, source_id: int, present_ids: set[int]) -> int:
        """After a full scan: flag messages that are no longer in the chat."""
        known = {r[0] for r in self.query("SELECT msg_id FROM messages WHERE source_id=? AND deleted=0",
                                          (source_id,))}
        gone = sorted(known - present_ids)
        with self.transaction() as conn:
            conn.executemany("UPDATE messages SET deleted=1 WHERE source_id=? AND msg_id=?",
                             [(source_id, mid) for mid in gone])
        return len(gone)

    def messages(self, source_id: int, *, q: str = "", state: str = "", offset: int = 0, limit: int = 200,
                 include_photos: bool = False) -> tuple[int, list[dict[str, Any]]]:
        where = ["source_id=?", "deleted=0"]
        params: list[Any] = [source_id]
        if not include_photos:
            where.append("kind!='photo'")
        if q:
            where.append("(file_name LIKE ? OR text LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if state:
            where.append("state=?")
            params.append(state)
        clause = " AND ".join(where)
        total = self.query(f"SELECT COUNT(*) FROM messages WHERE {clause}", params)[0][0]
        rows = self.query(f"SELECT msg_id, date, text, file_name, size, mime, kind, state, bytes_done, error, "
                          f"grouped_id FROM messages WHERE {clause} ORDER BY msg_id LIMIT ? OFFSET ?",
                          params + [limit, offset])
        return total, [dict(r) for r in rows]

    def all_messages(self, source_id: int) -> list[dict[str, Any]]:
        """Every live message of a source (for the check), oldest first."""
        rows = self.query("SELECT msg_id, file_name, size, kind, state, local_path, grouped_id FROM messages "
                          "WHERE source_id=? AND deleted=0 ORDER BY msg_id", (source_id,))
        return [dict(r) for r in rows]

    def mark_downloaded(self, source_id: int, msg_id: int, local_path: str, *,
                        only_from: Iterable[str] = (NEW, QUEUED, ERROR)) -> bool:
        """A complete copy of this file is at ``local_path`` (e.g. downloaded by tdl)."""
        allowed = list(only_from)
        with self.transaction() as conn:
            return conn.execute(
                f"UPDATE messages SET state=?, bytes_done=size, local_path=?, error=NULL, updated_at=? "
                f"WHERE source_id=? AND msg_id=? AND state IN ({','.join('?' * len(allowed))})",
                [DOWNLOADED, local_path, utcnow(), source_id, msg_id] + allowed).rowcount > 0

    def message(self, source_id: int, msg_id: int) -> Optional[dict[str, Any]]:
        rows = self.query("SELECT * FROM messages WHERE source_id=? AND msg_id=?", (source_id, msg_id))
        return dict(rows[0]) if rows else None

    def set_state(self, source_id: int, msg_ids: Iterable[int], state: str, *, error: Optional[str] = None,
                  only_from: Optional[Iterable[str]] = None) -> int:
        """Change the state of some messages (optionally only those currently in ``only_from``)."""
        if state not in STATES:
            raise ValueError(state)
        ids = list(msg_ids)
        if not ids:
            return 0
        extra, extra_params = "", []
        if only_from is not None:
            allowed = list(only_from)
            extra = f" AND state IN ({','.join('?' * len(allowed))})"
            extra_params = allowed
        changed = 0
        with self.transaction() as conn:
            for mid in ids:
                changed += conn.execute(
                    f"UPDATE messages SET state=?, error=?, updated_at=? WHERE source_id=? AND msg_id=?{extra}",
                    [state, error, utcnow(), source_id, mid] + extra_params).rowcount
        return changed


def open_database(data_dir: Path | str) -> Database:
    return Database(Path(data_dir) / DB_FILE)
