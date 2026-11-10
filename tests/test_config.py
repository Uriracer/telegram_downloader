from __future__ import annotations

import json
from pathlib import Path

from conftest import API_HASH
from tgdl.config import Settings, load_settings, save_settings, validation_errors


def test_defaults_are_sane() -> None:
    s = Settings()
    assert s.server_port == 8766
    assert len(s.api_key) == 32
    assert not s.telegram_configured
    assert s.effective_temp_dir.endswith(".tmp")


def test_telegram_configured_needs_both_values() -> None:
    assert not Settings(telegram_api_id=1).telegram_configured
    assert Settings(telegram_api_id=1, telegram_api_hash=API_HASH).telegram_configured


def test_invalid_values_fall_back_to_defaults() -> None:
    s = Settings.from_dict({"max_parallel_files": 99, "telegram_api_hash": "not-hex", "chunks_per_file": "x",
                            "stall_seconds": True, "unknown": 1, "temp_dir": "  /tmp/x  "})
    assert s.max_parallel_files == 2
    assert s.telegram_api_hash == ""
    assert s.chunks_per_file == 4
    assert s.stall_seconds == 90
    assert s.temp_dir == "/tmp/x"


def test_api_hash_is_normalised() -> None:
    assert Settings.from_dict({"telegram_api_hash": API_HASH.upper()}).telegram_api_hash == API_HASH


def test_validation_errors_are_catalan() -> None:
    errors = validation_errors({"max_parallel_files": 0, "telegram_api_hash": "abc", "download_dir": "/ok"})
    assert set(errors) == {"max_parallel_files", "telegram_api_hash"}
    assert "hexadecimals" in errors["telegram_api_hash"]


def test_updated_keeps_api_key_when_empty() -> None:
    s = Settings()
    assert s.updated({"api_key": ""}).api_key == s.api_key


def test_load_creates_and_repairs(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    first = load_settings(path)
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == first.api_key
    path.write_text("{broken", encoding="utf-8")
    again = load_settings(path)
    assert (tmp_path / "settings.json.bak").exists()
    assert again.api_key != first.api_key   # defaults, new key


def test_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    s = Settings(telegram_api_id=99, telegram_api_hash=API_HASH, storage_sentinel="/data/.tgdl-nas")
    save_settings(s, path)
    assert load_settings(path) == s
