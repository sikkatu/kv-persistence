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
    from . import compress_serde, salt_filename_patch

    salt_applied = salt_filename_patch.apply()
    if salt_applied:
        print(
            "[kvp] cache_salt included in L2 file names "
            "(nixl_store_dynamic filename patch active)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: filename patch NOT applied (adapter module "
            "unavailable) -- cache_salt isolation on L2 is the upstream "
            "behaviour (see docs/final-report.md)",
            file=sys.stderr,
        )

    reg_ok = compress_serde.register()
    patch_ok = compress_serde._install_size_patch()
    if reg_ok:
        print(
            "[kvp] lossless compress serde registered "
            "(kvp-lz4 / kvp-zstd / kvp-raw)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: lossless serde NOT registered (LMCache serde "
            "factory unavailable) -- a 'kvp-lz4'/'kvp-zstd' L2 adapter config "
            "will fail to load",
            file=sys.stderr,
        )
    if patch_ok:
        print(
            "[kvp] L2 file-size patch active (compressed frames are written "
            "padded to the page size, not the full allocation)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: L2 file-size patch NOT applied (adapter module "
            "unavailable) -- compression may not reduce on-disk size",
            file=sys.stderr,
        )

    from lmcache.cli.main import main as lmc_main

    lmc_main()


if __name__ == "__main__":
    main()