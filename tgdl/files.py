"""Folder browser for the web UI: list sub-folders (never file contents), inside the NAS only.

With a storage sentinel (``/data/.tgdl-nas``) the browser cannot leave its
folder (``/data``): the UI is used to pick library and download folders, and
nothing else of the container is shown.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from .naming import VIDEO_EXTS, split_tdl_name

#: Above this many sub-folders the list is cut (the UI says so).
MAX_DIRS = 2000


class BrowseError(Exception):
    """Catalan message; ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def browse_root(settings: Any) -> Optional[Path]:
    """The folder the browser may not leave, or None (no sentinel: anywhere)."""
    sentinel = str(getattr(settings, "storage_sentinel", "") or "")
    return Path(os.path.abspath(Path(sentinel).parent)) if sentinel else None


def default_path(settings: Any) -> str:
    root = browse_root(settings)
    if root is not None:
        return str(root)
    for name in ("library_tv_dir", "download_dir"):
        value = str(getattr(settings, name, "") or "")
        if value and os.path.isdir(value):
            return value
    return str(Path.home())


def _within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.normcase(path)).relative_to(os.path.normcase(root))
        return True
    except ValueError:
        return False


def browse(path: str, settings: Any) -> dict[str, Any]:
    """Sub-folders of ``path`` plus what the folder holds (videos, archives, tdl files)."""
    root = browse_root(settings)
    raw = (path or "").strip()
    if not raw or not Path(raw).is_absolute():          # empty, or a placeholder text: start somewhere useful
        raw = default_path(settings)
    target = Path(os.path.abspath(raw))
    if root is not None and not _within(target, root):
        target = root
    # A folder that does not exist yet (a download folder still to be created): show the nearest parent.
    while not target.exists() and target.parent != target and (root is None or _within(target.parent, root)):
        target = target.parent
    try:
        entries = list(os.scandir(target))
    except FileNotFoundError:
        raise BrowseError(f"No existeix la carpeta {target}.", 404) from None
    except NotADirectoryError:
        raise BrowseError(f"{target} no és una carpeta.", 422) from None
    except PermissionError:
        raise BrowseError(f"No hi ha permís per llegir {target}.", 403) from None
    except OSError as exc:
        raise BrowseError(f"No es pot llegir {target}: {exc.strerror or exc}", 502) from None

    dirs: list[dict[str, str]] = []
    counts = {"files": 0, "videos": 0, "archives": 0, "tdl": 0}
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            if entry.is_dir():
                dirs.append({"name": name, "path": os.path.join(str(target), name)})
                continue
            if not entry.is_file():
                continue
        except OSError:
            continue
        counts["files"] += 1
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in VIDEO_EXTS:
            counts["videos"] += 1
        elif ext in {"rar", "zip", "7z"} or ext.isdigit():
            counts["archives"] += 1
        if split_tdl_name(name):
            counts["tdl"] += 1
    dirs.sort(key=lambda d: d["name"].casefold())
    parent: Optional[str] = str(target.parent) if target.parent != target else None
    if parent is not None and root is not None and not _within(Path(parent), root):
        parent = None
    return {"path": str(target), "parent": parent, "root": str(root) if root else None,
            "dirs": dirs[:MAX_DIRS], "truncated": len(dirs) > MAX_DIRS, **counts}
