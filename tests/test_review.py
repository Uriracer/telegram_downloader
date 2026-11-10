"""TheTVDB mapping, the pre-download check, the library scan and the tdl import scan."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import skyhook_transport
from tgdl.db import Database
from tgdl.review import analyze, find_series_dir, scan_library, scan_tdl_dir, tdl_chat_matches
from tgdl.tvdb import TvdbClient, TvdbError

SERIES = "Detectiu Conan (1996) [tvdbid-72454]"


@pytest.fixture
def db(tmp_path: Path) -> Any:
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def show(db: Database) -> Any:
    return TvdbClient(db, transport=skyhook_transport()).show(72454)


def msg(msg_id: int, name: str, size: int = 1000, state: str = "new") -> dict[str, Any]:
    return {"msg_id": msg_id, "file_name": name, "size": size, "kind": "document", "state": state}


def test_absolute_numbers_become_tvdb_seasons(show: Any) -> None:
    assert show.title == "Detective Conan"
    ep = show.by_absolute(501)
    assert (ep.season, ep.episode) == (17, 11)
    assert (show.by_absolute(896).season, show.by_absolute(896).episode) == (26, 10)
    assert show.by_absolute(1) .title == "Roller Coaster Murder Case"
    assert all(not e.special for e in show.regular())


def test_tvdb_cache_and_offline_fallback(db: Database) -> None:
    calls: list[str] = []
    client = TvdbClient(db, transport=skyhook_transport(calls))
    client.show(72454)
    client.show(72454)
    assert len(calls) == 1 and "skyhook.sonarr.tv/v1/tvdb/shows/en/72454" in calls[0]
    # A new process with SkyHook down still has the copy in the database.
    offline = TvdbClient(db, transport=skyhook_transport(fail=True), cache_seconds=0)
    stale = offline.show(72454)
    assert stale.stale and stale.by_absolute(501) is not None
    with pytest.raises(TvdbError, match="cap sèrie"):
        TvdbClient(db, transport=skyhook_transport()).show(1)
    with pytest.raises(TvdbError, match="No s'ha pogut"):
        TvdbClient(Database(db.path.with_name("other.db")), transport=skyhook_transport(fail=True)).show(72454)


def test_check_report(show: Any) -> None:
    messages = [
        msg(1, "poster.jpg") | {"kind": "photo"},
        msg(2, "Animelliure_DC_001_Multi_Audio+Subs_Forzados_V2_96D508D8.rar"),
        msg(3, "Animelliure_DC_001_Multi_Audio+Subs_Forzados_96D508D7.rar"),        # V1 of the same: duplicate
        msg(4, "DC-003 [11111111].rar"),                                            # 2 is missing
        msg(5, "DC-928 (El vermell de l'amor) [70BF1E30].part1.rar", 2000),
        msg(6, "DC-928 (El vermell de l'amor) [70BF1E30].part2.rar", 500),
        msg(7, "DC-930 [22222222].part2.rar"),                                     # part1 never uploaded
        msg(8, "Llegeix-me.txt"),
        msg(9, "Recopilatori sense número.rar"),
        msg(10, "DC-9999 [33333333].rar"),                                          # not on TheTVDB
        msg(11, "DC-003 [11111111].rar"),                                           # same file uploaded twice
    ]
    report = analyze(messages, numbering="absolute", show=show, library={"S1E4": "/lib/S01E04.mkv"})
    s = report["summary"]
    assert s["releases"] == 7 and s["other_files"] == 1 and s["unrecognized"] == 1
    assert s["not_in_tvdb"] == 1 and s["incomplete"] == 1 and s["in_library"] == 0
    dup = {d["label"]: d for d in report["duplicates"]}
    assert dup["S01E01 · 0001"]["keep"].endswith("_V2_96D508D8.rar")         # the highest version wins
    assert dup["S01E03 · 0003"]["reuploaded"] == ["DC-003 [11111111].rar"]
    r928 = next(r for r in report["releases"] if r["absolute"] == 928)
    assert r928["volumes"] == [1, 2] and r928["complete"] and r928["size"] == 2500
    assert r928["tvdb_title"].startswith("The Scarlet School Trip")
    # 2 is missing; 4 is in the library so it is not; then 5…927 and 929.
    assert report["missing"][0] == {"from": "S01E02 · 0002", "to": "S01E02 · 0002", "count": 1}
    assert report["missing"][1]["from"].endswith("0005")
    assert s["beyond"] > 0


def test_check_without_tvdb_uses_the_gaps() -> None:
    report = analyze([msg(1, "DC-001 [AAAAAAA1].rar"), msg(2, "DC-004 [AAAAAAA2].rar")], numbering="absolute")
    assert report["tvdb"] is None and report["missing"] == [{"from": "2", "to": "3", "count": 2}]


def test_library_scan(tmp_path: Path, show: Any) -> None:
    lib = tmp_path / "Series"
    season = lib / SERIES / "Season 17"
    season.mkdir(parents=True)
    (season / "Detectiu Conan - S17E11 - 0501.mkv").write_bytes(b"x")
    (season / "Detectiu Conan - S17E11 - 0501.nfo").write_text("x")
    (season / "Detectiu Conan - S17E12 - 0502.mkv").write_bytes(b"x")
    (lib / "Una altra (2000) [tvdbid-1]").mkdir()
    folder = find_series_dir(lib, 72454)
    assert folder is not None and folder.name == SERIES
    assert find_series_dir(lib, 5) is None and find_series_dir(tmp_path / "no", 72454) is None
    found = scan_library(folder, show)
    assert set(found) == {"S17E11", "S17E12"}
    report = analyze([msg(1, "13x501_El_xoc_D4BAF093.rar")], numbering="absolute", show=show, library=found)
    assert report["releases"][0]["in_library"] and report["summary"]["library_only"] == 1


def test_tdl_scan(tmp_path: Path) -> None:
    zips = tmp_path / "zips"
    zips.mkdir()
    (zips / "1495790952_1006_DC-901 (I) [5DC7CC1E].rar").write_bytes(b"12345")
    (zips / "1495790952_1007_DC-902 (II) [7B97A34E].rar.tmp").write_bytes(b"1")   # still downloading
    (zips / "notes.txt").write_text("x")
    files = scan_tdl_dir(zips)
    assert [(f.chat, f.msg_id, f.name, f.size) for f in files] == [(1495790952, 1006, "DC-901 (I) [5DC7CC1E].rar", 5)]
    assert tdl_chat_matches(1495790952, -1001495790952) and not tdl_chat_matches(1495790952, -1001111111111)
    with pytest.raises(FileNotFoundError):
        scan_tdl_dir(tmp_path / "missing")


def test_library_names_without_sxxeyy(tmp_path: Path, show: Any) -> None:
    s17 = tmp_path / "Conan" / "Season 17"
    s17.mkdir(parents=True)
    for name in ("501. El xoc entre el vermell i el negre.mkv",   # absolute, with the title
                 "12 - Episodi.mkv",                              # episode of the season folder
                 "Detectiu Conan 0503.mp4"):                      # absolute, Japanese numbering
        (s17 / name).write_bytes(b"x")
    (tmp_path / "Conan" / "Extres").mkdir()
    (tmp_path / "Conan" / "Extres" / "Opening.mkv").write_bytes(b"x")
    found = scan_library(tmp_path / "Conan", show, "absolute")
    assert set(found) == {"S17E11", "S17E12", "S17E13"}
    # A normal series: "Temporada 2/05 - Títol.mkv" is S02E05.
    t2 = tmp_path / "Hawaii" / "Temporada 2"
    t2.mkdir(parents=True)
    (t2 / "05 - Títol.mkv").write_bytes(b"x")
    assert set(scan_library(tmp_path / "Hawaii", None, "seasons")) == {"S2E5"}
