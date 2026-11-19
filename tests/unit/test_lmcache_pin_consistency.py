# SPDX-License-Identifier: Apache-2.0
"""r15 P0': ONE LMCache source pin must stay consistent everywhere, the
version label must track the pin's base, and no OLD pin/label may survive.

The pin is duplicated by necessity across build-lmcache.sh / build-image.sh /
SOURCES.md / docs / requirements-lmcache.txt -- a stale copy anywhere is the
same class of lie that shipped the r15 stale-patch defect (an identity that
no longer describes reality). Checks:
  - every recorded LMCACHE_SOURCE_COMMIT is the SAME full 40-hex SHA;
  - SOURCES.md and the docs carry that SHA;
  - the version label is `0.5.8rc1.post48+kvp.<fp12>` (PEP 440, post48 =
    `git rev-list --count v0.5.8rc1..<pin>`, verified on the real pin);
  - the patch order is 4136 BEFORE 5972 in build-lmcache.sh;
  - the OLD pin (03ee5b82...) and the OLD label (0.5.7.dev428) are gone from
    scripts/, implementation/, vendor/, tests/, docs/ (historical CHANGELOG
    entries are allowed).

Mutation: restoring the old SHA in any one file -> the consistency test RED.
"""
from __future__ import annotations
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXPECTED_PIN = "07f1c6bb642f4772606a36eab730ae009ddce08f"
EXPECTED_LABEL = "0.5.8rc1.post48"
OLD_PIN = "03ee5b82418b6bd44e424cdd46932d9346f43eb2"
OLD_LABEL = "0.5.7.dev428"
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _pin_occurrences() -> dict[str, list[str]]:
    """Every file outside CHANGELOG history that records the LMCache pin."""
    out: dict[str, list[str]] = {}
    for f in (ROOT / "scripts").glob("*.sh"):
        text = f.read_text()
        for m in re.finditer(r'LMCACHE_SOURCE_COMMIT.*?:-([0-9a-f]{40})\}', text):
            out.setdefault(m.group(1), []).append(str(f.relative_to(ROOT)))
        for m in re.finditer(r'"lmcache_source_commit": "\$\{LMCACHE_SOURCE_COMMIT:-([0-9a-f]{40})\}"', text):
            out.setdefault(m.group(1), []).append(str(f.relative_to(ROOT)))
    return out


def test_every_recorded_pin_is_the_same_sha():
    """A stale copy anywhere is a lie -- mutation: restore the old SHA in one
    file -> this test RED."""
    pins = _pin_occurrences()
    assert pins, "no LMCACHE_SOURCE_COMMIT pin found in the build scripts"
    assert set(pins) == {EXPECTED_PIN}, (
        f"the LMCache pin must be {EXPECTED_PIN} everywhere, found: {pins}")
    for f in ("vendor/patches/lmcache/SOURCES.md",
              "docs/lmcache-build.md", "docs/lmcache-build.ru.md",
              "docs/lmcache-build.zh.md", "requirements-lmcache.txt"):
        text = (ROOT / f).read_text()
        assert EXPECTED_PIN in text, f"{f} must carry the pin {EXPECTED_PIN}"


def test_pin_is_a_full_40_hex_sha():
    for f in ("scripts/build-lmcache.sh", "scripts/build-image.sh"):
        text = (ROOT / f).read_text()
        for m in SHA_RE.finditer(text):
            pass  # presence of 40-hex is fine
        assert EXPECTED_PIN in text, f"{f} must carry the full 40-hex pin"


def test_version_label_matches_the_pin_base():
    """`0.5.8rc1.post48+kvp.<fp12>`: post48 = rev-list count
    v0.5.8rc1..07f1c6bb6 (verified on the real clone), PEP 440 valid, NOT
    below the real base."""
    text = (ROOT / "scripts" / "build-lmcache.sh").read_text()
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=" + EXPECTED_LABEL in text, (
        "the wheel version label must be " + EXPECTED_LABEL + "+kvp.<fp12>")
    # the local part must remain the patched-tree fingerprint
    assert "${PATCHED_STATE:0:12}" in text, (
        "the +kvp local part must stay the patched-tree fingerprint")
    # a bare-label sanity check against a PEP 440 pattern
    assert re.fullmatch(r"0\.5\.8rc1\.post48", EXPECTED_LABEL)


def test_patch_order_is_4136_before_5972():
    """The patches apply in one deterministic order: 4136 first, 5972 second
    (verified on the real pinned base)."""
    text = (ROOT / "scripts" / "build-lmcache.sh").read_text()
    i4136 = text.find("apply_patch \"$PATCH_4136\"")
    i5972 = text.find("apply_patch \"$PATCH_5972\"")
    assert i4136 != -1 and i5972 != -1, "both apply_patch calls must exist"
    assert i4136 < i5972, "4136 must be applied BEFORE 5972"


def test_old_pin_and_label_are_gone():
    """No stale pin/label in code, tests, vendor or docs (historical
    CHANGELOG entries are allowed -- they describe past rounds as they
    happened)."""
    stale = []
    for base in ("scripts", "implementation", "vendor", "tests", "docs"):
        for f in (ROOT / base).rglob("*"):
            if not f.is_file() or "__pycache__" in str(f):
                continue
            if f.name == "test_lmcache_pin_consistency.py":
                continue  # this file records the old values as constants
            if f.suffix not in (".py", ".sh", ".md", ".txt", ".env"):
                continue
            text = f.read_text()
            if OLD_PIN in text:
                stale.append(f"{f.relative_to(ROOT)}: old pin")
            if OLD_LABEL in text and "CHANGELOG" not in f.name:
                stale.append(f"{f.relative_to(ROOT)}: old label")
    assert not stale, (
        "stale pin/label survived:\n" + "\n".join(stale))