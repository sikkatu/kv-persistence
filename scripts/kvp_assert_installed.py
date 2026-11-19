# SPDX-License-Identifier: Apache-2.0
"""pytest plugin (r11 C11): assert that `lmcache` comes from the INSTALLED
distribution location, never from a source tree.

r14 P0: the old check looked for the substring `site-packages` inside the
__file__ path --
but the vLLM image is Debian/Ubuntu layout and pip installs into
`/usr/local/lib/python3.12/dist-packages`, so a correctly installed wheel was
REFUSED (hardware log 19.11 12:52, C8 false refusal). The criterion is now by
substance: every module file must live UNDER the root that
importlib.metadata reports as the installed `lmcache` distribution location,
and that root must NOT live inside a source tree ($LMC_SRC / cwd).

The build gates its five-file regression run on this plugin: at
pytest_sessionstart it imports lmcache, lmcache.cuda_ops and
lmcache.lmcache_native, runs assert_installed() on them (so the tests
exercise the INSTALLED wheel, never the source tree) and prints them. Any
failure aborts the session with exit code 4 and the message
"BUILD REFUSED ...".

Loaded via `python3 -P -m pytest -p kvp_assert_installed` with PYTHONPATH
pointing at the scripts dir that holds this file. The same assert_installed()
is imported by the C8 heredoc in _incontainer_build.sh
(PYTHONPATH=$KVP_SCRIPTS) -- it must stay importable WITHOUT pytest, because
C8 runs before the test-deps install.
"""
from __future__ import annotations
import sys
from pathlib import Path


def assert_installed(mods, source_roots=None) -> None:
    """Raise RuntimeError when any of `mods` is NOT loaded from the installed
    `lmcache` distribution location, or the distribution root sits inside a
    source tree. The refusal message carries BOTH paths (file, dist root).

    Args:
        mods: module objects (lmcache, lmcache.cuda_ops,
            lmcache.lmcache_native).
        source_roots: directories that must NOT contain the distribution root
            (default: [cwd]); protects against editable/source installs.
    """
    import importlib.metadata as md

    try:
        dist_root = Path(md.distribution("lmcache").locate_file("")).resolve()
    except Exception as e:
        raise RuntimeError(
            "BUILD REFUSED: cannot locate the installed lmcache distribution "
            f"({e!r}) -- is the wheel installed?") from e

    roots = [Path(r).resolve() for r in
             (source_roots if source_roots is not None else [Path.cwd()])]
    for src_root in roots:
        if dist_root == src_root or src_root in dist_root.parents:
            raise RuntimeError(
                f"BUILD REFUSED: the lmcache distribution root {dist_root} "
                f"lives inside the source tree {src_root} -- the tests would "
                "silently run against the SOURCE tree, not the installed "
                "wheel")

    for mod in mods:
        f = getattr(mod, "__file__", "") or ""
        name = getattr(mod, "__name__", str(mod))
        if not f:
            raise RuntimeError(
                f"BUILD REFUSED: {name} has no __file__ -- the installed "
                f"distribution location is {dist_root}")
        fp = Path(f).resolve()
        if fp != dist_root and dist_root not in fp.parents:
            raise RuntimeError(
                f"BUILD REFUSED: {name} imported from {fp}, not under the "
                f"installed distribution location {dist_root} -- the tests "
                "would silently run against the SOURCE tree, not the "
                "installed wheel")


def pytest_sessionstart(session):  # noqa: ARG001 - pytest hook signature
    import os
    import pytest
    try:
        import lmcache
        import lmcache.cuda_ops  # noqa: F401
        import lmcache.lmcache_native  # noqa: F401
    except Exception as e:
        pytest.exit(f"BUILD REFUSED: lmcache native import failed: {e!r}", 4)
    mods = (
        lmcache,
        sys.modules["lmcache.cuda_ops"],
        sys.modules["lmcache.lmcache_native"],
    )
    # source tree = $KVP_LMC_SRC (the checkout) or cwd as fallback -- the
    # distribution root must never live inside it (editable/source install).
    src_root = os.environ.get("KVP_LMC_SRC") or os.getcwd()
    try:
        assert_installed(mods, source_roots=[src_root])
    except RuntimeError as e:
        pytest.exit(str(e), 4)
    for mod in mods:
        print(f"[kvp_assert_installed] {mod.__name__}.__file__ = {mod.__file__}")