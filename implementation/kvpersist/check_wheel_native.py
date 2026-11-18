# SPDX-License-Identifier: Apache-2.0
"""Refuse a custom LMCache wheel that lacks the required native extensions.

r2 P0-2: the upstream build silently falls back to a pure-Python wheel when
no `nvcc` is found -- such a wheel runs but every transfer copies on the slow
Python fallback. r10 P0-2: without `lmcache_native` LMCache drops to
CLI-only mode (no compute backend at all), so BOTH extensions are now
required: `lmcache/cuda_ops*.so` AND `lmcache/lmcache_native*.so`.
Optional extensions (`lmcache_redis*.so`, `lmcache_fs*.so`) are collected for
provenance (`native_extensions`) but never required.

Called by scripts/_incontainer_build.sh after `pip wheel` (build aborts on a
miss) and by unit tests.
"""
from __future__ import annotations
# Standard
import argparse
import json
import sys
import zipfile

REQUIRED = ("lmcache/cuda_ops", "lmcache/lmcache_native")
OPTIONAL = ("lmcache/lmcache_redis", "lmcache/lmcache_fs")


def _member_of(zf: zipfile.ZipFile, prefix: str) -> str | None:
    """Return the wheel member name of `<prefix>*.so`, or None."""
    for name in zf.namelist():
        base = name.rsplit("/", 1)[-1]
        if name.startswith(prefix) and base.endswith(".so"):
            return name
    return None


def inspect_wheel(wheel_path: str) -> tuple[list[str], list[str], dict]:
    """Return (missing_required, found_optional, all_found)."""
    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())
        missing = [p for p in REQUIRED if _member_of(zf, p) is None]
        found_optional = [p for p in OPTIONAL if _member_of(zf, p) is not None]
        all_found = {p: _member_of(zf, p) for p in REQUIRED + OPTIONAL
                     if _member_of(zf, p) is not None}
    return missing, found_optional, all_found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wheel", help="path to the built lmcache .whl")
    args = ap.parse_args(argv)

    try:
        missing, optional, found = inspect_wheel(args.wheel)
    except (OSError, zipfile.BadZipFile) as e:
        print(f"NATIVE CHECK FAILED: cannot read wheel {args.wheel}: {e}",
              file=sys.stderr)
        return 2

    if missing:
        print("NATIVE CHECK FAILED: " +
              f"{args.wheel} is missing required native extension(s):", file=sys.stderr)
        for p in missing:
            print(f"  missing: {p}*.so", file=sys.stderr)
        print("  Without them LMCache falls back to the slow Python copy path "
              "(cuda_ops) or drops to CLI-only mode (lmcache_native). "
              "Rebuild with nvcc + CUDA dev packages -- scripts/build-lmcache.sh "
              "enforces this.", file=sys.stderr)
        return 1

    # machine-readable provenance fragment: everything found (required +
    # optional), printed as one JSON line the caller can grep/capture.
    print("native_extensions=" + json.dumps(found))
    print(f"native extension present: {found[REQUIRED[0]]}, {found[REQUIRED[1]]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())