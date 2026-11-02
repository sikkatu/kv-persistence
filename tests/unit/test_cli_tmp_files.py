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
    """A marker whose writer PROVABLY exited is ignored (its tmp is deletable)
    and swept after the grace period. Liveness is judged by (pid, start-time),
    not by age (review fix: a write may legitimately run for hours)."""
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    reg = tmp_guard.registry_dir(str(root))
    Path(reg).mkdir(parents=True, exist_ok=True)
    # A marker for a process that certainly does not exist.
    marker = Path(reg) / "999999-12345-abcdef0123456789.active"
    marker.write_text("m_0.bin\n0\n")

    # Dead writer -> its tmp is NOT protected.
    assert tmp_guard.active_final_names(str(root)) == set()
    # Fresh dead marker: grace period, not swept yet.
    assert tmp_guard.sweep_stale_markers(str(root)) == 0
    assert marker.exists()
    # After the grace period it is swept.
    old = time.time() - tmp_guard.STALE_MARKER_SEC - 60
    os.utime(marker, (old, old))
    assert tmp_guard.sweep_stale_markers(str(root)) == 1
    assert marker.exists() is False


def test_live_marker_protects_tmp_regardless_of_age(tmp_path, monkeypatch):
    """Review fix: a REGISTERED write is protected for however long it runs --
    the marker's age must NOT matter when the writer process is alive."""
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    final_name = "m_00000000_0_" + "a" * 64 + ".bin"
    inflight = _touch(root, final_name + ".tmp." + "f" * 32, size=100,
                      age_sec=3600)  # old tmp file

    marker = tmp_guard.register_write(str(root), final_name)
    assert marker is not None
    # Age the marker far beyond the stale window: the writer is this test's
    # own process, which is ALIVE -- so the write must stay protected.
    ancient = time.time() - tmp_guard.STALE_MARKER_SEC - 600
    os.utime(marker, (ancient, ancient))

    try:
        assert tmp_guard.active_final_names(str(root)) == {final_name}, (
            "a live writer's marker must protect its file regardless of age"
        )
        monkeypatch.setenv("KVP_CACHE_ROOT", str(root))
        monkeypatch.setenv("KVP_CACHE_BUDGET_GB", "100")
        rc = cmd_purge(_Args(tmp=True, yes=True))
        assert rc == 0
        assert inflight.exists() is True, (
            "an OLD tmp file of a LIVE writer must never be deleted"
        )
    finally:
        tmp_guard.unregister_write(marker)


def test_writer_alive_false_for_dead_process():
    from kvpersist import tmp_guard

    # PID 2^22 is above the default Linux pid_max -> cannot exist. On a
    # Linux host with /proc the probe reports "provably dead" (None), so the
    # marker is NOT protected; elsewhere the probe is "unknown" and safety
    # keeps the marker.
    probe = tmp_guard._proc_start_time(4194304)
    if probe is None:  # /proc present and the process is gone
        assert tmp_guard.writer_alive(4194304, 12345) is False
    else:  # "unknown" -> cannot prove death -> keep
        assert tmp_guard.writer_alive(4194304, 12345) is True

    # A live process with the right start-time is alive.
    me = os.getpid()
    start = tmp_guard._proc_start_time(me)
    if isinstance(start, int):  # /proc available (Linux)
        assert tmp_guard.writer_alive(me, start) is True
        # Same PID but a DIFFERENT start-time = reused PID -> dead.
        assert tmp_guard.writer_alive(me, start + 1) is False


# --- docker launch mode: writer in a DIFFERENT PID namespace (round 18) -----


def test_writer_alive_falls_back_to_heartbeat_in_foreign_pid_ns(tmp_path, monkeypatch):
    """Review fix (round 18): the docker launch mode starts the cache service
    in a container WITHOUT --pid=host, so the marker's PID lives in the
    container's namespace -- the host CLI cannot map it to /proc/<pid>.
    The old code treated that as "provably dead" (the host PID may simply not
    exist) and `purge --tmp` deleted a LIVE store's tmp file. Liveness must
    fall back to the marker's heartbeat: fresh mtime = alive, stale = dead.
    """
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    reg = tmp_guard.registry_dir(str(root))
    Path(reg).mkdir(parents=True, exist_ok=True)
    self_ns = tmp_guard._self_pid_ns()

    # Simulate a marker written by a process in ANOTHER pid namespace.
    foreign_ns = self_ns + "-fake" if self_ns else "pid:[4026539999]"
    marker = Path(reg) / "7-4242-abcdef0123456789.active"
    marker.write_text("m_0.bin\n0\n%s\n" % foreign_ns)

    # Fresh heartbeat (just written) -> alive, regardless of the PID.
    assert tmp_guard.active_final_names(str(root)) == {"m_0.bin"}, (
        "a marker from a foreign PID namespace with a fresh heartbeat must "
        "protect its tmp file -- the host cannot check its PID"
    )

    # Age the marker past the heartbeat window -> the writer is gone.
    old = time.time() - tmp_guard.HEARTBEAT_ALIVE_SEC - 60
    os.utime(marker, (old, old))
    assert tmp_guard.active_final_names(str(root)) == set(), (
        "a stale-heartbeat marker from a foreign PID namespace is a crash "
        "leftover and must not protect anything"
    )


def test_marker_records_writer_pid_namespace(tmp_path):
    """register_write() must record the writer's PID namespace so the CLI can
    tell a foreign-namespace writer from a dead one."""
    from kvpersist import tmp_guard

    root = _make_root(tmp_path)
    final_name = "m_00000000_0_" + "a" * 64 + ".bin"
    marker = tmp_guard.register_write(str(root), final_name)
    assert marker is not None
    try:
        lines = Path(marker).read_text().splitlines()
        assert len(lines) >= 3, (
            "marker must carry (final_name, timestamp, pid_namespace)"
        )
        assert lines[2] == tmp_guard._self_pid_ns() or lines[2] == "", (
            "the third line must be the writer's /proc/self/ns/pid id "
            "(empty when /proc is unavailable)"
        )
    finally:
        tmp_guard.unregister_write(marker)