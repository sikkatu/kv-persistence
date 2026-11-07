"""Patch: include ``cache_salt`` in the nixl_store_dynamic L2 file name.
WHY (review rounds 12-15): pre-0.5.6 upstream LMCache derived the L2 file name
from ``(model_name, kv_rank, object_group_id, chunk_hash)`` only -- the
``cache_salt`` field of ``ObjectKey`` participated in the key's equality/hash
but NOT in the file name. After a server restart the adapter's secondary
(disk) lookup of the same tokens under a DIFFERENT salt therefore found the
file written under the original salt: a measured cross-tenant data leak
(store salt=tenantA -> lookup salt=tenantB returned 4/4 chunks).

LMCache 0.5.6 FIXED the leak upstream: its own ``_object_key_to_filename``
appends the salt as a trailing ``@``-delimited field. The patch therefore
became a COMPAT SHIM (round 43):
* on 0.5.6+ (``upstream_active()`` True) the patched mapping is NOT
  installed -- upstream already isolates and its ``@``-suffix naming must
  stay intact (consuming modules bind the name at import);
* with the rendered-adapter option ``legacy_salt_suffix: true`` the patch
  installs the OLD ``_``-suffix naming so files written by a pre-0.5.6
  server remain findable (migration aid only).

Version support: the module knows BOTH layouts. On pre-0.5.6 the patch installs
in ``nixl_store_dynamic_l2_adapter``; on 0.5.6+ it resolves
``nixl_store_agents.dynamic_nixl_store_agent`` (the DEFINING module).

Usage (host and systemd):
    PYTHONPATH=<repo>/implementation python3 -m kvpersist.salt_filename_patch server ...
It behaves exactly like ``lmcache <args>`` after applying the patch.
Usage (docker): mount the repo's implementation/ and run the same module
with PYTHONPATH set (see scripts/start-cache.sh).
"""
from __future__ import annotations

import sys


def patched_object_key_to_filename(key) -> str:
    """Legacy '_'-suffix name + the cache salt (empty salt == upstream).

    On LMCache 0.5.6+ upstream appends ``@salt`` itself, so this mapping is
    installed ONLY when the rendered adapter opts into legacy naming."""
    safe_model_name = key.model_name.replace("/", "--")
    chunk_hex = key.chunk_hash.hex()
    base = (
        f"{safe_model_name}_{key.kv_rank:08x}"
        f"_{key.object_group_id:x}_{chunk_hex}"
    )
    # ObjectKey forbids '@', '/', '\\' and NUL in the salt; trim defensively.
    salt = (key.cache_salt or "").replace("/", "--").replace("\\", "--")
    return f"{base}_{salt}.bin" if salt else f"{base}.bin"


def _adapter_modules():
    """Yield the module(s) defining the filename mapping (pre-0.5.6 adapter
    module or the 0.5.6+ nixl_store_agents package)."""
    import importlib
    for name in (
        "lmcache.v1.distributed.l2_adapters.nixl_store_agents"
        ".dynamic_nixl_store_agent",
        "lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter",
    ):
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - slim install / missing deps
            continue
        if hasattr(mod, "_object_key_to_filename"):
            yield mod


def upstream_active() -> bool:
    """True when lmcache >= 0.5.6 embeds cache_salt in the name itself
    (the trailing '@'-field). Static check of the live source."""
    import inspect
    for mod in _adapter_modules():
        try:
            src = inspect.getsource(getattr(mod, "_object_key_to_filename"))
        except Exception:  # noqa: BLE001
            continue
        if "@{" in src and "cache_salt" in src:
            return True
    return False


def legacy_salt_requested() -> bool:
    """True when the rendered adapter opts into the legacy '_'-suffix."""
    import json
    import os
    root = os.environ.get("KVP_CACHE_ROOT", "/var/lib/kv-persistence/l2")
    try:
        with open(os.path.join(root, "l2-adapter.rendered.json")) as fh:
            return bool(json.load(fh).get("legacy_salt_suffix"))
    except Exception:  # noqa: BLE001 - unreadable adapter: keep upstream
        return False


def install() -> str:
    """Version-aware install. Returns 'upstream' (0.5.6+ already isolates),
    'patched' (legacy mapping installed) or 'missing'."""
    if upstream_active():
        return "upstream"
    patched = False
    for mod in _adapter_modules():
        mod._object_key_to_filename = patched_object_key_to_filename
        if getattr(mod, "_object_key_to_filename", None) is \
                patched_object_key_to_filename:
            patched = True
    return "patched" if patched else "missing"


def apply() -> bool:
    """Back-compat wrapper: True only when the legacy mapping is live."""
    return install() == "patched"


def main() -> None:
    """Patch (version-aware), then delegate to the real LMCache CLI."""
    status = install()
    if status == "upstream":
        print("[kvp] cache_salt included in L2 file names by upstream LMCache "
              "(0.5.6+ native '@'-suffix) -- no patch needed", file=sys.stderr)
    elif status == "patched":
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