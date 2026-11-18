# SPDX-License-Identifier: Apache-2.0
"""pytest plugin (r11 C11): assert that `lmcache` comes from site-packages.

The build gates its five-file regression run on this plugin: at
pytest_sessionstart it imports lmcache, lmcache.cuda_ops and
lmcache.lmcache_native, requires site-packages in the three __file__ paths
(so the tests exercise the INSTALLED wheel, never the source tree) and
prints them. Any failure aborts the session with exit code 4 and the
message "BUILD REFUSED ...".

Loaded via `python3 -P -m pytest -p kvp_assert_installed` with PYTHONPATH
pointing at the scripts dir that holds this file.
"""
from __future__ import annotations

import sys

import pytest


def pytest_sessionstart(session):  # noqa: ARG001 - pytest hook signature
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
    for mod in mods:
        f = getattr(mod, "__file__", "") or ""
        if "site-packages" not in f:
            pytest.exit(
                f"BUILD REFUSED: {mod.__name__} imported from {f} -- the tests "
                "would silently run against the SOURCE tree, not the installed "
                "wheel", 4)
    for mod in mods:
        print(f"[kvp_assert_installed] {mod.__name__}.__file__ = {mod.__file__}")