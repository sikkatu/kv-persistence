"""Round 56: shared API key + hotsplit by default (user findings).

* two repos generated DIFFERENT API keys (kv-persistence: $ROOT/live/api_key,
  recipes: $PWD/live/api_key) -> ONE canonical file $HOME/.config/kvp/api_key
  (KVP_API_KEY_FILE overrides), legacy files migrated;
* the kv-persistence launcher did NOT start the recipe's dynamic hot/cold
  experts machinery (/w mounts + PYTHONPATH=/w + HOTSPLIT_* env) -- now the
  SAME recipe defaults run (HOTSPLIT_DYNAMIC=1), opt out with HOTSPLIT_DYNAMIC=0.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read_module() -> str:
    """Read the shared hotsplit env module from the recipe checkout."""
    # Round 60: resolve relative to THIS file first (works in CI where the
    # workflows clone sikkatu/recipes into ./recipes next to the kv-persistence
    # checkout), then fall back to the developer layouts.
    candidates = [
        Path(__file__).resolve().parents[2] / "recipes" / "recipes"
        / "rtx-pro-6000" / "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh",
        Path.home() / "sikkatu-recipes" / "recipes" / "rtx-pro-6000"
        / "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh",
        Path.home() / "recipes" / "recipes" / "rtx-pro-6000"
        / "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh",
        Path("/home/review/recipes/recipes/rtx-pro-6000"
             "/mimo-v2.6-pro-hotsplit/scripts/hotsplit_env.sh"),
    ]
    for c in candidates:
        if c.is_file():
            return c.read_text()
    import pytest
    pytest.skip("shared hotsplit_env.sh not found (recipe checkout not available)")

def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- shared API key
def test_env_has_shared_key_helpers():
    t = _read("env.sh")
    assert "_kvp_api_key_file(){" in t
    assert "_kvp_load_api_key(){" in t
    assert "_kvp_save_api_key(){" in t
    assert ".config/kvp/api_key" in t, "the canonical key path must be shared"


def test_launcher_uses_the_shared_key():
    t = _T.contract_text()
    # Round 71 (review): generation/save live in env.sh (_kvp_ensure_api_key);
    # the launcher calls the shared ensure step (same contract the delegated
    # recipe path runs).
    assert "_kvp_ensure_api_key" in t, "the launcher must call the shared ensure step"
    e = _read("env.sh")
    assert "_kvp_save_api_key" in e, "the shared library must save to the shared path"


def test_numa_map_validator_accepts_the_real_format():
    """Round 74 (hardware run blocker): the validator must accept the format
    numa_bind.py actually parses -- `gpu:node` pairs joined by commas
    ('0:0,1:0,2:1,3:1', the tp4.env.example documented value). The old regex
    demanded `node:cpu,cpu;...` and rejected the correct map, aborting the
    launch on a VM."""
    import re
    # Round 76: the validator lives in the SHARED module (both launchers run
    # _hotsplit_validate_config); the launcher only calls it.
    t = _read_module()
    assert "_hotsplit_validate_config" in t, (
        "the shared module must define _hotsplit_validate_config")
    # anchor to the NUMA_MAP grep itself (a bare NUMA_MAP:- prefix would
    # capture the float-knob regex from the ENVS array further up)
    block = re.search(r'"\$HOTSPLIT_NUMA_MAP" \| grep -Eq .([^\"\x27]+).', t)
    assert block, "the NUMA_MAP validator regex not found in the shared module"
    pat = re.compile(block.group(1))
    # accepted: the real format (one entry per GPU: gpu:node, comma-joined)
    for good in ("0:0", "0:0,1:0", "0:0,1:0,2:1,3:1", "3:1"):
        assert pat.match(good), f"the real NUMA_MAP format must pass: {good!r}"
    # rejected: garbage and the WRONG old format (node:cpu lists)
    for bad in ("", "0", "0:", ":1", "0:0,1:0,", "a:b", "0:0,1,2,3;1:4,5,6,7",
                "0:0 1:0", "0:0;1:0"):
        assert not pat.match(bad), f"malformed NUMA_MAP must NOT pass: {bad!r}"


def test_promotion_note_explains_auto_resolution():
    """Round 74 (hardware run): the promotion NOTE must NOT claim the raw
    `auto` value is used -- `auto` resolves to the model dtype before the
    namespace is hashed (the note said 'engine ... will all use
    KV_CACHE_DTYPE=auto', misleading the operator)."""
    t = _T.contract_text()
    # Round 80b: the promotion note lives in the recipe launcher's effective-
    # identity resolver; `auto` resolves to the model dtype via config.json.
    assert "resolves to the model" in t or "model's dtype" in t or "model dtype" in t, (
        "the promotion NOTE must explain that `auto` resolves to the model dtype")


# ---------------------------------------------------------------- hotsplit by default
def test_launcher_mounts_hotsplit_machinery():
    t = _T.contract_text()
    for f in ("hotsplit.py", "hotsplit_plan.py", "numa_bind.py",
              "sitecustomize.py", "expert_hist_mix.json"):
        assert "/w/%s" % f in t, "the recipe's %s must be mounted at /w" % f
    assert 'HOTSPLIT_DYNAMIC:-1' in t, "hotsplit must be ON by default"
    # Round 59: PYTHONPATH=/w comes from the SHARED module (scripts/hotsplit_env.sh
    # in the recipe) that the launcher sources -- not a hand-written duplicate.
    assert "hotsplit_env.sh" in t, "the launcher must source the shared hotsplit env module"
    _mod = _read_module()
    assert 'PYTHONPATH=/w' in _mod, "the shared module must set PYTHONPATH=/w"


def test_launcher_passes_hotsplit_env_defaults():
    t = _T.contract_text()
    # Round 59: the env defaults live in the SHARED module; the launcher sources
    # it. Round 71 (review): the hot-bank default moved to ONE shared place
    # (env.sh _kvp_hotsplit_hot_gib_default=30) so the kv path and the
    # delegated recipe path cannot drift.
    assert "_kvp_hotsplit_hot_gib_default" in t, (
        "the launcher must take the hot-bank default from the shared helper")
    e = _read("env.sh")
    assert 'HOTSPLIT_HOT_GIB:-30' in e, (
        "the shared helper must default to the recipe's 30 GiB")
    _mod = _read_module()
    assert 'HOTSPLIT_DYNAMIC=${HOTSPLIT_DYNAMIC:-1}' in _mod, (
        "dynamic hot experts must be the default (recipe default)")
    # the env must reach the docker run (round 80b: the shared module's array
    # is HOTSPLIT_ENVS in the recipe launcher)
    assert 'HOTSPLIT_ENVS' in t, "the hotsplit env must reach the docker run"
    assert '${HOTSPLIT_ENVS[@]' in t, "HOTSPLIT_ENVS must be passed to docker run"


def test_hotsplit_disabled_path_clears_env():
    """Round 80b: in the recipe path hotsplit is the recipe's IDENTITY (the
    patches are always mounted); the old KVP_HOTSPLIT=0 opt-out was the kv
    launcher's switch to drop the patches entirely. The recipe's contract is:
    HOTSPLIT_DYNAMIC=0 = static placement (the verified fallback), and the
    env array is built from the shared module's defaults."""
    _mod = _read_module()
    # static placement is reachable and documented
    assert "HOTSPLIT_DYNAMIC" in _mod, (
        "the shared module must expose HOTSPLIT_DYNAMIC (0 = static placement)")
    # the env array is defined exactly once in the shared module (no drift)
    assert "HOTSPLIT_ENVS=(" in _mod, (
        "the shared module must own the hotsplit env array")
    assert "HOTSPLIT_DYNAMIC=${HOTSPLIT_DYNAMIC:-1}" in _mod, (
        "dynamic hot experts must remain the default (recipe default)")