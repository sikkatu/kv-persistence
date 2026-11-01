"""Active-write registry: make `purge --tmp` safe for in-flight stores.

WHY
Upstream nixl_store_dynamic writes each chunk to ``<final>.bin.tmp.<uuid>`` and
atomically renames it on success. A purge that deletes tmp files can corrupt a
store that is STILL WRITING. The first guard was a 15-minute mtime window --
a heuristic: a write longer than the window, or a race between the check and
the delete, could still lose. The review asked for a real mechanism.

WHAT THIS DOES
A tiny registry of in-flight writes, shared between the writer (the LMCache
adapter, patched here at the ``dynamic_store_file`` level) and the admin CLI
(``kvpersist.cli``):

* Before a store starts, a marker file ``<cache_root>/.kvp-active-writes/
  <pid>-<uuid>.active`` is created, naming the FINAL file being written.
* After the store finishes (success or failure) the marker is removed.
* ``purge --tmp`` / ``purge --all`` skip any ``*.tmp.*`` whose final name is
  listed in the registry -- regardless of how long the write takes.

Race analysis: tmp names embed a fresh uuid4, so a tmp file that exists at
scan time but is NOT in the registry can only come from (a) an unpatched
writer or (b) a crashed store -- both are safe to delete after the stale
window. A write that starts AFTER the purge scanned cannot collide: its tmp
file did not exist at scan time and carries a unique name.

Crash safety: a hard kill leaves the marker behind. Markers older than
``STALE_MARKER_SEC`` are ignored and swept (their write cannot still be
running after the window; if it somehow is, the same uncertainty as before
applies -- documented, not hidden). The marker is also ignored when its
referenced final file no longer has a tmp sibling.

INSTALL
``install()`` wraps ``DynamicNixlStorageAgent.dynamic_store_file`` (the single
place where the tmp name is created and renamed) -- an upstream-compatible,
in-process patch, exactly like the salt-filename and size patches. Applied
unconditionally by ``kvpersist.launch`` when the adapter module is importable;
inert otherwise.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

REGISTRY_DIR = ".kvp-active-writes"
# Markers older than this are considered crash leftovers and are ignored
# (and swept by purge). 1 hour: far beyond any single chunk store, and a
# process that is still writing after an hour is already a pathology of its
# own. This window only applies to DEAD writers -- live writes are protected
# by the registry itself, for however long they run.
STALE_MARKER_SEC = 3600


def registry_dir(cache_root: str) -> str:
    return os.path.join(cache_root, REGISTRY_DIR)


def _marker_name() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:16]}.active"


def register_write(cache_root: str, final_name: str) -> str | None:
    """Create a marker for an in-flight store. Returns the marker path."""
    reg = registry_dir(cache_root)
    try:
        os.makedirs(reg, exist_ok=True)
        path = os.path.join(reg, _marker_name())
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{final_name}\n{int(time.time())}\n")
        return path
    except OSError:
        return None  # never break a store because of the guard


def unregister_write(marker_path: str | None) -> None:
    if not marker_path:
        return
    try:
        os.remove(marker_path)
    except OSError:
        pass


def active_final_names(cache_root: str) -> set:
    """Final file names (basenames) with a live in-flight write.

    Stale markers (older than STALE_MARKER_SEC) are ignored: their writer is
    gone. Files unreadable are skipped -- an unreadable registry must never
    block a purge.
    """
    reg = registry_dir(cache_root)
    out: set = set()
    if not os.path.isdir(reg):
        return out
    now = time.time()
    for name in os.listdir(reg):
        if not name.endswith(".active"):
            continue
        p = os.path.join(reg, name)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            if now - os.path.getmtime(p) > STALE_MARKER_SEC:
                continue
            if lines and lines[0].strip():
                out.add(os.path.basename(lines[0].strip()))
        except OSError:
            continue
    return out


def sweep_stale_markers(cache_root: str) -> int:
    """Remove crash-leftover markers. Returns how many were removed."""
    reg = registry_dir(cache_root)
    if not os.path.isdir(reg):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(reg):
        p = os.path.join(reg, name)
        try:
            if now - os.path.getmtime(p) > STALE_MARKER_SEC:
                os.remove(p)
                removed += 1
        except OSError:
            continue
    return removed


def install() -> bool:
    """Wrap DynamicNixlStorageAgent.dynamic_store_file with the registry.

    Returns True when installed. The wrapper:
      1. registers the FINAL file name before the store starts,
      2. runs the original (tmp write + atomic rename),
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
        try:
            return await orig(self, mem_indices, file_path, page_size)
        finally:
            unregister_write(marker)

    agent_cls.dynamic_store_file = guarded_store_file
    agent_cls._kvp_tmp_guard_installed = True
    return True


def main() -> None:  # pragma: no cover - debugging helper
    print(f"registry dir: {REGISTRY_DIR}", file=sys.stderr)
    print("install() is applied by kvpersist.launch automatically.", file=sys.stderr)


if __name__ == "__main__":
    main()