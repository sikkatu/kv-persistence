"""Round 56: shared API key + hotsplit by default (user findings).

* two repos generated DIFFERENT API keys (kv-persistence: $ROOT/live/api_key,
  recipes: $PWD/live/api_key) -> ONE canonical file $HOME/.config/kvp/api_key
  (KVP_API_KEY_FILE overrides), legacy files migrated;
* the kv-persistence launcher did NOT start the recipe's dynamic hot/cold
  experts machinery (/w mounts + PYTHONPATH=/w + HOTSPLIT_* env) -- now the
  SAME recipe defaults run (HOTSPLIT_DYNAMIC=1), opt out with KVP_HOTSPLIT=0.
"""
from __future__ import annotations

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
        Path("/home/review/recipes/recipes/rtx-pro-6000"
             "/mimo-v2.6-pro-hotsplit/scripts/hotsplit_env.sh"),
    ]
    for c in candidates:
        if c.is_file():
            return c.read_text()
    raise AssertionError("shared hotsplit_env.sh not found next to the kv-persistence checkout")

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
    t = _read("run-vllm-with-persist.sh")
    assert "_kvp_load_api_key" in t, "the launcher must read the shared key"
    assert "_kvp_save_api_key" in t, "the launcher must save to the shared path"


# ---------------------------------------------------------------- hotsplit by default
def test_launcher_mounts_hotsplit_machinery():
    t = _read("run-vllm-with-persist.sh")
    for f in ("hotsplit.py", "hotsplit_plan.py", "numa_bind.py",
              "sitecustomize.py", "expert_hist_mix.json"):
        assert "/w/%s" % f in t, "the recipe's %s must be mounted at /w" % f
    assert 'KVP_HOTSPLIT:-1' in t, "hotsplit must be ON by default"
    # Round 59: PYTHONPATH=/w comes from the SHARED module (scripts/hotsplit_env.sh
    # in the recipe) that the launcher sources -- not a hand-written duplicate.
    assert "hotsplit_env.sh" in t, "the launcher must source the shared hotsplit env module"
    _mod = _read_module()
    assert 'PYTHONPATH=/w' in _mod, "the shared module must set PYTHONPATH=/w"


def test_launcher_passes_hotsplit_env_defaults():
    t = _read("run-vllm-with-persist.sh")
    # Round 59: the env defaults live in the SHARED module; the launcher sources
    # it and only sets its own profile default (HOT_GIB=30).
    assert "hotsplit_env.sh" in t, "the launcher must source the shared module"
    assert 'HOTSPLIT_HOT_GIB=${HOTSPLIT_HOT_GIB:-30}' in t or 'HOTSPLIT_HOT_GIB="${HOTSPLIT_HOT_GIB:-30}"' in t, (
        "the hot budget default must match the recipe (30 GiB)")
    _mod = _read_module()
    assert 'HOTSPLIT_DYNAMIC=${HOTSPLIT_DYNAMIC:-1}' in _mod, (
        "dynamic hot experts must be the default (recipe default)")
    assert "HOTSPLIT_ENV_ARGS" in t
    # the env must reach the docker run
    assert '${HOTSPLIT_ENV_ARGS[@]+' in t


def test_hotsplit_disabled_path_clears_env():
    t = _read("run-vllm-with-persist.sh")
    i_off = t.index("KVP_HOTSPLIT=0 (static placement only)")
    seg = t[i_off:i_off + 400]
    assert "HOTSPLIT_ENV_ARGS=()" in seg, (
        "KVP_HOTSPLIT=0 must clear the hotsplit env entirely")