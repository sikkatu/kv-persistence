"""Round 56g: start-all/stop-all + recipe codec defaults (user: one command)."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
# the recipes repo lives as a sibling checkout (~/sikkatu-recipes) on real hosts
RECIPES = (Path.home() / "sikkatu-recipes" / "recipes" / "rtx-pro-6000"
           / "mimo-v2.6-pro-hotsplit" / "scripts")
if not RECIPES.exists():
    RECIPES = (Path("/home/review/recipes/recipes/rtx-pro-6000"
                    "/mimo-v2.6-pro-hotsplit/scripts"))


def _read(path: Path) -> str:
    return path.read_text()


# ---------------------------------------------------------------- start-all / stop-all
def test_start_all_exists_and_is_ordered():
    t = _read(SCRIPTS / "start-all.sh")
    obs = t.index("start-observability.sh")
    cache = t.index("start-cache.sh")
    engine = t.index("run-vllm-with-persist.sh")
    assert obs < cache < engine, (
        "start-all: observability -> cache -> engine (the cache auto-attaches)")


def test_stop_all_exists():
    t = _read(SCRIPTS / "stop-all.sh")
    assert "stop-engine.sh" in t
    assert "stop-cache.sh" in t
    # engine first, then the cache
    assert t.index("stop-engine.sh") < t.index("stop-cache.sh")


def test_start_all_no_flags_needed():
    t = _read(SCRIPTS / "start-all.sh")
    assert "export " not in t, "start-all must need no exports"
    assert "KVP_SAME_UID" in t, "same-UID is the default, but keep the override"


# ---------------------------------------------------------------- recipe codec defaults
@pytest.mark.skipif(not RECIPES.exists(), reason="sikkatu-recipes checkout not found")
def test_recipe_launchers_default_to_zstd():
    for name in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        t = _read(RECIPES / name)
        assert 'LMCACHE_L2_SERDE="${LMCACHE_L2_SERDE:-zstd}"' in t, (
            "%s: lossless zstd must be the default codec" % name)


@pytest.mark.skipif(not RECIPES.exists(), reason="sikkatu-recipes checkout not found")
def test_recipe_launchers_share_the_api_key():
    for name in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        t = _read(RECIPES / name)
        assert ".config/kvp/api_key" in t, (
            "%s: must use the canonical shared API key file" % name)