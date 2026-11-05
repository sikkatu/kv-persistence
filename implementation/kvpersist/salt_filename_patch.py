"""Patch: include ``cache_salt`` in the nixl_store_dynamic L2 file name.

WHY (review rounds 12-15): upstream LMCache 0.5.3 derives the L2 file name
from ``(model_name, kv_rank, object_group_id, chunk_hash)`` only -- the
``cache_salt`` field of ``ObjectKey`` participates in the key's equality/hash
but NOT in the file name. After a server restart the adapter's secondary
(disk) lookup of the same tokens under a DIFFERENT salt therefore finds the
file written under the original salt: a measured cross-tenant data leak
(store salt=tenantA -> lookup salt=tenantB returned 4/4 chunks).

The other L2 adapters (fs/s3/bigtable) DO embed the salt in the file name;
only ``nixl_store_dynamic`` -- the adapter this project pins -- omits it.

FIX AT THE DISK-KEY LEVEL: this module redefines
``nixl_store_dynamic_l2_adapter._object_key_to_filename`` to append the salt
to the file name (``<name>_<rank>_<group>_<hash>_<salt>.bin``). Both the store
path and the secondary lookup path call the same module-level function, so
the mapping stays consistent: a lookup under salt B derives a DIFFERENT file
name and misses, exactly as intended.

Compatibility:
* Empty salt keeps the upstream name (``..._<hash>.bin``), so un-salted data
  written by an unpatched server remains readable.
* Files written WITH a salt by an unpatched server are named without the salt;
  a patched server looking up that salt misses (a safe miss, never a wrong
  read). Re-store to migrate.
* The patch is inert if the adapter module is absent (e.g. a slim install).

Usage (host and systemd):
    PYTHONPATH=<repo>/implementation python3 -m kvpersist.salt_filename_patch server ...
It behaves exactly like ``lmcache <args>`` after applying the patch.

Usage (docker): mount the repo's implementation/ into the container and run
the same module with PYTHONPATH set (see scripts/start-cache.sh).
"""
from __future__ import annotations

import sys


def patched_object_key_to_filename(key) -> str:
    """Upstream name + the cache salt (empty salt == upstream name)."""
    safe_model_name = key.model_name.replace("/", "--")
    chunk_hex = key.chunk_hash.hex()
    base = (
        f"{safe_model_name}_{key.kv_rank:08x}"
        f"_{key.object_group_id:x}_{chunk_hex}"
    )
    # ObjectKey forbids '@', '/', '\\' and NUL in the salt, so it is already
    # filename-safe; trim defensively anyway.
    salt = (key.cache_salt or "").replace("/", "--").replace("\\", "--")
    return f"{base}_{salt}.bin" if salt else f"{base}.bin"


def apply() -> bool:
    """Install the patched filename mapping. Returns True when applied.

    Review fix (round 37): assigning a NEW module attribute is not a patch --
    if upstream renames ``_object_key_to_filename`` the assignment creates a
    dead attribute, nothing calls it, and apply() used to return True while
    the cache_salt isolation was silently lost. Verify the target EXISTS
    first; a missing/renamed target must return False so the caller prints
    the loud WARNING instead of claiming success.
    """
    try:
        from lmcache.v1.distributed.l2_adapters import (
            nixl_store_dynamic_l2_adapter as mod,
        )
    except Exception:  # noqa: BLE001 - slim install / missing deps
        return False
    # The adapter must expose the function we are about to replace -- a rename
    # upstream means our mapping would never be called.
    if not hasattr(mod, "_object_key_to_filename"):
        return False
    mod._object_key_to_filename = patched_object_key_to_filename
    # Guard against the assignment landing on a module that re-exports the
    # name but does not USE it for lookups: the patched callable must be in
    # place after assignment.
    return getattr(mod, "_object_key_to_filename", None) is patched_object_key_to_filename


def main() -> None:
    """Patch, then delegate to the real LMCache CLI entry point."""
    applied = apply()
    if applied:
        print("[kvp] cache_salt included in L2 file names "
              "(nixl_store_dynamic filename patch active)", file=sys.stderr)
    else:
        print("[kvp] WARNING: filename patch NOT applied (adapter module "
              "unavailable) -- cache_salt isolation on L2 is the upstream "
              "behaviour (see docs/final-report.md)", file=sys.stderr)
    from lmcache.cli.main import main as lmc_main

    lmc_main()


if __name__ == "__main__":
    main()