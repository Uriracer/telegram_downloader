"""File names -> episode numbers, with the real names of the Conan backups."""
from __future__ import annotations

import pytest

from tgdl.naming import parse, split_tdl_name


@pytest.mark.parametrize("name, absolute, version, crc, title, story", [
    ("Animelliure_DC_001_Multi_Audio+Subs_Forzados_V2_96D508D8.rar", 1, 2, "96D508D8", "", ""),
    ("13x501_El_xoc_entre_el_vermell_i_el_negre_La_sospita_D4BAF093.rar", 501, 1, "D4BAF093",
     "El xoc entre el vermell i el negre La sospita", ""),
    ("10x424-El fotomissatge del pallasso [DADCAEE6].rar", 424, 1, "DADCAEE6", "El fotomissatge del pallasso", ""),
    ("Animelliure_TotsRucs_El_Detectiu_Conan_525_720pCat_Jap+Subs_ForçatsB8C16FEA.rar", 525, 1, "B8C16FEA", "", ""),
    ("DC-928 (El vermell de l'amor) [70BF1E30].part1.rar", 928, 1, "70BF1E30", "El vermell de l'amor", ""),
    ("DC-1004 (Nus) [5A052F3A].rar", 1004, 1, "5A052F3A", "Nus", ""),
    ("DC-896 (I) [EFC30E87].rar", 896, 1, "EFC30E87", "", "I"),
    ("DC-897 (II) [35F28516].rar", 897, 1, "35F28516", "", "II"),
])
def test_the_four_conan_formats(name: str, absolute: int, version: int, crc: str, title: str, story: str) -> None:
    p = parse(name, "absolute")
    assert (p.absolute, p.version, p.crc, p.title, p.story_part) == (absolute, version, crc, title, story)
    assert p.is_archive and p.recognized


@pytest.mark.parametrize("name, absolute", [
    # Real names from the backup channel: the CRC contains "DC" followed by a digit (or not).
    ("DC-901 (I) [5DC7CC1E].rar", 901),
    ("DC-899 [F9207A9C].rar", 899),
    ("DC-898 [9D9F5682].rar", 898),
    ("10x424-El fotomissatge del pallasso [DADCAEE6].rar", 424),
    # Invented worst cases: the CRC alone would read as DC-7 / DC-123.
    ("Conan_312_[ADC7CC1E].rar", 312),
    ("Episodi 77 [1DC12300].mkv", 77),
    ("Detectiu Conan 055 FFDC1234.mkv", 55),
])
def test_dc_inside_the_crc_is_never_the_episode(name: str, absolute: int) -> None:
    assert parse(name, "absolute").absolute == absolute


def test_a_crc_alone_is_not_an_episode() -> None:
    p = parse("[5DC7CC1E].rar", "absolute")
    assert not p.recognized and p.crc == "5DC7CC1E"


def test_multipart_volumes_share_the_archive_key() -> None:
    one = parse("DC-928 (El vermell de l'amor) [70BF1E30].part1.rar")
    two = parse("DC-928 (El vermell de l'amor) [70BF1E30].part2.rar")
    assert (one.volume, two.volume) == (1, 2) and one.archive_key == two.archive_key
    assert parse("Peli.7z.003").volume == 3 and parse("Peli.7z.003").ext == "7z"
    assert parse("vell.r00").volume == 2 and parse("vell.rar").archive_key == parse("vell.r00").archive_key


def test_nxm_depends_on_the_numbering_of_the_source() -> None:
    absolute = parse("Hawaii Five-0 1x05.mkv", "absolute")
    seasons = parse("Hawaii Five-0 1x05.mkv", "seasons")
    assert absolute.absolute == 5 and absolute.season is None
    assert (seasons.season, seasons.episode, seasons.absolute) == (1, 5, None)


def test_library_names() -> None:
    p = parse("Detectiu Conan - S17E11 - 0501.mkv", "seasons")
    assert (p.season, p.episode, p.absolute) == (17, 11, 501) and p.is_video
    p = parse("Hawaii Five-0 (2010) - S02E10 - Ka Hakaka Maika'i.mkv", "seasons")
    assert (p.season, p.episode) == (2, 10)


def test_noise_and_ambiguity() -> None:
    assert parse("Conan 720p.mkv", "absolute").absolute is None          # a resolution is not an episode
    assert parse("Serie 12 x264 1080p.mkv", "absolute").absolute == 12
    assert parse("Pack 3 de 12.rar", "absolute").absolute is None        # two numbers: we do not guess
    assert parse("poster.jpg").recognized is False


def test_tdl_prefix() -> None:
    assert split_tdl_name("1495790952_1006_DC-901 (I) [5DC7CC1E].rar") == (1495790952, 1006,
                                                                            "DC-901 (I) [5DC7CC1E].rar")
    assert split_tdl_name("13x501_El_xoc.rar") is None
