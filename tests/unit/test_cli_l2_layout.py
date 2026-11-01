"""Unit tests: admin CLI L2-layout handling (flat *.bin files). No network."""
import os
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))
import pytest
from kvpersist.cli import _real_cache_files


def _make_cache_dir(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    return root


def _touch(root: Path, name: str, size: int = 100):
    p = root / name
    p.write_bytes(b"x" * size)
    return p


def test_real_cache_files_parses_names_with_underscores(tmp_path):
    """`my_model` must NOT be truncated to `my` (review round 13)."""
    root = _make_cache_dir(tmp_path)
    _touch(root, "my_model_00000000_0_" + "a" * 64 + ".bin")
    files = _real_cache_files(str(root))
    assert len(files) == 1
    _p, ns = files[0]
    assert ns == "my_model", f"namespace parsed as {ns!r}, expected 'my_model'"


def test_real_cache_files_groups_by_model_prefix(tmp_path):
    root = _make_cache_dir(tmp_path)
    _touch(root, "mimo-pro--a84fadf1_00000000_0_" + "a" * 64 + ".bin")
    _touch(root, "mimo-pro--a84fadf1_00000001_0_" + "b" * 64 + ".bin")
    _touch(root, "other-model_00000000_0_" + "c" * 64 + ".bin")
    files = _real_cache_files(str(root))
    assert len(files) == 3
    namespaces = sorted({ns for _p, ns in files})
    assert namespaces == ["mimo-pro--a84fadf1", "other-model"]


def test_real_cache_files_handles_unparseable_names(tmp_path):
    """A .bin file that doesn't match the adapter pattern is still counted,
    grouped by its stem (no crash, no truncation)."""
    root = _make_cache_dir(tmp_path)
    _touch(root, "weird_file.bin")
    files = _real_cache_files(str(root))
    assert len(files) == 1
    _p, ns = files[0]
    assert ns == "weird_file"


def test_real_cache_files_ignores_non_bin_and_dirs(tmp_path):
    root = _make_cache_dir(tmp_path)
    _touch(root, "good_00000000_0_" + "a" * 64 + ".bin")
    _touch(root, "not-a-cache.txt")
    (root / "subdir").mkdir()
    files = _real_cache_files(str(root))
    assert len(files) == 1


def test_purge_exact_match_only(tmp_path, monkeypatch):
    """`--namespace MiMo` must NOT delete `MiMo-V2.6-Pro` (review round 13:
    the old substring rule deleted everything containing the string)."""
    root = _make_cache_dir(tmp_path)
    _touch(root, "MiMo_00000000_0_" + "a" * 64 + ".bin")
    _touch(root, "MiMo-V2.6-Pro_00000000_0_" + "b" * 64 + ".bin")
    monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
    monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "1")
    from kvpersist import cli as cli_mod
    class _Args:
        namespace = "MiMo"
        all = False
        yes = True
    rc = cli_mod.cmd_purge(_Args())
    assert rc == 0
    remaining = [f.name for f in root.iterdir()]
    assert any("MiMo-V2.6-Pro" in n for n in remaining), \
        "exact-match purge must NOT touch MiMo-V2.6-Pro"
    assert not any(n.startswith("MiMo_00000000") for n in remaining), \
        "exact-match purge must delete MiMo_*"


def test_purge_no_match_exits_nonzero(tmp_path, monkeypatch):
    root = _make_cache_dir(tmp_path)
    monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
    from kvpersist import cli as cli_mod
    class _Args:
        namespace = "does-not-exist"
        all = False
        yes = True
    rc = cli_mod.cmd_purge(_Args())
    assert rc == 1, "purge with no matching namespace must exit 1, not 0"