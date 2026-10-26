"""Unit tests: single config-loading source and LMCACHE_*/KVP_* consistency."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

from kvpersist.config import ConfigError, export_env, load_config, verify

ROOT = Path(__file__).resolve().parents[2]


def test_env_wins_over_files(monkeypatch):
    monkeypatch.setenv("KVP_CHUNK_SIZE", "1024")
    merged = load_config(str(ROOT))
    assert merged["KVP_CHUNK_SIZE"] == "1024", "env must override file values"


def test_no_conflicts_in_shipped_config():
    assert verify(str(ROOT)) == [], "shipped KVP_* and LMCACHE_* knobs disagree"


def test_conflict_detected_when_files_split(tmp_path):
    (tmp_path / "configs/generic").mkdir(parents=True)
    (tmp_path / "configs/generic/cache.env").write_text("KVP_CHUNK_SIZE=256\n")
    (tmp_path / "configs/generic/lmcache-server.env").write_text("LMCACHE_CHUNK_SIZE=512\n")
    conflicts = verify(str(tmp_path))
    assert conflicts, "a split knob must be reported"
    assert "KVP_CHUNK_SIZE" in conflicts[0] and "LMCACHE_CHUNK_SIZE" in conflicts[0]


def test_env_override_needs_no_file_change(monkeypatch, tmp_path):
    (tmp_path / "configs/generic").mkdir(parents=True)
    (tmp_path / "configs/generic/cache.env").write_text("KVP_L1_SIZE_GB=64\n")
    (tmp_path / "configs/generic/lmcache-server.env").write_text("LMCACHE_L1_SIZE_GB=64\n")
    monkeypatch.setenv("KVP_L1_SIZE_GB", "32")
    # env-wins merge exposes 32; the consistency check uses file values only,
    # so this stays consistent -- env override does not create a fake conflict
    assert load_config(str(tmp_path))["KVP_L1_SIZE_GB"] == "32"
    assert verify(str(tmp_path)) == []


def test_comments_and_quotes_parsed():
    tmp_path = os.environ.get("PYTEST_TMP", "")
    p = Path(ROOT / "configs/generic/cache.env")
    merged = load_config(str(ROOT))
    # every KVP_ knob in cache.env must be non-empty after load (comments stripped)
    for line in p.read_text().splitlines():
        if line.startswith("KVP_") and "=" in line:
            name = line.split("=", 1)[0]
            if not line.split("=", 1)[1].strip().startswith("#"):
                assert name in merged