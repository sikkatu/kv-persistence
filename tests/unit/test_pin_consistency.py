# SPDX-License-Identifier: Apache-2.0
"""r6-7 B-3: ONE digest pin (vendor/pinned-base-image.env) must stay
consistent with the recipes checkout when one is available.

The pin was previously duplicated across a dozen places (README x3,
tp4.env.example x3, four recipes, the build-image.sh header). Now the pin
file is the single source; the scripts no longer hardcode the digest.
"""
from __future__ import annotations
import os
import pathlib
import re
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PIN_FILE = ROOT / "vendor" / "pinned-base-image.env"
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
BUILD_IMG = ROOT / "scripts" / "build-image.sh"

PIN_RE = re.compile(r'[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}')


def _pin_value() -> str:
    m = re.search(r'^KVP_BASE_IMAGE_DEFAULT="([^"]+)"', PIN_FILE.read_text(), re.M)
    assert m, "vendor/pinned-base-image.env must define KVP_BASE_IMAGE_DEFAULT"
    return m.group(1)


def test_pin_file_format():
    pin = _pin_value()
    assert PIN_RE.fullmatch(pin), (
        f"the pin must be repo:tag@sha256:<64 hex>, got {pin!r}"
    )


def test_build_scripts_do_not_hardcode_the_digest():
    """The digest lives ONLY in the pin file (the 'what not to do' rule)."""
    for script in (BUILD_LMC, BUILD_IMG):
        text = script.read_text()
        assert _pin_value() not in text, (
            f"{script.name} must not hardcode the pinned digest -- it reads "
            f"vendor/pinned-base-image.env"
        )
        assert "pinned-base-image" in text, (
            f"{script.name} must reference the pin file"
        )


def test_recipes_tp4_env_example_matches_pin():
    """When a recipes checkout is reachable, mimo-v2.6-pro-hotsplit's
    tp4.env.example must carry the SAME digest (other recipes untouched).
    Without a reachable checkout the test skips with an explicit reason."""
    candidates = []
    for base in (
        os.environ.get("KVP_RECIPES_DIR", ""),
        str(ROOT.parent / "recipes"),
        os.path.expanduser("~/kvp-work/recipes"),
        os.path.expanduser("~/recipes"),
    ):
        if base:
            cand = pathlib.Path(base) / "recipes" / "rtx-pro-6000" / \
                "mimo-v2.6-pro-hotsplit" / "tp4.env.example"
            candidates.append(cand)
    example = next((c for c in candidates if c.is_file()), None)
    if example is None:
        pytest.skip(
            "SKIP: no recipes checkout reachable "
            "(set KVP_RECIPES_DIR to run the cross-repo pin check)"
        )
    text = example.read_text()
    found = PIN_RE.findall(text)
    assert found, f"{example} must carry the pinned base image digest"
    for pin in found:
        assert pin == _pin_value(), (
            f"pin drift: {example} carries {pin} but "
            f"vendor/pinned-base-image.env says {_pin_value()}"
        )