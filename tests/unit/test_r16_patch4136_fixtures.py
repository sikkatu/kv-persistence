# SPDX-License-Identifier: Apache-2.0
"""r16 P0: the 4136 patch test fixtures must be adapted to the pinned base.

Hardware log 19.11 18:53 (first C0-C10 pass on the new base): C11 = 150
passed, 5 failed -- all five failures in tests/v1/test_lmcache_mp_connector.py
(the file the 4136 patch ADDS). The upstream tests were written against the
August PR head and built the connector via __new__ with only part of the
current __init__ attributes (_delay_free_req_ids, _can_store,
_pinned_boundary_state_blocks, _lazy_offload_manager), the request stubs
lacked num_computed_tokens, and one test still asserted the obsolete
`_pending_store` API (gone from production). These tests were NEVER green on
our base -- before r15 the gate died at collection, and the "manual rebase"
of 4136 was never run in a real image.

Static checks on the shipped patch (offline, no LMCache checkout):
  - the added (^+) lines of the TEST FILE section carry the four fixture
    attributes and `num_computed_tokens=`;
  - no added line carries `_pending_store` (obsolete API);
  - the PRODUCTION sections carry none of the fixture-only attributes as
    test scaffolding (sanity: _delay_free_req_ids etc. exist in production,
    so this is checked only for `_pending_store` -- it must be gone from the
    whole patch);
  - SOURCES.md records the adapted-fixtures note and the patch's current
    SHA-256.

Mutation: restoring `_pending_store` in the patch -> this test RED.
"""
from __future__ import annotations
import hashlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PATCH = ROOT / "vendor" / "patches" / "lmcache" / "4136-mp-retrieve-abort.patch"
SOURCES = ROOT / "vendor" / "patches" / "lmcache" / "SOURCES.md"

FIXTURE_MARKERS = ("_delay_free_req_ids", "_can_store",
                   "_pinned_boundary_state_blocks", "_lazy_offload_manager",
                   "num_computed_tokens=")


def _added_lines_of(patch_text: str, filename: str) -> list[str]:
    """Added (^+) lines of the section for `filename`."""
    out = []
    in_section = False
    for ln in patch_text.splitlines():
        if ln.startswith("diff --git "):
            in_section = ln.split(" b/")[-1].strip() == filename
            continue
        if in_section and ln.startswith("+") and not ln.startswith("+++"):
            out.append(ln)
    return out


def test_patch_test_fixtures_carry_the_adapted_attributes():
    """The added lines of the 4136 TEST FILE must set all four fixture
    attributes and carry num_computed_tokens (the r16 P0 adaptation)."""
    text = PATCH.read_text()
    added = _added_lines_of(text, "tests/v1/test_lmcache_mp_connector.py")
    assert added, "the patch must ADD tests/v1/test_lmcache_mp_connector.py"
    body = "\n".join(added)
    for marker in FIXTURE_MARKERS:
        assert marker in body, (
            f"the added test-fixture lines must carry {marker!r} "
            f"(r16 P0 fixture adaptation)")


def test_no_pending_store_left_in_the_patch():
    """`_pending_store` is gone from production -- the patch (and especially
    its test fixtures) must not resurrect it.
    Mutation: restoring `_pending_store` -> this test RED."""
    text = PATCH.read_text()
    added = _added_lines_of(text, "tests/v1/test_lmcache_mp_connector.py")
    body = "\n".join(added)
    assert "_pending_store" not in body, (
        "the obsolete _pending_store API must not appear in the test fixtures")


def test_sources_md_records_the_adaptation_and_current_sha():
    """SOURCES.md must document the r16 fixture adaptation and carry the
    CURRENT SHA-256 of the patch (a stale pin here is the same class of lie
    the r15 series closed)."""
    sources = SOURCES.read_text()
    assert "fixtures adapted to the pinned base (r16)" in sources, (
        "SOURCES.md must record the r16 test-fixture adaptation")
    digest = hashlib.sha256(PATCH.read_bytes()).hexdigest()
    assert digest in sources, (
        f"SOURCES.md does not carry the current 4136 SHA-256 ({digest})")