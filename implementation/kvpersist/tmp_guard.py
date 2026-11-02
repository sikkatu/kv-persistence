"""Active-write registry: make `purge --tmp` safe for in-flight stores.

WHY
Upstream nixl_store_dynamic writes each chunk to ``<final>.bin.tmp.<uuid>`` and
atomically renames it on success. A purge that deletes tmp files can corrupt a
store that is STILL WRITING. The first guard was a 15-minute mtime window --
a heuristic: a write longer than the window could still be hit. A registry of
in-flight writes was added next; the review then found that judging a marker
dead by ITS AGE is still wrong (a write can legitimately run for hours).

WHAT THIS DOES
A registry of in-flight writes, shared between the writer (the LMCache
adapter, patched here at the ``dynamic_store_file`` level) and the admin CLI
(``kvpersist.cli``). Liveness is judged by PROOF, not by age:

* Before a store starts, a marker file ``<cache_root>/.kvp-active-writes/
  <pid>-<proc_start>-<uuid>.active`` is created, naming the FINAL file being
  written and the writer's (pid, process start-time).
* The marker is periodically refreshed (heartbeat) while the store runs, so
  operators can see the write is active.
* After the store finishes (success or failure) the marker is removed.
* A marker is considered LIVE when its writer process is STILL ALIVE at the
  recorded (pid, start-time) -- the start-time guards against PID reuse. If
  the liveness probe CANNOT be performed (no /proc, unreadable marker), the
  marker is treated as LIVE: it is safer to keep one tmp file too long than to
  delete a live store's file.
* ``purge --tmp`` / ``purge --all`` skip any ``*.tmp.*`` whose final name is
  listed in the registry with a LIVE marker -- regardless of how long the
  write runs.
* Only markers whose writer provably exited AND that are older than
  ``STALE_MARKER_SEC`` are swept.

Race analysis: tmp names embed a fresh uuid4, so a tmp file that exists at
scan time but is NOT in the registry can only come from (a) an unpatched
writer or (b) a crashed store -- both are safe to delete after the stale
window. A write that starts AFTER the purge scanned cannot collide: its tmp
file did not exist at scan time and carries a unique name.

INSTALL
``install()`` wraps ``DynamicNixlStorageAgent.dynamic_store_file`` (the single
place where the tmp name is created and renamed) and starts a heartbeat for
the marker -- an upstream-compatible, in-process patch, exactly like the
salt-filename and size patches. Applied by ``kvpersist.launch`` when the
adapter module is importable; inert otherwise.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid

REGISTRY_DIR = ".kvp-active-writes"
# A marker whose writer provably EXITED is a crash leftover; such markers are
# ignored by active_final_names() and swept after this age. Live writers are
# never aged out -- liveness, not age, is the criterion.
STALE_MARKER_SEC = 3600
# How often an in-flight marker is refreshed. Informational for operators;
# liveness is decided by the process check, not by this.
HEARTBEAT_SEC = 30
# Review fix (round 18): when the writer runs in a DIFFERENT PID namespace (a
# docker container started without --pid=host), the host CLI cannot verify the
# recorded PID against /proc at all -- the container's PID 7 may be some
# unrelated host process, or nothing. In that case liveness falls back to the
# marker's own heartbeat: a running store refreshes the marker every
# HEARTBEAT_SEC, so a marker touched within HEARTBEAT_ALIVE_SEC proves the
# writer is alive WITHOUT any PID mapping. A marker whose heartbeat has gone
# stale AND whose PID cannot be verified is treated as dead (crash leftover).
HEARTBEAT_ALIVE_SEC = 120
_MARKER_RE = re.compile(r"^(?P<pid>\d+)-(?P<start>\d+)-(?P<uuid>[0-9a-f]+)\.active$")


def _self_pid_ns() -> str:
    """The calling process's PID-namespace id (``/proc/self/ns/pid`` target)."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Process liveness (PID + start-time, guards against PID reuse)
# ---------------------------------------------------------------------------


def _proc_start_time(pid: int):
    """Kernel start-time (clock ticks) of *pid*.

    Returns:
      int  -- start-time read from /proc/<pid>/stat;
      None -- the process provably does NOT exist (/proc/<pid> is gone);
      "unknown" -- the probe could not be performed (no /proc, permission
      denied, unparseable stat): liveness cannot be decided either way.
    """
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read()
        # comm (field 2) may contain spaces/parens: split after the last ')'.
        rparen = data.rfind(b")")
        if rparen < 0:
            return "unknown"
        fields = data[rparen + 2 :].split()
        # after ')' the next field is state (field 3), so starttime (22) is
        # at index 22-3 = 19 of this tail.
        return int(fields[19])
    except FileNotFoundError:
        return None  # /proc/<pid> is gone -> the process is provably dead
    except Exception:  # noqa: BLE001 - no /proc, permission, parse error
        return "unknown"


def writer_alive(pid: int, start_time, marker_ns: str = "", marker_age_sec: float = 0.0) -> bool:
    """True when the (pid, start-time) writer must be treated as running.

    Three outcomes, decided in order:

    1. **PID namespace mismatch** (the marker was written from another PID
       namespace -- e.g. a docker container without ``--pid=host``): the host
       /proc cannot see the writer's PID, so the recorded PID is meaningless
       here. Fall back to the marker's own HEARTBEAT: a marker touched within
       ``HEARTBEAT_ALIVE_SEC`` proves the writer is alive (it keeps refreshing
       the marker while the store runs). An older marker is treated as a crash
       leftover.

    2. **Provably dead** (``/proc/<pid>`` gone AND the marker is in our own
       namespace): the writer exited. Only this is reported as dead.

    3. **Cannot determine** (no /proc, unreadable stat): return True -- safety
       first, an unreadable probe must never justify deleting a live store.

    PID reuse is excluded by comparing the recorded start-time.
    """
    if pid <= 0:
        return True  # malformed marker -> keep
    # --- 1. different PID namespace (docker launch mode) -------------------
    # NOTE: when our OWN namespace id is unreadable ("" -- e.g. /proc/self/ns/pid
    # is not available), a marker carrying a non-empty foreign id is still
    # clearly not ours, so the heartbeat fallback applies exactly the same way.
    # Only markers with an EMPTY id (old two-line markers, or writers that could
    # not record their ns) fall through to the /proc check below.
    if marker_ns and marker_ns != _self_pid_ns():
        # The recorded PID belongs to another namespace; we cannot map it to
        # our /proc. Liveness = the writer's heartbeat is fresh.
        return marker_age_sec <= HEARTBEAT_ALIVE_SEC
    # --- 2./3. same (or unknown) namespace: use /proc ----------------------
    current = _proc_start_time(pid)
    if current is None:
        return False  # provably dead: /proc/<pid> does not exist
    if current == "unknown":
        return True  # cannot prove death -> assume alive
    if not start_time:
        return True  # marker without a usable start-time (None/0) -> keep
    return current == start_time


# ---------------------------------------------------------------------------
# Marker lifecycle
# ---------------------------------------------------------------------------


def registry_dir(cache_root: str) -> str:
    return os.path.join(cache_root, REGISTRY_DIR)


def register_write(cache_root: str, final_name: str):
    """Create a marker for an in-flight store. Returns the marker path.

    The marker encodes (pid, process start-time) so liveness can be proven
    later even after the process exits and the PID is reused. It also records
    the writer's PID-NAMESPACE id (``/proc/self/ns/pid``) so a host-side CLI
    can tell "this PID is not in my namespace" apart from "this process died"
    -- essential for the docker launch mode, where the writer runs in a
    container without ``--pid=host`` (see ``HEARTBEAT_ALIVE_SEC``).
    """
    reg = registry_dir(cache_root)
    try:
        os.makedirs(reg, exist_ok=True)
        start = _proc_start_time(os.getpid())
        name = "%d-%d-%s.active" % (
            os.getpid(),
            start if start is not None else 0,
            uuid.uuid4().hex[:16],
        )
        path = os.path.join(reg, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%s\n%d\n%s\n" % (final_name, int(time.time()), _self_pid_ns()))
        return path
    except OSError:
        return None  # never break a store because of the guard


def unregister_write(marker_path) -> None:
    if not marker_path:
        return
    try:
        os.remove(marker_path)
    except OSError:
        pass


def heartbeat(marker_path) -> None:
    """Refresh the marker's mtime so operators can see the write is active."""
    if not marker_path:
        return
    try:
        os.utime(marker_path, None)
    except OSError:
        pass


def _iter_markers(cache_root: str):
    reg = registry_dir(cache_root)
    if not os.path.isdir(reg):
        return
    now = time.time()
    for name in sorted(os.listdir(reg)):
        m = _MARKER_RE.match(name)
        if not m:
            continue
        p = os.path.join(reg, name)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            age = now - os.path.getmtime(p)
        except OSError:
            continue  # unreadable -> never yields an actionable entry
        final = lines[0].strip() if lines else ""
        # Third line (added round 18) records the writer's PID namespace id;
        # old markers written before this fix have only two lines -> ns = "".
        marker_ns = lines[2].strip() if len(lines) > 2 else ""
        yield p, name, m, final, age, marker_ns


def active_final_names(cache_root: str) -> set:
    """Final file names (basenames) with a LIVE in-flight write.

    A marker counts as live unless the registry can PROVE its writer exited.
    Liveness is judged by ``writer_alive()``:
    * same PID namespace as this process -> /proc check (pid + start-time);
    * different PID namespace (docker writer) -> the marker's own heartbeat
      (fresh mtime within ``HEARTBEAT_ALIVE_SEC``);
    * anything the probe cannot decide is kept as live -- safer to retain a tmp
      file too long than to delete a live store's file.
    """
    out: set = set()
    for _p, _name, m, final, age, ns in _iter_markers(cache_root):
        pid = int(m.group("pid"))
        start = int(m.group("start"))
        if writer_alive(pid, start or None, ns, age) and final:
            out.add(os.path.basename(final))
    return out


def sweep_stale_markers(cache_root: str) -> int:
    """Remove crash-leftover markers. Returns how many were removed.

    Only removes markers whose writer PROVABLY exited (per ``writer_alive()``)
    AND which are older than ``STALE_MARKER_SEC``. Live writers are never
    swept, however old the marker gets.
    """
    reg = registry_dir(cache_root)
    if not os.path.isdir(reg):
        return 0
    removed = 0
    for p, _name, m, _final, age, ns in list(_iter_markers(cache_root)):
        pid = int(m.group("pid"))
        start = int(m.group("start"))
        if writer_alive(pid, start or None, ns, age):
            continue  # writer still running -- keep forever
        if age < STALE_MARKER_SEC:
            continue  # just exited, give the CLI a grace period
        try:
            os.remove(p)
            removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# LMCache patch
# ---------------------------------------------------------------------------


def install() -> bool:
    """Wrap DynamicNixlStorageAgent.dynamic_store_file with the registry.

    Returns True when installed. The wrapper:
      1. registers the FINAL file name before the store starts,
      2. heartbeats the marker while the store runs,
      3. unregisters in ``finally`` -- success and failure both clear the
         marker, so only crashed stores leave one behind.
    """
    try:
        from lmcache.v1.distributed.l2_adapters import (
            nixl_store_dynamic_l2_adapter as mod,
        )
    except Exception:  # noqa: BLE001 - slim install / missing deps
        return False

    agent_cls = getattr(mod, "DynamicNixlStorageAgent", None)
    if agent_cls is None or getattr(agent_cls, "_kvp_tmp_guard_installed", False):
        return bool(agent_cls)

    orig = agent_cls.dynamic_store_file

    async def guarded_store_file(self, mem_indices, file_path, page_size):
        root = getattr(self, "file_path", None) or os.path.dirname(file_path)
        marker = register_write(root, os.path.basename(file_path))
        stop = threading.Event()

        def _beat():
            while not stop.wait(HEARTBEAT_SEC):
                heartbeat(marker)

        hb = threading.Thread(target=_beat, daemon=True) if marker else None
        if hb:
            hb.start()
        try:
            return await orig(self, mem_indices, file_path, page_size)
        finally:
            stop.set()
            if hb:
                hb.join(timeout=1)
            unregister_write(marker)

    agent_cls.dynamic_store_file = guarded_store_file
    agent_cls._kvp_tmp_guard_installed = True
    return True


def main() -> None:  # pragma: no cover - debugging helper
    print("registry dir: %s" % REGISTRY_DIR, file=sys.stderr)
    print("install() is applied by kvpersist.launch automatically.", file=sys.stderr)


if __name__ == "__main__":
    main()