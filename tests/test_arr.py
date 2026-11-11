"""Sonarr/Radarr: the Newznab indexer and the SABnzbd API, through HTTP as Sonarr uses them."""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import CONAN_CHAT, wait_for
from test_server import ORIGIN, Env, login_telegram
from tgdl import arr
from tgdl.db import IN_LIBRARY, NEW, QUEUED

NS = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"}


class ArrEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.dl_dir = tmp_path / "dl"
        self.env = Env(tmp_path, overrides={"download_dir": str(self.dl_dir)})
        self.c = self.env.client
        self.key = self.env.store.get().api_key
        login_telegram(self.env)
        src = self.c.post("/api/sources", json={"chat_id": CONAN_CHAT}, headers=ORIGIN).json()
        self.sid = src["id"]
        wait_for(lambda: self.c.get(f"/api/sources/{self.sid}").json()["files"] == 7)
        self.c.patch(f"/api/sources/{self.sid}", json={"options": {"tvdb_id": 72454, "numbering": "absolute"}},
                     headers=ORIGIN)
        self.db = self.env.harness.db

    def newznab(self, **params: Any) -> ET.Element:
        res = self.c.get("/arr/newznab/api", params={"apikey": self.key, **params})
        assert res.status_code == 200
        return ET.fromstring(res.content)

    def items(self, **params: Any) -> list[dict[str, Any]]:
        out = []
        for item in self.newznab(**params).iter("item"):
            attrs = {a.get("name"): a.get("value") for a in item.findall("newznab:attr", NS)}
            out.append({"title": item.findtext("title"), "link": item.findtext("link"), **attrs})
        return out

    def sab(self, **params: Any) -> Any:
        return self.c.get("/sabnzbd/api", params={"apikey": self.key, "output": "json", **params}).json()

    def add(self, item: dict[str, Any], category: str = "tv") -> str:
        nzb = self.c.get(item["link"]).content
        res = self.c.post("/sabnzbd/api", params={"apikey": self.key, "mode": "addfile", "cat": category},
                          files={"name": ("x.nzb", nzb, "application/x-nzb")}).json()
        assert res["status"] is True, res
        return res["nzo_ids"][0]

    def finish_download(self, msg_id: int, video: bytes = b"V" * 2000) -> None:
        """What the download manager does when a file is complete (here: a ZIP with the episode)."""
        path = self.dl_dir / ".tmp" / f"{msg_id}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("episode.mp4", video)
        self.db.set_state(self.sid, [msg_id], "downloading")
        assert self.db.finish_download(self.sid, msg_id, str(path))
        self.env.app.state.downloads.after_download(self.sid, msg_id)

    def close(self) -> None:
        self.env.app.state.placer.wait()
        self.env.close()


@pytest.fixture
def ae(tmp_path: Path) -> Iterator[ArrEnv]:
    e = ArrEnv(tmp_path)
    yield e
    e.close()


def test_caps_and_credentials(ae: ArrEnv) -> None:
    caps = ae.newznab(t="caps")
    assert caps.find("searching/tv-search").get("available") == "yes"          # type: ignore[union-attr]
    bad = ET.fromstring(ae.c.get("/arr/newznab/api", params={"apikey": "x", "t": "caps"}).content)
    assert bad.tag == "error" and bad.get("code") == "100"
    assert ae.c.get("/sabnzbd/api", params={"mode": "version"}).json()["version"]
    assert ae.c.get("/sabnzbd/api", params={"mode": "queue"}).json()["error"] == "API Key Required"
    assert ae.c.get("/sabnzbd/api", params={"mode": "queue", "apikey": "x"}).json()["error"] == "API Key Incorrect"


def test_tv_search_by_tvdb_id_season_and_episode(ae: ArrEnv) -> None:
    items = ae.items(t="tvsearch", tvdbid=72454)
    # Conan fixture: 7 episodes (DC-928 is two volumes = one release; the poster is not offered).
    assert len(items) == 6
    s17 = [i for i in items if i["season"] == "17" and i["episode"] == "11"]       # 501 = S17E11
    assert len(s17) == 1
    name = s17[0]["title"]
    assert name.startswith("Detective.Conan.S17E11.") and name.endswith(".CATALAN.720p.WEB-DL-TGDL")
    assert s17[0]["size"] == "500000000" and s17[0]["tvdbid"] == "72454" and s17[0]["language"] == "Catalan"
    assert [i["title"] for i in ae.items(t="tvsearch", tvdbid=72454, season=17, ep=11)] == [name]
    assert ae.items(t="tvsearch", tvdbid=99999) == []
    two = [i for i in items if i["size"] == "1000000000"]
    assert len(two) == 1                                   # DC-928: both volumes in one release


def test_title_search_and_rss(ae: ArrEnv) -> None:
    assert len(ae.items(t="tvsearch", q="Detective Conan")) == 6        # TheTVDB title
    assert ae.items(t="tvsearch", q="Hawai") == []
    assert len(ae.items(t="search")) == 6                                # RSS sync
    assert ae.items(t="movie", q="Detective Conan") == []               # films: not yet


def test_nzb_round_trip() -> None:
    tok = arr.Token(3, [7, 8], "Show.S01E01.CATALAN.720p.WEB-DL-TGDL")
    assert arr.token_from_nzb(arr.build_nzb(tok)) == tok
    with pytest.raises(ValueError):
        arr.Token.decode("not-a-token")
    with pytest.raises(ValueError):
        arr.token_from_nzb(b"<nzb/>")


def test_sab_full_cycle(ae: ArrEnv) -> None:
    item = next(i for i in ae.items(t="tvsearch", tvdbid=72454) if i["episode"] == "11")
    nzo = ae.add(item)
    assert ae.add(item) == nzo                                           # asked twice: one job
    msg = ae.db.message(ae.sid, 4)                                       # 13x501…
    assert msg is not None and msg["state"] == QUEUED
    queue = ae.sab(mode="queue")["queue"]
    assert queue["noofslots"] == 1 and queue["slots"][0]["filename"] == item["title"]

    ae.finish_download(4)
    assert ae.env.app.state.placer.wait()
    hist = ae.sab(mode="history")["history"]["slots"]
    assert hist[0]["status"] == "Completed" and hist[0]["nzo_id"] == nzo
    folder = Path(hist[0]["storage"])
    assert folder == ae.dl_dir / "tv" / item["title"]
    assert (folder / f"{item['title']}.mp4").read_bytes() == b"V" * 2000
    assert ae.sab(mode="queue")["queue"]["noofslots"] == 0

    # Sonarr imported it and asks to delete it.
    assert ae.sab(mode="history", name="delete", value=nzo, del_files=1)["status"] is True
    assert not folder.exists()
    assert ae.db.message(ae.sid, 4)["state"] == IN_LIBRARY                # type: ignore[index]
    assert ae.sab(mode="history")["history"]["noofslots"] == 0


def test_library_placement_leaves_sonarr_releases_alone(ae: ArrEnv) -> None:
    ae.c.patch(f"/api/sources/{ae.sid}", json={"options": {"mode": "library"}}, headers=ORIGIN)
    item = next(i for i in ae.items(t="tvsearch", tvdbid=72454) if i["episode"] == "11")
    ae.add(item)
    ae.finish_download(4)
    assert ae.env.app.state.placer.wait()
    assert ae.sab(mode="history")["history"]["slots"][0]["status"] == "Completed"
    assert ae.env.app.state.placer.last(ae.sid) is None                  # nothing placed by phase 4


def test_cancel_and_failure(ae: ArrEnv) -> None:
    items = ae.items(t="tvsearch", tvdbid=72454)
    first = next(i for i in items if i["episode"] == "11")
    nzo = ae.add(first)
    assert ae.sab(mode="queue", name="delete", value=nzo)["status"] is True
    assert ae.db.message(ae.sid, 4)["state"] == NEW                       # type: ignore[index]

    other = next(i for i in items if i["size"] == "1000000000")          # DC-928, two volumes
    nzo2 = ae.add(other)
    ae.db.set_state(ae.sid, [7], "error", error="Telegram no ha respost")
    slot = ae.sab(mode="history")["history"]["slots"][0]
    assert slot["nzo_id"] == nzo2 and slot["status"] == "Failed" and "no ha respost" in slot["fail_message"]
    assert ae.sab(mode="retry", value=nzo2)["status"] is True
    assert ae.db.message(ae.sid, 7)["state"] == QUEUED                    # type: ignore[index]


def test_addurl_and_already_downloaded(ae: ArrEnv) -> None:
    ae.db.set_state(ae.sid, [9], "downloading")                          # DC-1004, downloaded before
    path = ae.dl_dir / ".tmp" / "9.zip"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.mkv", b"k" * 10)
    ae.db.finish_download(ae.sid, 9, str(path))
    def msg_ids(i: dict[str, Any]) -> list[int]:
        return arr.Token.decode(i["link"].split("/arr/nzb/", 1)[1].split(".nzb", 1)[0]).msg_ids

    item = next(i for i in ae.items(t="tvsearch", tvdbid=72454) if msg_ids(i) == [9])
    res = ae.sab(mode="addurl", name=item["link"], cat="tv")
    assert res["status"] is True
    assert ae.env.app.state.placer.wait()
    slot = ae.sab(mode="history")["history"]["slots"][0]
    assert slot["status"] == "Completed" and slot["storage"].endswith(item["title"])
    assert ae.db.message(ae.sid, 9)["state"] == "extracted"              # type: ignore[index]
    assert ae.sab(mode="addurl", name="http://elsewhere/x.nzb")["status"] is False


def test_episodes_in_the_library_are_never_offered(ae: ArrEnv) -> None:
    """Sonarr would 'upgrade' an SDTV file by downloading the very same file again."""
    assert len(ae.items(t="tvsearch", tvdbid=72454)) == 6
    ae.db.mark_in_library(ae.sid, [4], "/data/Series/x.mkv")              # 501 = S17E11, already there
    items = ae.items(t="tvsearch", tvdbid=72454)
    assert len(items) == 5 and not any(i["season"] == "17" and i["episode"] == "11" for i in items)
    assert ae.items(t="tvsearch", tvdbid=72454, season=17, ep=11) == []
    assert len(ae.items(t="search")) == 5


def test_scene_numbering_by_absolute_number(ae: ArrEnv) -> None:
    """Sonarr asks for Detective Conan as season=1&ep=<absolute> (TheXEM): 501 is TheTVDB's S17E11."""
    items = ae.items(t="tvsearch", tvdbid=72454, season=1, ep=501)
    assert len(items) == 1
    assert items[0]["title"].startswith("Detective.Conan.S01E501.") and items[0]["season"] == "1"
    assert items[0]["episode"] == "501"
    token = arr.Token.decode(items[0]["link"].split("/arr/nzb/", 1)[1].split(".nzb", 1)[0])
    assert token.msg_ids == [4] and token.title == items[0]["title"]
    assert ae.items(t="tvsearch", tvdbid=72454, season=17, ep=11)[0]["title"].startswith("Detective.Conan.S17E11.")
    assert ae.items(t="tvsearch", tvdbid=72454, season=1, ep=7777) == []


def test_empty_feed_has_a_placeholder_sonarr_never_grabs(ae: ArrEnv) -> None:
    ae.db.mark_in_library(ae.sid, [3, 4, 5, 6, 7, 8, 9], "/data/Series/x")     # everything already there
    assert ae.items(t="tvsearch", tvdbid=72454) == []                         # searches: nothing
    feed = ae.items(t="tvsearch")                                             # Sonarr's test / RSS sync
    assert len(feed) == 1 and feed[0]["title"].startswith("Telegram.Downloader.Feed.S01E01.")
    nzb = ae.c.get(feed[0]["link"]).content
    res = ae.c.post("/sabnzbd/api", params={"apikey": ae.key, "mode": "addfile", "cat": "tv"},
                    files={"name": ("x.nzb", nzb, "application/x-nzb")}).json()
    assert res["status"] is False and "no existeix" in res["error"]
