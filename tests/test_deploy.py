"""Deployment files: the guarantees the docs promise."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

import tgdl
from tgdl.runtime import LOCKABLE_FIELDS

ROOT = Path(__file__).resolve().parent.parent


def test_entrypoint_never_creates_the_download_dir() -> None:
    """With the NAS unmounted, mkdir would fill the host's root disk."""
    script = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    for line in script.splitlines():
        if "mkdir" in line and not line.lstrip().startswith("#"):
            assert "DOWNLOAD_DIR" not in line, line


def test_dockerfile_pins_7zz_for_both_architectures() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for arch in ("AMD64", "ARM64"):
        assert re.search(rf"SEVENZIP_SHA256_{arch}=[0-9a-f]{{64}}\b", text)
    assert "sha256sum -c" in text
    assert "7zzs /usr/local/bin/7zz" in text


def test_compose_examples_use_known_variables() -> None:
    known = set(LOCKABLE_FIELDS.values()) | {"PUID", "PGID", "UMASK", "TZ"}
    data = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    service = data["services"]["telegram-downloader"]
    assert set(service["environment"]) <= known
    assert "/mnt/Plex:/data" in service["volumes"]
    assert service["restart"] == "unless-stopped"


def test_version_matches_changelog() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = tgdl.__version__.split(".dev")[0]
    assert f"## [{version}]" in changelog or f"## {version}" in changelog
