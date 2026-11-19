# SPDX-License-Identifier: Apache-2.0
"""r15 P0: patch porting -- a patch taken from a PR head must be SEMANTICALLY
current for the pinned base, not merely applicable.

Hardware log 19.11 14:57: C11 died on
`ImportError: cannot import name 'IPCCacheServerKey' from
'lmcache.v1.multiprocess.custom_types'`. The 5972 patch came from the PR head
(Oct 27); upstream moved IPCCacheServerKey to lmcache.v1.common.custom_types
(c4fb19dd, AFTER the PR head). The patch applied cleanly (fuzz) but was
stale -- `SOURCES.md` said "applied to the pinned base commit", which is true
as text and wrong as meaning.

Static checks on the shipped patches + their recorded identities:
  - no added (`^+`) line carries the stale import path;
  - the fixed import path IS carried by 5972;
  - every Patch SHA-256 recorded in SOURCES.md matches the real file
    (the r15 P0.2 requirement: pins/sha256 must track the patch bytes).
Mutation: restoring the stale import in 5972 -> the stale-import test RED.
"""
from __future__ import annotations
import hashlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PATCH_DIR = ROOT / "vendor" / "patches" / "lmcache"
STALE = "lmcache.v1.multiprocess.custom_types import IPCCacheServerKey"
FIXED = "lmcache.v1.common.custom_types import IPCCacheServerKey"


def test_no_stale_ipccacheserverkey_import_in_patches():
    """No added (`^+`) line in either patch may carry the stale import path
    (mutation: restoring it in 5972 -> this test RED)."""
    hits = []
    for patch in sorted(PATCH_DIR.glob("*.patch")):
        for i, ln in enumerate(patch.read_text().splitlines(), 1):
            if ln.startswith("+") and STALE in ln:
                hits.append(f"{patch.name}:{i}: {ln.strip()}")
    assert not hits, (
        "stale import still added by a patch:\n" + "\n".join(hits))


def test_5972_carries_the_fixed_import():
    """The r15 P0 fix must be present: 5972 adds the import from
    lmcache.v1.common.custom_types (the location on the pinned base)."""
    patch = (PATCH_DIR / "5972-incomplete-kv-objects.patch").read_text()
    added = [ln for ln in patch.splitlines()
             if ln.startswith("+") and "IPCCacheServerKey" in ln and "import" in ln]
    assert added, "5972 must add an IPCCacheServerKey import line"
    for ln in added:
        assert FIXED in ln, (
            f"the added import must use the pinned-base location "
            f"({FIXED}), got: {ln.strip()}")


def test_sources_md_sha256_matches_patch_files():
    """SOURCES.md records a Patch SHA-256 per patch -- it must match the
    actual file bytes (a stale pin here is the same class of lie that let the
    r15 defect ship: an identity that no longer describes reality)."""
    sources = (PATCH_DIR / "SOURCES.md").read_text()
    for patch in sorted(PATCH_DIR.glob("*.patch")):
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        assert digest in sources, (
            f"SOURCES.md does not carry the current SHA-256 of "
            f"{patch.name} ({digest}) -- update the table")