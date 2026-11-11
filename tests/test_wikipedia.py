"""Phase 6: Catalan titles from the Catalan Wikipedia, in new and existing .nfo files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from tgdl.db import Database
from tgdl.placement import NFO_MARK, nfo_xml, retitle_nfos
from tgdl.review import build_releases
from tgdl.wikipedia import Entry, WikipediaClient, clean, parse_wikitext

FIXTURE = Path(__file__).parent / "fixtures" / "viquipedia-conan.wikitext"
PAGE = "Llista d'episodis d'El detectiu Conan"


def test_rows_by_japanese_number() -> None:
    entries = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
    assert sorted(entries) == [886, 887, 888]
    assert entries[886].title == "Resolent misteris al Cafè Poirot. Segona part"       # link and final dot gone
    assert entries[886].catalan_number == 941
    assert entries[886].plot == ("Cap dels tres sospitosos d'haver apunyalat en en Tengo té taques de sang. "
                                 "L'home de la gavardina ajudarà en Conan.")
    assert entries[887].plot.startswith("Després de la mort")
    assert entries[888].title == "En Kaito Kid i la caixa secreta. Segona part" and entries[888].plot == ""


def test_clean_markup() -> None:
    assert clean("A<ref name=x>{{ref-web|url=y}}</ref> [[B|C]] '''D''' {{Abr|E|F}}<br />G") == "A C D G"


def test_client_reads_the_list_and_its_season_pages() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params.get("action", ""))
        if params["action"] == "query":
            return httpx.Response(200, json={"query": {"pages": [{"title": PAGE, "links": [
                {"title": f"{PAGE} (temporades 21-30)"}, {"title": "El detectiu Conan"}]}]}})
        text = FIXTURE.read_text(encoding="utf-8") if "21-30" in params["page"] else "Índex sense taules."
        return httpx.Response(200, json={"parse": {"wikitext": text}})

    class Mem:
        def __init__(self) -> None:
            self.d: dict[str, Any] = {}

        def get_value(self, k: str, default: Any = None) -> Any:
            return self.d.get(k, default)

        def set_value(self, k: str, v: Any) -> None:
            self.d[k] = json.loads(json.dumps(v))

    db = Mem()
    entries = WikipediaClient(db, transport=httpx.MockTransport(handler)).episodes(PAGE)
    assert sorted(entries) == [886, 887, 888] and calls == ["query", "parse", "parse"]   # the list, its season page
    again = WikipediaClient(db, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert again.episodes(PAGE)[887].title.startswith("En Kaito Kid")                     # from the database


def _release(name: str) -> Any:
    from tgdl.review import EpisodeRef

    rel = build_releases([{"msg_id": 1, "file_name": name, "size": 1, "kind": "document", "state": "downloaded"}],
                         "absolute", None)[0][0]
    rel.ref = EpisodeRef("S26E1", 26, 1, 887)
    return rel


def test_new_nfo_uses_the_catalan_title_and_plot() -> None:
    entry = Entry(887, "En Kaito Kid i la caixa secreta. Primera part", "La senyora Tomoyose & <companyia>.")
    xml = nfo_xml("Detectiu Conan", _release("DC-887 (I) [E7E41CF7].rar"), entry)
    assert "<title>887. En Kaito Kid i la caixa secreta. Primera part</title>" in xml
    assert "<plot>La senyora Tomoyose &amp; &lt;companyia&gt;.</plot>" in xml and NFO_MARK in xml
    assert "<title>Episodi 887 (I)</title>" in nfo_xml("Detectiu Conan", _release("DC-887 (I) [E7E41CF7].rar"))


def test_retitle_only_our_nfo_files(tmp_path: Path) -> None:
    season = tmp_path / "Season 26"
    season.mkdir()
    ours = season / "Detectiu Conan - S26E01 - 0887.nfo"
    ours.write_text(nfo_xml("Detectiu Conan", _release("DC-887 (I) [E7E41CF7].rar")), encoding="utf-8")
    old = season / "Detectiu Conan - S25E83 - 0886.nfo"
    old_text = ("<?xml version=\"1.0\"?><episodedetails><title>886. Vell</title>"
                "<uniqueid type=\"absolute\">886</uniqueid><!-- conan-catala --></episodedetails>")
    old.write_text(old_text, encoding="utf-8")
    unknown = season / "Detectiu Conan - S30E99 - 1099.nfo"
    unknown.write_text(ours.read_text(encoding="utf-8").replace(">887<", ">1099<"), encoding="utf-8")

    entries = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
    res = retitle_nfos(tmp_path, entries)
    assert res == {"updated": 1, "unchanged": 0, "missing": [1099], "skipped": 1}
    text = ours.read_text(encoding="utf-8")
    assert "<title>887. En Kaito Kid i la caixa secreta. Primera part</title>" in text
    assert "<plot>Després de la mort" in text and NFO_MARK in text and "<season>26</season>" in text
    assert old.read_text(encoding="utf-8") == old_text                           # not ours: untouched
    assert retitle_nfos(tmp_path, entries)["unchanged"] == 1                     # second run: nothing to do


def test_source_option_round_trip(tmp_path: Path) -> None:
    from tgdl.db import SourceOptions

    db = Database(tmp_path / "t.db")
    src = db.add_source(-1, "Conan", options=SourceOptions.from_dict({"wikipedia_list": f"  {PAGE} "}))
    assert db.source(src.id).options.wikipedia_list == PAGE                      # type: ignore[union-attr]
    db.close()
