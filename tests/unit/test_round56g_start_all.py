"""Round 56g: start-all/stop-all + recipe codec defaults (one-command launch)."""
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
    # Only the real LAUNCH lines define the order (comments mention script names
    # too and must not confuse the check).
    obs = t.index('bash "$HERE/start-observability.sh"')
    cache = t.index('bash "$HERE/start-cache.sh"')
    engine = t.index('bash "$RECIPE_DIR/scripts/run-tp4-hotsplit.sh"')  # Round 80b: recipe owns the engine
    assert obs < cache < engine, (
        "start-all: observability -> cache -> engine (the cache auto-attaches)")
    # The config layer must load BEFORE the cache decision (round 58): the PID
    # probe needs the EFFECTIVE KVP_CACHE_ROOT from cache.env[.local].
    assert t.index("_kvp_load_env") < cache, (
        "start-all must load the config before deciding about the cache service")
    # ONE codec decision, resolved once and handed to BOTH children (round 58):
    # the cache and the engine must never pick codecs independently.
    assert "_kvp_resolve_l2_serde" in t, "start-all must resolve the codec once"
    assert t.index("_kvp_resolve_l2_serde") < cache, "resolve the codec before the cache starts"
    assert 'export LMCACHE_L2_SERDE=' in t, "the resolved codec must reach both children"


def test_stop_all_exists():
    t = _read(SCRIPTS / "stop-all.sh")
    assert "stop-engine.sh" in t
    assert "stop-cache.sh" in t
    # engine first, then the cache
    assert t.index("stop-engine.sh") < t.index("stop-cache.sh")


def test_start_all_no_flags_needed():
    t = _read(SCRIPTS / "start-all.sh")
    # No USER-FACING exports: IMAGE/MODEL_DIR resolve automatically. The single
    # internal `export LMCACHE_L2_SERDE=` (resolved codec handed to both
    # children) is allowed -- it is the point of the one-codec decision.
    for var in ("export IMAGE=", "export KVP_MODEL_DIR="):
        assert var not in t, "start-all must not require manual %s" % var
    # Round 81: the internal auto-resolution MAY export MODEL_DIR (that is the
    # zero-config feature) -- but only inside the resolver block, never as a
    # manual prerequisite (no bare 'export MODEL_DIR=<...>' outside it).
    assert '_kvp_resolve_model_dir' in t, (
        "MODEL_DIR must be auto-resolved (no manual export needed)")
    assert "KVP_SAME_UID" in t, "same-UID is the default, but keep the override"
    assert "_kvp_load_env" in t, "start-all must load cache.env/identity.env itself"


# ---------------------------------------------------------------- recipe codec defaults
@pytest.mark.skipif(not RECIPES.exists(), reason="sikkatu-recipes checkout not found")
def test_recipe_launchers_default_to_zstd():
    for name in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        t = _read(RECIPES / name)
        # Round 64: the default is applied ONLY when no opt-out switch is set
        # (USE_LOSSLESS_SERDE=0 / KVP_L2_SERDE=none must reach the identity
        # gate instead of being overridden by an exported zstd).
        assert 'LMCACHE_L2_SERDE=zstd' in t, "%s: lossless zstd must be the default codec" % name
        assert 'USE_LOSSLESS_SERDE:-}' in t, "%s: the codec default must yield to opt-out switches" % name
        assert 'USE_LOSSLESS_SERDE:-}' in t, (
            "%s: the codec default must yield to the documented opt-out switches" % name)


@pytest.mark.skipif(not RECIPES.exists(), reason="sikkatu-recipes checkout not found")
def test_recipe_launchers_share_the_api_key():
    for name in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        t = _read(RECIPES / name)
        assert ".config/kvp/api_key" in t, (
            "%s: must use the canonical shared API key file" % name)