"""The folder browser of the web UI."""
from __future__ import annotations

from pathlib import Path

import pytest

from tgdl.config import Settings
from tgdl.files import BrowseError, browse


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    root = tmp_path / "Plex"
    (root / "Series" / "Detectiu Conan (1996) [tvdbid-72454]" / "Season 17").mkdir(parents=True)
    (root / "Series" / "aaa").mkdir()
    (root / ".amagada").mkdir()
    (root / "_conan" / "zips").mkdir(parents=True)
    (root / "_conan" / "zips" / "1495790952_1001_DC-896 (I) [EFC30E87].rar").write_bytes(b"x")
    (root / "_conan" / "zips" / "nota.txt").write_text("x")
    (root / ".tgdl-nas").write_text("")
    return root


def test_lists_folders_inside_the_nas_only(nas: Path, tmp_path: Path) -> None:
    settings = Settings(storage_sentinel=str(nas / ".tgdl-nas"))
    top = browse("", settings)
    assert top["path"] == str(nas) and top["parent"] is None
    assert [d["name"] for d in top["dirs"]] == ["_conan", "Series"]           # hidden folders are not shown
    series = browse(str(nas / "Series"), settings)
    assert [d["name"] for d in series["dirs"]] == ["aaa", "Detectiu Conan (1996) [tvdbid-72454]"]
    assert series["parent"] == str(nas)
    zips = browse(str(nas / "_conan" / "zips"), settings)
    assert (zips["files"], zips["archives"], zips["tdl"]) == (2, 1, 1)
    assert browse(str(tmp_path), settings)["path"] == str(nas)               # outside: back to the NAS
    assert browse(str(nas / ".." / ".."), settings)["path"] == str(nas)
    assert browse("automàtica ([tvdbid-…])", settings)["path"] == str(nas)   # a placeholder, not a path
    assert browse(str(nas / "Descarregues" / "telegram"), settings)["path"] == str(nas)   # not created yet


def test_without_sentinel(nas: Path) -> None:
    settings = Settings(library_tv_dir=str(nas / "Series"))
    assert browse("", settings)["path"] == str(nas / "Series")
    with pytest.raises(BrowseError):
        browse(str(nas / ".tgdl-nas"), settings)                            # a file, not a folder
