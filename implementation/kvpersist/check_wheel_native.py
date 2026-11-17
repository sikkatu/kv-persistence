# SPDX-License-Identifier: Apache-2.0
"""Refuse a custom LMCache wheel that lacks the native ``cuda_ops`` extension.

r2 P0-2: the upstream build (``setup_extensions/policy.py``) silently falls
back to a pure-Python wheel when no ``nvcc`` is found ("building without
extensions"). Such a wheel runs, but every transfer copies on the slow Python
fallback -- with a wrong torch minor it can even fail to import and fall back
completely silently. That exact defect already caused the ``cudaMemcpy
error 1`` incident downstream.

This module is the single source of truth for "does the wheel carry the
binary extension?" and is called by ``scripts/build-lmcache.sh`` after the
wheel is built (build aborts on a miss) and by unit tests.
"""

from __future__ import annotations

# Standard
import argparse
import sys
import zipfile

# Wheel-internal path pattern of the compiled module, e.g.
# lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so
NATIVE_MEMBER_PREFIX = "lmcache/cuda_ops"


def find_native_member(wheel_path: str) -> str | None:
    """Return the wheel member name of the cuda_ops binary, or None.

    Args:
        wheel_path: path to the built ``.whl`` file.

    Returns:
        The exact zip member name when the compiled extension is packed,
        otherwise ``None`` (never raises for a plain "missing" case).
    """
    with zipfile.ZipFile(wheel_path) as zf:
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if name.startswith(NATIVE_MEMBER_PREFIX) and base.endswith(".so"):
                return name
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wheel", help="path to the built lmcache .whl")
    args = ap.parse_args(argv)

    try:
        member = find_native_member(args.wheel)
    except (OSError, zipfile.BadZipFile) as e:
        print(f"NATIVE CHECK FAILED: cannot read wheel {args.wheel}: {e}", file=sys.stderr)
        return 2

    if member is None:
        print(
            "NATIVE CHECK FAILED: "
            f"{args.wheel} does NOT contain a compiled '{NATIVE_MEMBER_PREFIX}*.so'.\n"
            "  This wheel was built without CUDA extensions (likely no `nvcc` at "
            "build time;\n"
            "  LMCache prints 'building without extensions' in that case). Running it "
            "silently\n"
            "  falls back to the slow Python copy path. Rebuild with nvcc in PATH "
            "(BUILD_WITH_CUDA=1,\n"
            "  TORCH_CUDA_ARCH_LIST=12.0) -- scripts/build-lmcache.sh enforces this.",
            file=sys.stderr,
        )
        return 1

    print(f"native extension present: {member}")
    return 0


if __name__ == "__main__":
    sys.exit(main())