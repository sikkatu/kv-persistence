"""Single launcher entry point applying ALL kv-persistence runtime patches.

This is the ONE module every launcher (scripts/start-cache.sh, systemd units,
docker sidecar) runs instead of `lmcache`:

    PYTHONPATH=<repo>/implementation python3 -m kvpersist.launch server ...

It applies, in order:

1. :mod:`kvpersist.salt_filename_patch` -- cache_salt embedded in the L2 file
   names (disk-level tenant isolation fix).
2. :mod:`kvpersist.compress_serde` -- registers the LOSSLESS compression
   serdes (kvp-lz4 / kvp-zstd / kvp-raw) and installs the L2 file-size patch
   so compressed frames are what actually lands on NVMe.

Both patches are idempotent and inert when the corresponding LMCache modules
are unavailable (each prints a loud warning instead of crashing). Everything
is opt-in: with no "serde" in the L2 adapter JSON the storage path is
byte-identical to the uncompressed upstream behaviour.

The real LMCache CLI runs last, with the original argv.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Apply every patch, then delegate to the lmcache CLI entry point."""
    from . import compress_serde, salt_filename_patch, tmp_guard

    # Round 43: version-aware install. On LMCache 0.5.6+ upstream already
    # embeds cache_salt in the L2 file name (the leak is fixed upstream) --
    # nothing to patch there. The legacy '_'-suffix mapping is installed
    # ONLY when the rendered adapter opts into it (compat for old files).
    if salt_filename_patch.upstream_active():
        _salt_status = "upstream"
    else:
        _salt_status = salt_filename_patch.install()
    if _salt_status == "upstream":
        print(
            "[kvp] cache_salt included in L2 file names by upstream LMCache "
            "(0.5.6+ native '@'-suffix) -- no patch needed",
            file=sys.stderr,
        )
    elif _salt_status == "patched":
        print(
            "[kvp] cache_salt included in L2 file names "
            "(nixl_store_dynamic filename patch active)",
            file=sys.stderr,
        )
    else:
        import json as _json
        import os as _os
        _root = _os.environ.get("KVP_CACHE_ROOT", "/var/lib/kv-persistence/l2")
        _adapter_path = _os.path.join(_root, "l2-adapter.rendered.json")
        _uses_salt = False
        try:
            with open(_adapter_path) as _fh:
                _uses_salt = bool(_json.load(_fh).get("cache_salt"))
        except Exception:
            pass
        if _uses_salt:
            print(
                "[kvp] FATAL: cache_salt is configured but the filename patch "
                "could NOT be applied (adapter module renamed upstream?) -- "
                "cross-tenant L2 isolation is BROKEN. Refusing to start. "
                "Update kvpersist.salt_filename_patch for this lmcache "
                "version (check docs/compatibility.md).",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(
            "[kvp] WARNING: filename patch NOT applied (adapter module "
            "unavailable) -- cache_salt isolation on L2 is the upstream "
            "behaviour (see docs/final-report.md)",
            file=sys.stderr,
        )
    # Active-write registry: `purge --tmp` never deletes a tmp file whose
    # store is still in flight (see kvpersist.tmp_guard). Inert without the
    # adapter module.
    if tmp_guard.install():
        print(
            "[kvp] active-write registry installed "
            "(purge skips in-flight *.tmp.* stores)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: active-write registry NOT installed (adapter "
            "module unavailable) -- purge --tmp falls back to the mtime "
            "stale window only",
            file=sys.stderr,
        )

    # Fail-fast: if the SELECTED lossless codec's Python bindings are missing,
    # abort here instead of reaching READY and failing later inside the serde
    # thread pool (log-only, invisible to the readiness check). Unused codecs
    # are never required -- a `LMCACHE_L2_SERDE=none` cache starts without
    # lz4/zstandard at all (review fix).
    try:
        reg_ok = compress_serde.register()
        required = compress_serde.resolve_required_codec()
        compress_serde.require_codec(required)
    except compress_serde.CodecError as e:
        print(f"[kvp] FATAL: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    if reg_ok:
        print(
            "[kvp] lossless compress serde registered "
            "(kvp-lz4 / kvp-zstd / kvp-raw); "
            + (f"required codec '{required}' is available; " if required else "no codec required (raw mode); ")
            + "the L2 file-size patch installs when a kvp-* serde is configured "
            "in the L2 adapter JSON",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: lossless serde NOT registered (LMCache serde "
            "factory unavailable) -- a 'kvp-lz4'/'kvp-zstd' adapter config "
            "will fail to load",
            file=sys.stderr,
        )

    try:
        from lmcache.cli.main import main as lmc_main
        lmc_main()
    except ModuleNotFoundError as exc:
        # Review fix (install report): the lmcache CLI imports optional client
        # packages (e.g. `openai`) while registering its subcommands -- a
        # missing one crashed `lmcache server` with a bare traceback. Say
        # exactly what to install instead.
        print(
            "[kvp] ERROR: the lmcache CLI cannot start -- missing python "
            "package %r.\n"
            "Install the pinned runtime into the SAME venv that provides the "
            "`lmcache` command:\n"
            "    ~/.venvs/lmcache/bin/pip install -r "
            "requirements-lmcache.txt\n"
            "(that file pins lmcache AND the packages its CLI imports at "
            "startup, e.g. openai.)" % (exc.name or str(exc)),
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()