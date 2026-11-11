"""Episode numbers from release file names.

The four Detective Conan formats seen in the backups (plus the usual ones)::

    Animelliure_DC_001_Multi_Audio+Subs_Forzados_V2_96D508D8.rar         -> absolute 1, V2
    13x501_El_xoc_entre_el_vermell_i_el_negre_La_sospita_D4BAF093.rar     -> absolute 501
    10x424-El fotomissatge del pallasso [DADCAEE6].rar                    -> absolute 424
    Animelliure_TotsRucs_El_Detectiu_Conan_525_720pCat_Jap+Subs_ForçatsB8C16FEA.rar -> 525
    DC-928 (El vermell de l'amor) [70BF1E30].part1.rar                    -> 928, volume 1
    DC-896 (I) [EFC30E87].rar                                             -> 896, story part I

The CRC is removed **before** looking for numbers: ``[5DC7CC1E]`` contains
"DC7" and ``[DADCAEE6]`` contains "DC", and neither is an episode. So are
resolutions (``720p``), codecs and versions (``V2``).

``NxM`` is ambiguous: in an anime with absolute numbering (``13x501``) M is
the absolute number, in a normal series (``1x05``) it is season × episode.
The source decides (``numbering``).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

ABSOLUTE, SEASONS = "absolute", "seasons"
NUMBERINGS = (ABSOLUTE, SEASONS)

VIDEO_EXTS = {"mkv", "mp4", "avi", "m4v", "ts", "webm", "mov", "wmv", "mpg", "mpeg"}
ARCHIVE_EXTS = {"rar", "zip", "7z"}

# name.part2.rar | name_part2.rar (Animelliure) | name.rar | name.7z.001 | name.zip.003 | name.mkv
_EXT = re.compile(r"(?:[._ -]part(?P<part>\d{1,3}))?\.(?P<ext>[A-Za-z0-9]{1,4})(?:\.(?P<vol>\d{3}))?$")
_OLD_RAR = re.compile(r"\.r(?P<num>\d{2})$", re.I)          # name.r00, name.r01 … (name.rar is volume 1)
_TDL = re.compile(r"^(?P<chat>\d{6,})_(?P<msg>\d+)_(?P<name>.+)$")

_HEX8 = "[0-9A-Fa-f]{8}"
_CRC_BRACKET = re.compile(rf"[\[(](?P<crc>{_HEX8})[\])]")
# Unbracketed at the end, after a separator or glued to a word ("ForçatsB8C16FEA"):
# upper-case hex with at least one digit and one letter, so a date or a word is not taken.
_CRC_TAIL = re.compile(r"(?:(?<=[\s_.\-])|(?<=[^\W\d_]))(?P<crc>(?=[0-9A-F]*[0-9])(?=[0-9A-F]*[A-F])[0-9A-F]{8})$")
_VERSION = re.compile(r"(?:(?<=\d)|(?<![A-Za-z0-9]))[vV](?P<v>[1-9])(?![A-Za-z0-9])")
_NOISE = re.compile(r"(?<![A-Za-z0-9])(?:\d{3,4}[pPiI]|[xXhH]\.?26[45]|HEVC|AVC|AAC|AC3|DTS|\d{1,2}bits?|"
                    r"(?:Dual|Multi)[ ._-]?Audio|WEB[ ._-]?DL|BD[ ._-]?Rip|DVD[ ._-]?Rip)(?![A-Za-z0-9])", re.I)
_ROMAN = re.compile(r"^(?:I|II|III|IV|V|VI)$")

_SXXEYY = re.compile(r"(?<![A-Za-z0-9])[Ss](?P<s>\d{1,2})[ ._-]?[Ee](?P<e>\d{1,4})(?!\d)")
_NXM = re.compile(r"(?<![A-Za-z0-9])(?P<s>\d{1,2})[xX](?P<e>\d{1,4})(?!\d)")
# "DC-Movie 22", "DC Movie 22", "Animelliure_DC_Movie_01_v2…": a film, not an episode.
_MOVIE = re.compile(r"(?<![A-Za-z0-9])(?:DC[ ._-]?)?(?:Movie|Pel[·.]?l[ií]cula|Pelicula)[ ._-]*(?P<n>\d{1,3})(?!\d)", re.I)
_DC = re.compile(r"(?<![A-Za-z0-9])DC[ ._-]?(?P<n>\d{1,4})(?!\d)", re.I)
_CONAN = re.compile(r"(?<![A-Za-z0-9])Conan[ ._-]+#?(?P<n>\d{1,4})(?!\d)", re.I)
_EPWORD = re.compile(r"(?<![A-Za-z0-9])(?:Ep|Episodi|Episodio|Episode|Cap[ií]tol|Cap[ií]tulo|Cap)[ ._#-]*(?P<n>\d{1,4})(?!\d)",
                     re.I)
_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?P<n>\d{1,4})(?![A-Za-z0-9])")
_YEAR = re.compile(r"[\[(](?:19|20)\d{2}[\])]")


@dataclass(frozen=True)
class ParsedName:
    """What a file name says. ``absolute`` and ``season``/``episode`` may both be set."""
    stem: str                       # without extension, volume suffix and tdl prefix
    ext: str                        # "rar", "mkv"… (lower case, "" if none)
    volume: int = 1                 # multipart archives: 1, 2, 3…
    archive_key: str = ""           # same for every volume of one archive
    absolute: Optional[int] = None
    movie: Optional[int] = None     # "DC-Movie 22": the number of a film in a series of films
    season: Optional[int] = None
    episode: Optional[int] = None
    version: int = 1
    crc: str = ""
    title: str = ""
    story_part: str = ""            # "(I)", "(II)": half of a two-part story, not part of the title
    rule: str = ""                  # which pattern matched (shown in the check)

    @property
    def recognized(self) -> bool:
        return self.absolute is not None or (self.season is not None and self.episode is not None)

    @property
    def is_archive(self) -> bool:
        return self.ext in ARCHIVE_EXTS

    @property
    def is_video(self) -> bool:
        return self.ext in VIDEO_EXTS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recognized"] = self.recognized
        return data


def split_tdl_name(file_name: str) -> Optional[tuple[int, int, str]]:
    """``1495790952_1001_DC-896 (I) [EFC30E87].rar`` -> (1495790952, 1001, ``DC-896 (I) [EFC30E87].rar``)."""
    m = _TDL.match(file_name)
    return (int(m["chat"]), int(m["msg"]), m["name"]) if m else None


def _split_ext(name: str) -> tuple[str, str, int]:
    m = _OLD_RAR.search(name)
    if m:
        return name[:m.start()], "rar", int(m["num"]) + 2
    m = _EXT.search(name)
    if not m:
        return name, "", 1
    ext = m["ext"].lower()
    if ext not in VIDEO_EXTS | ARCHIVE_EXTS | {"srt", "ass", "nfo", "jpg", "png"}:
        return name, "", 1
    volume = int(m["part"] or m["vol"] or 1)
    return name[:m.start()], ext, volume


def _clean_title(text: str) -> str:
    text = text.replace("_", " ")
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -.,")
    return text


def parse(file_name: str, numbering: str = ABSOLUTE) -> ParsedName:
    """Read episode number, version, CRC and title from a file name."""
    name = file_name.strip()
    stem, ext, volume = _split_ext(name)
    archive_key = stem.lower()

    work = stem
    crc = ""
    m = _CRC_BRACKET.search(work)
    if m:
        crc = m["crc"].upper()
        work = work[:m.start()] + " " + work[m.end():]
    else:
        m = _CRC_TAIL.search(work)
        if m:
            crc = m["crc"]
            work = work[:m.start()]

    version = 1
    m = _VERSION.search(work)
    if m:
        version = int(m["v"])
        work = work[:m.start()] + " " + work[m.end():]
    work = _YEAR.sub(" ", work)
    work = _NOISE.sub(" ", work)

    fields: dict[str, Any] = {}
    title = story = ""
    if (m := _MOVIE.search(work)):
        fields = {"movie": int(m["n"]), "rule": "Movie N"}
    elif (m := _SXXEYY.search(work)):
        fields = {"season": int(m["s"]), "episode": int(m["e"]), "rule": "SxxEyy"}
        tail = work[m.end():]
        absm = re.match(r"\s*-\s*(\d{1,4})(?!\d)", tail)          # "S17E11 - 0501" (our own library names)
        if absm:
            fields["absolute"] = int(absm[1])
    elif (m := _NXM.search(work)):
        s, e = int(m["s"]), int(m["e"])
        if numbering == ABSOLUTE:
            fields = {"absolute": e, "rule": "NxAbs"}
        else:
            fields = {"season": s, "episode": e, "rule": "NxM"}
        title = _clean_title(work[m.end():])
    elif (m := _DC.search(work)):
        fields = {"absolute": int(m["n"]), "rule": "DC-N"}
        paren = re.match(r"\s*\(([^)]*)\)", work[m.end():])
        if paren:
            inner = paren[1].strip()
            if _ROMAN.match(inner):
                story = inner
            else:
                title = _clean_title(inner)
    elif (m := _CONAN.search(work)):
        fields = {"absolute": int(m["n"]), "rule": "Conan N"}
    elif (m := _EPWORD.search(work)):
        fields = {"absolute" if numbering == ABSOLUTE else "episode": int(m["n"]), "rule": "Ep N"}
    else:
        numbers = list(_NUMBER.finditer(work))
        if len(numbers) == 1:
            fields = {"absolute" if numbering == ABSOLUTE else "episode": int(numbers[0]["n"]), "rule": "N"}

    if fields.get("absolute") == 0:          # "DC-000"? not an episode
        fields.pop("absolute")
        fields.pop("rule", None)
    return ParsedName(stem=stem, ext=ext, volume=volume, archive_key=archive_key, version=version, crc=crc,
                      title=title, story_part=story, **fields)
