from __future__ import annotations

from pathlib import Path

import pytest

from tgdl import db as dbm
from tgdl.db import Database, IndexedMessage, SourceOptions


def msg(mid: int, name: str = "", size: int = 100, kind: str = "document") -> IndexedMessage:
    return IndexedMessage(msg_id=mid, date="2024-01-01T00:00:00+00:00", text="", file_name=name or f"f{mid}.rar",
                          size=size, kind=kind)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    d = Database(tmp_path / "tgdl.db")
    yield d
    d.close()


def test_migrations_run_once(tmp_path: Path) -> None:
    d = Database(tmp_path / "x.db")
    assert d.schema_version == len(dbm.MIGRATIONS)
    d.close()
    d = Database(tmp_path / "x.db")   # reopening does not fail on existing tables
    assert d.schema_version == len(dbm.MIGRATIONS)
    d.close()


def test_add_source_is_idempotent_and_keeps_options(database: Database) -> None:
    src = database.add_source(-100, "Nom llarg del canal", "channel", options=SourceOptions(language="es"))
    again = database.add_source(-100, "Nom canviat", "channel")
    assert again.id == src.id
    assert again.title == "Nom canviat"
    assert again.options.language == "es"     # re-adding never resets the options


def test_upsert_never_touches_state(database: Database) -> None:
    src = database.add_source(-100, "C")
    assert database.upsert_messages(src.id, [msg(1), msg(2)]) == 2
    database.set_state(src.id, [1], dbm.IN_LIBRARY)
    # The same message indexed again (e.g. full reindex), with a renamed file:
    assert database.upsert_messages(src.id, [msg(1, "renamed.rar"), msg(3)]) == 1
    m1 = database.message(src.id, 1)
    assert m1 and m1["state"] == dbm.IN_LIBRARY and m1["file_name"] == "renamed.rar"


def test_counts_exclude_photos_and_deleted(database: Database) -> None:
    src = database.add_source(-100, "C")
    database.upsert_messages(src.id, [msg(1, size=10), msg(2, size=20), msg(3, "cover.jpg", kind="photo")])
    database.mark_deleted(src.id, {1, 3})
    s = database.source(src.id)
    assert s is not None
    assert s.counts == {"new": 1} and s.total_size == 10
    total, items = database.messages(src.id)
    assert total == 1 and items[0]["msg_id"] == 1
    total, _ = database.messages(src.id, include_photos=True)
    assert total == 2
    # Seen again: no longer deleted
    database.upsert_messages(src.id, [msg(2, size=20)])
    assert database.messages(src.id)[0] == 2


def test_messages_filters(database: Database) -> None:
    src = database.add_source(-100, "C")
    database.upsert_messages(src.id, [msg(1, "DC-1004 (Nus).rar"), msg(2, "altre.mkv")])
    database.set_state(src.id, [2], dbm.ERROR, error="ha fallat")
    assert database.messages(src.id, q="Nus")[0] == 1
    total, items = database.messages(src.id, state=dbm.ERROR)
    assert total == 1 and items[0]["error"] == "ha fallat"


def test_set_state_only_from(database: Database) -> None:
    src = database.add_source(-100, "C")
    database.upsert_messages(src.id, [msg(1), msg(2)])
    database.set_state(src.id, [1], dbm.IN_LIBRARY)
    assert database.set_state(src.id, [1, 2], dbm.QUEUED, only_from=[dbm.NEW]) == 1
    assert database.message(src.id, 1)["state"] == dbm.IN_LIBRARY
    with pytest.raises(ValueError):
        database.set_state(src.id, [1], "nonsense")


def test_delete_source_cascades(database: Database) -> None:
    src = database.add_source(-100, "C")
    database.upsert_messages(src.id, [msg(1)])
    assert database.delete_source(src.id)
    assert database.query("SELECT COUNT(*) FROM messages")[0][0] == 0


def test_source_options_ignore_garbage() -> None:
    o = SourceOptions.from_dict({"kind": "music", "mode": "library", "language": "español", "auto_index": "no"})
    assert (o.kind, o.mode, o.language, o.auto_index) == ("series", "library", "ca", True)


def test_kv(database: Database) -> None:
    assert database.get_value("x", 5) == 5
    database.set_value("x", {"a": [1, 2]})
    assert database.get_value("x") == {"a": [1, 2]}


def test_tvdb_and_numbering_options() -> None:
    o = SourceOptions.from_dict({"tvdb_id": " 72454 ", "numbering": "absolute"})
    assert (o.tvdb_id, o.numbering) == (72454, "absolute")
    o = SourceOptions.from_dict({"tvdb_id": "abc", "numbering": "x"})
    assert (o.tvdb_id, o.numbering) == (0, "seasons")
    assert SourceOptions.from_dict({"tvdb_id": True}).tvdb_id == 0


def test_next_queued_starts_with_files_already_begun(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from tgdl.db import QUEUED, Database, IndexedMessage

    db = Database(tmp_path / "q.db")
    src = db.add_source(-100, "Canal")
    db.upsert_messages(src.id, [IndexedMessage(i, None, "", f"{i}.rar", 1000) for i in (1, 2, 3)])
    db.set_state(src.id, [1, 2, 3], QUEUED)
    db.set_progress(src.id, 3, 500)
    assert [m["msg_id"] for m in db.next_queued(3)] == [3, 1, 2]
    assert [m["msg_id"] for m in db.next_queued(1, exclude=[(src.id, 3)])] == [1]
    assert db.queue_stats() == {"queued": 3, "queued_bytes": 3000, "queued_done": 500}
    db.close()
