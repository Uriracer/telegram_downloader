"""Phase 4: extract downloaded releases and place them in the library (ZIP archives: no 7-Zip needed)."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import skyhook_transport
from tgdl.config import Settings
from tgdl.db import DOWNLOADED, IN_LIBRARY, Database, IndexedMessage, SourceOptions
from tgdl.placement import (NFO_MARK, PlacementError, Placer, episode_basename, link_volumes, nfo_xml, pick_video,
                            ready_releases, show_name)
from tgdl.review import build_releases, season_dir
from tgdl.tvdb import TvdbClient

SERIES = "Detectiu Conan (1996) [tvdbid-72454]"


class Lib:
    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.root = tmp_path
        self.tv = tmp_path / "Series"
        self.series = self.tv / SERIES
        (self.series / "Season 17").mkdir(parents=True)
        self.zips = tmp_path / "zips"
        self.zips.mkdir()
        self.settings = Settings(download_dir=str(tmp_path / "dl"), library_tv_dir=str(self.tv), **settings)
        self.db = Database(tmp_path / "t.db")
        self.tvdb = TvdbClient(self.db, transport=skyhook_transport())
        self.src = self.db.add_source(-100, "Conan", options=SourceOptions(tvdb_id=72454, numbering="absolute",
                                                                           mode="library"))
        self.events: list[tuple[str, Any]] = []
        self.placer = Placer(lambda: self.settings, self.db, show_getter=lambda s: self.tvdb.show(72454),
                             publish=lambda n, p: self.events.append((n, p)),
                             source_payload=lambda sid: {"id": sid}, seven_zip=lambda: None)

    def add(self, msg_id: int, name: str, path: Path) -> None:
        self.db.upsert_messages(self.src.id, [IndexedMessage(msg_id, None, "", name, path.stat().st_size)])
        assert self.db.mark_downloaded(self.src.id, msg_id, str(path))

    def zip_episode(self, msg_id: int, name: str, videos: dict[str, bytes], *, tdl: bool = True) -> Path:
        path = self.zips / (f"1495790952_{msg_id}_{name}" if tdl else name)
        with zipfile.ZipFile(path, "w") as zf:
            for inner, data in videos.items():
                zf.writestr(inner, data)
        self.add(msg_id, name, path)
        return path

    def msg(self, msg_id: int) -> dict[str, Any]:
        m = self.db.message(self.src.id, msg_id)
        assert m is not None
        return m

    def run(self, limit: int | None = None) -> dict[str, Any]:
        self.placer.place(self.src.id, limit)
        assert self.placer.wait()
        last = self.placer.last(self.src.id)
        assert last is not None
        return last


@pytest.fixture
def lib(tmp_path: Path) -> Iterator[Lib]:
    lb = Lib(tmp_path)
    yield lb
    lb.db.close()


def test_extracts_names_and_places_an_episode(lib: Lib) -> None:
    archive = lib.zip_episode(1242, "DC-501 (El xoc) [D4BAF093].zip", {"DC-501.mp4": b"v" * 5000, "sample.mp4": b"s" * 10})
    res = lib.run()
    assert res["done"] == 1 and res["failed"] == 0
    target = lib.series / "Season 17" / "Detectiu Conan - S17E11 - 0501.mp4"    # 501 = S17E11 (TheTVDB)
    assert target.read_bytes() == b"v" * 5000
    nfo = target.with_suffix(".nfo").read_text(encoding="utf-8")
    assert "<title>501. El xoc</title>" in nfo and "<season>17</season>" in nfo and "<episode>11</episode>" in nfo
    assert '<uniqueid type="absolute">501</uniqueid>' in nfo and NFO_MARK in nfo
    m = lib.msg(1242)
    assert m["state"] == IN_LIBRARY and m["local_path"] == str(target) and m["error"] is None
    assert not archive.exists()                                        # archive deleted once in the library
    assert not (Path(lib.settings.effective_temp_dir) / "extract").exists() or not any(
        (Path(lib.settings.effective_temp_dir) / "extract").iterdir())


def test_new_season_folder_and_tvdb_title_with_story_part(lib: Lib) -> None:
    lib.zip_episode(10, "DC-896 (I) [EFC30E87].zip", {"a.mkv": b"x" * 100})
    assert lib.run()["done"] == 1
    placed = list((lib.series / "Season 26").iterdir())                 # created: 896 = S26E10
    assert sorted(p.name for p in placed) == ["Detectiu Conan - S26E10 - 0896.mkv",
                                              "Detectiu Conan - S26E10 - 0896.nfo"]
    nfo = (lib.series / "Season 26" / "Detectiu Conan - S26E10 - 0896.nfo").read_text(encoding="utf-8")
    assert "<title>896. " in nfo and "(I)</title>" not in nfo        # TheTVDB's title already says which part


def test_story_part_is_added_to_titles_from_the_file_name(lib: Lib) -> None:
    lib.zip_episode(11, "DC-501 (El xoc) (II) [D4BAF093].zip", {"a.mkv": b"x"})
    lib.zip_episode(12, "DC-502 (II) [D4BAF094].zip", {"a.mkv": b"x"})
    lib.placer._show = lambda s: None     # no TheTVDB: numbers only… and no season: reported
    res = lib.run()
    assert res["failed"] == 2 and "temporada" in res["errors"][0]["error"]


def test_nfo_is_readable_by_everyone(lib: Lib) -> None:
    import os
    import stat

    lib.zip_episode(13, "DC-501 [D4BAF093].zip", {"a.mkv": b"x"})
    assert lib.run()["done"] == 1
    nfo = lib.series / "Season 17" / "Detectiu Conan - S17E11 - 0501.nfo"
    if os.name == "posix":
        assert stat.S_IMODE(nfo.stat().st_mode) & 0o044 == 0o044


def test_never_overwrites_a_different_file(lib: Lib) -> None:
    existing = lib.series / "Season 17" / "Detectiu Conan - S17E11 - 0501.mp4"
    existing.write_bytes(b"old")
    archive = lib.zip_episode(5, "DC-501 [D4BAF093].zip", {"new.mp4": b"n" * 50})
    res = lib.run()
    assert res["failed"] == 1 and "fitxer diferent" in res["errors"][0]["error"]
    assert existing.read_bytes() == b"old" and archive.exists()
    m = lib.msg(5)
    assert m["state"] == DOWNLOADED and "fitxer diferent" in m["error"]


def test_archive_with_two_episodes_is_reported(lib: Lib) -> None:
    lib.zip_episode(6, "DC-502 [AAAAAAAA].zip", {"a.mp4": b"a" * 100, "b.mp4": b"b" * 90})
    res = lib.run()
    assert res["failed"] == 1 and "2 vídeos" in res["errors"][0]["error"]
    assert lib.msg(6)["state"] == DOWNLOADED


def test_plain_video_is_moved_and_limit_is_respected(lib: Lib) -> None:
    for mid, number in ((20, 503), (21, 504)):
        video = lib.zips / f"1495790952_{mid}_DC-{number}.mkv"
        video.write_bytes(b"m" * 10)
        lib.add(mid, f"DC-{number}.mkv", video)
    assert lib.run(limit=1)["total"] == 1
    assert lib.msg(20)["state"] == IN_LIBRARY and lib.msg(21)["state"] == DOWNLOADED
    assert not (lib.zips / "1495790952_20_DC-503.mkv").exists()
    assert lib.run()["done"] == 1 and lib.msg(21)["state"] == IN_LIBRARY


def test_keeps_archives_when_asked(tmp_path: Path) -> None:
    lb = Lib(tmp_path, delete_archives=False)
    try:
        archive = lb.zip_episode(7, "DC-505 [BBBBBBBB].zip", {"e.mp4": b"e"})
        assert lb.run()["done"] == 1 and archive.exists()
    finally:
        lb.db.close()


def test_nothing_is_written_without_the_nas(tmp_path: Path) -> None:
    lb = Lib(tmp_path, storage_sentinel=str(tmp_path / ".tgdl-nas"))
    try:
        lb.zip_episode(8, "DC-506 [CCCCCCCC].zip", {"e.mp4": b"e"})
        res = lb.run()
        assert res["failed"] == 1 and "no està muntat" in res["errors"][0]["error"]
        assert lb.msg(8)["state"] == DOWNLOADED and not any((lb.series / "Season 17").iterdir())
    finally:
        lb.db.close()


def test_ready_releases_need_every_volume(lib: Lib) -> None:
    msgs = [{"msg_id": 1, "file_name": "DC-928 (X) [70BF1E30].part1.rar", "size": 1, "kind": "document",
             "state": DOWNLOADED, "local_path": "/a"},
            {"msg_id": 2, "file_name": "DC-928 (X) [70BF1E30].part2.rar", "size": 1, "kind": "document",
             "state": "new", "local_path": None},
            {"msg_id": 3, "file_name": "DC-929 [11111111].rar", "size": 1, "kind": "document",
             "state": DOWNLOADED, "local_path": "/b"}]
    assert [r.release.ref.absolute for r in ready_releases(msgs, "absolute", None)] == [929]  # type: ignore[union-attr]


def test_volumes_get_their_original_names(tmp_path: Path) -> None:
    a, b = tmp_path / "9_1_X.part1.rar", tmp_path / "9_2_X.part2.rar"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    links = link_volumes([("X.part1.rar", a), ("X.part2.rar", b)], tmp_path / "vols")
    assert [p.name for p in links] == ["X.part1.rar", "X.part2.rar"]
    assert links[1].read_bytes() == b"2"


def test_names_and_nfo_escape(tmp_path: Path) -> None:
    assert show_name(Path(SERIES)) == "Detectiu Conan"
    assert show_name(Path("Hawaii Five-0 (2010) {tvdb-164541}")) == "Hawaii Five-0"
    rel = build_releases([{"msg_id": 1, "file_name": "1x05 <Títol> & co.mkv", "size": 1, "kind": "video",
                           "state": DOWNLOADED}], "seasons", None)[0][0]
    assert episode_basename("Sèrie", rel.ref, "seasons") == "Sèrie - S01E05"   # type: ignore[arg-type]
    assert "&lt;Títol&gt; &amp; co" in nfo_xml("Sèrie", rel)
    with pytest.raises(PlacementError):
        pick_video(tmp_path)
    (tmp_path / "Season 03").mkdir()
    assert season_dir(tmp_path, 3).name == "Season 03" and season_dir(tmp_path, 4).name == "Season 4"
