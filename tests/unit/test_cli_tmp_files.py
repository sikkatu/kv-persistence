"""Unit tests: crash-leftover ``*.tmp.<uuid>`` accounting and safe purge.

Review points covered:
* ``*.bin.tmp.<uuid>`` files are surfaced in `status` (they consume disk but
  are invisible to the cache index);
* `purge --tmp` removes ONLY those leftovers, never real cache files;
* `purge --tmp` / `purge --all` SKIP files modified within the active-write
  window -- deleting a file the adapter is still DMA-writing into would
  corrupt a store in progress.

No network, no LMCache: the CLI logic is stdlib-only.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

from kvpersist.cli import (  # noqa: E402
    _TMP_ACTIVE_WINDOW_SEC,
    _tmp_files,
    _tmp_files_safe_to_delete,
    cmd_purge,
    cmd_status,
)


class _Args:
    def __init__(self, **kw):
        self.all = kw.get("all", False)
        self.tmp = kw.get("tmp", False)
        self.yes = kw.get("yes", False)
        self.namespace = kw.get("namespace")
        self.namespace_pos = kw.get("namespace_pos")


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    root.mkdir()
    return root


def _touch(root: Path, name: str, size: int = 100, age_sec: float | None = None) -> Path:
    p = root / name
    p.write_bytes(b"x" * size)
    if age_sec is not None:
        old = time.time() - age_sec
        os.utime(p, (old, old))
    return p


def test_tmp_files_are_detected(tmp_path):
    root = _make_root(tmp_path)
    real = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin")
    tmp = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin.tmp." + "f" * 32)
    found = _tmp_files(str(root))
    assert len(found) == 1
    p, final = found[0]
    assert Path(p) == tmp
    assert final == real.name, "the leftover must point at its final name"


def test_status_reports_tmp_files(tmp_path, monkeypatch):
    root = _make_root(tmp_path)
    _touch(root, "m_00000000_0_" + "a" * 64 + ".bin", size=100)
    _touch(root, "m_00000000_0_" + "a" * 64 + ".bin.tmp." + "f" * 32, size=250)

    monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
    monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "100")

    import io
    import json as _json
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_status(_Args())
    assert rc == 0
    rep = _json.loads(buf.getvalue())
    assert rep["tmp_files"] == 1, "status must report crash leftovers"
    assert rep["tmp_files_bytes"] == 250


def test_purge_tmp_removes_only_leftovers(tmp_path, monkeypatch):
    root = _make_root(tmp_path)
    real = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin", size=100,
                  age_sec=3600)
    leftover = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin.tmp." + "f" * 32,
                      size=250, age_sec=3600)

    monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
    monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "100")

    rc = cmd_purge(_Args(tmp=True, yes=True))
    assert rc == 0
    assert leftover.exists() is False, "the crash leftover must be removed"
    assert real.exists() is True, "the real cache file must survive purge --tmp"


def test_purge_tmp_skips_active_writes(tmp_path, monkeypatch):
    """Review fix: a fresh *.tmp.* may be an in-flight store -- keep it."""
    root = _make_root(tmp_path)
    active = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin.tmp." + "f" * 32,
                    size=100)  # mtime = now
    stale = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin.tmp." + "e" * 32,
                   size=100, age_sec=_TMP_ACTIVE_WINDOW_SEC + 60)

    monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
    monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "100")

    safe = _tmp_files_safe_to_delete(str(root))
    assert len(safe) == 1 and Path(safe[0][0]) == stale, (
        "only leftovers older than the active-write window are safe to delete"
    )

    rc = cmd_purge(_Args(tmp=True, yes=True))
    assert rc == 0
    assert active.exists() is True, (
        "an actively-written *.tmp.* file must NOT be deleted"
    )
    assert stale.exists() is False, "a stale leftover must be deleted"


def test_purge_all_also_skips_active_writes(tmp_path, monkeypatch):
    root = _make_root(tmp_path)
    active = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin.tmp." + "f" * 32,
                    size=100)
    real = _touch(root, "m_00000000_0_" + "a" * 64 + ".bin", size=100,
                  age_sec=3600)

    monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
    monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "100")

    rc = cmd_purge(_Args(all=True, yes=True))
    assert rc == 0
    assert active.exists() is True, "purge --all must also skip active writes"
    assert real.exists() is False, "real cache files are removed by --all"


def test_purge_tmp_skips_registered_inflight_writes(tmp_path, monkeypatch):
    """The active-write REGISTRY is authoritative: a tmp file whose store is
    registered is never deleted, even when it is OLD (beyond the mtime window).
    This is the fix for the review point that the mtime window alone is only a
    heuristic -- a write longer than the window must still be safe.
    """
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    final_name = "m_00000000_0_" + "a" * 64 + ".bin"
    inflight = _touch(root, final_name + ".tmp." + "f" * 32, size=100,
                      age_sec=3600)  # OLD -- would pass the mtime heuristic
    stale_leftover = _touch(root, "m_00000000_0_" + "b" * 64 + ".bin.tmp." +
                           "e" * 32, size=100, age_sec=3600)

    marker = tmp_guard.register_write(str(root), final_name)
    assert marker is not None, "registry must accept the in-flight write"

    try:
        monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
        monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "100")

        rc = cmd_purge(_Args(tmp=True, yes=True))
        assert rc == 0
        assert inflight.exists() is True, (
            "a REGISTERED in-flight tmp file must never be deleted, "
            "regardless of its mtime"
        )
        assert stale_leftover.exists() is False, (
            "an unregistered old leftover must still be swept"
        )
    finally:
        tmp_guard.unregister_write(marker)


def test_registry_unregister_frees_the_file(tmp_path):
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    final_name = "m_00000000_0_" + "a" * 64 + ".bin"
    marker = tmp_guard.register_write(str(root), final_name)
    assert tmp_guard.active_final_names(str(root)) == {final_name}
    tmp_guard.unregister_write(marker)
    assert tmp_guard.active_final_names(str(root)) == set()


def test_stale_markers_are_ignored_and_swept(tmp_path):
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    marker = tmp_guard.register_write(str(root), "m_0.bin")
    assert marker is not None
    old = time.time() - tmp_guard.STALE_MARKER_SEC - 60
    os.utime(marker, (old, old))

    # Ignored: its writer is gone.
    assert tmp_guard.active_final_names(str(root)) == set()
    # And swept on demand.
    assert tmp_guard.sweep_stale_markers(str(root)) == 1
    assert Path(marker).exists() is False