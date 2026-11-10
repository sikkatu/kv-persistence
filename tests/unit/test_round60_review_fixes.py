"""Round 60 regression tests: CI module path, BOOT_HOOK equivalence, complete knob validation."""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = _T.recipe_launcher()  # Round 80b: recipe owns the engine launch
WF = ROOT / ".github" / "workflows" / "check.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")

def _read_module() -> str:
    """The shared hotsplit_env.sh (CI layout first, then developer paths) --
    the same resolution test_round56_key_hotsplit.py uses."""
    candidates = [
        ROOT.parent / "recipes" / "recipes" / "rtx-pro-6000" /
        "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh",
        ROOT / "recipes" / "recipes" / "rtx-pro-6000" /
        "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh",
        Path.home() / "sikkatu-recipes" / "recipes" / "rtx-pro-6000" /
        "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh",
    ]
    for c in candidates:
        if c.is_file():
            return c.read_text(encoding="utf-8")
    raise FileNotFoundError("hotsplit_env.sh not found in any known layout")


def test_ci_checks_out_recipes_for_the_shared_module():
    """Round 60 (review): the kv-persistence CI must clone sikkatu/recipes so
    tests can read the shared hotsplit_env.sh module -- without it two tests
    failed on every CI run (module looked up only in developer home paths)."""
    t = _read(WF)
    assert "repository: sikkatu/recipes" in t, "CI must check out sikkatu/recipes"
    assert "path: recipes" in t, "the recipes checkout must land in ./recipes"


def test_read_module_resolves_relative_to_the_checkout():
    """The test helper must find the module via the CI layout (repo root /
    recipes / recipes / ...) before falling back to developer paths."""
    t = _read(ROOT / "tests" / "unit" / "test_round56_key_hotsplit.py")
    assert 'parents[2] / "recipes" / "recipes"' in t, (
        "_read_module must resolve the CI checkout layout relative to the test file")
    # the CI-relative candidate must come FIRST (before developer home paths)
    i_rel = t.index('parents[2] / "recipes" / "recipes"')
    i_home = t.index('Path.home() / "sikkatu-recipes"')
    assert i_rel < i_home, "the CI layout must be tried before home-directory guesses"


def test_boot_hook_matches_the_recipe_decision():
    """Round 60 (review): with the model patches mounted (mimo_v2_hotsplit.py
    already calls apply_hotsplit) the boot hook must NOT be forced on --
    the recipe launcher sets BOOT_HOOK=0 in the same situation. Forcing
    HOTSPLIT_BOOT_HOOK=1 would install a SECOND wrapper on the same loader."""
    t = _read(LAUNCHER)
    assert 'HOTSPLIT_BOOT_HOOK' in t, (
        "the launcher must default HOTSPLIT_BOOT_HOOK to 0 with model patches mounted")
    assert "HOTSPLIT_BOOT_HOOK=1\n" not in t.replace('HOTSPLIT_BOOT_HOOK="${HOTSPLIT_BOOT_HOOK:-1}"', ""), (
        "HOTSPLIT_BOOT_HOOK must never be hard-forced to 1")
    # the health check must follow the configured value (INACTIVE is correct at 0)
    assert "HOOK STATUS boot_hook=INACTIVE" in t, (
        "the activation check must expect INACTIVE when the boot hook is off")


def test_every_numeric_knob_is_validated_on_the_host():
    """Round 60 (review): validation before the model load must cover every
    numeric knob the shared module passes through -- a bad value must fail on
    the host, not inside the container after a 13-16 minute load."""
    # Round 76: the checks live in the SHARED module (both launchers call
    # _hotsplit_validate_config) -- read it there, and assert the launcher
    # actually calls the shared function.
    module = _read(ROOT.parent / "recipes" / "recipes" / "rtx-pro-6000" /
                   "mimo-v2.6-pro-hotsplit" / "scripts" / "hotsplit_env.sh") \
        if (ROOT.parent / "recipes").is_dir() else _read_module()
    t = module
    assert "_hotsplit_validate_config" in t, (
        "the shared module must define _hotsplit_validate_config")
    launcher = _read(LAUNCHER)
    assert "_hotsplit_validate_config" in launcher, (
        "the launcher must call the shared validation (checks must not live in one path only)")
    # ALL validation loops (integer + float; round 61 splits float GiB knobs out)
    loops = re.findall(r"for _kv in ([^;]+?); do", t, re.S)
    assert loops, "the shared module must have knob validation loops"
    covered = set()
    for m in loops:
        covered.update(m.split())
    for knob in ("HOTSPLIT_DYN_STEPS", "HOTSPLIT_DYN_MAX_SWAPS", "HOTSPLIT_DYN_PRIOR_TOKENS",
                 "HOTSPLIT_MAX_TOKENS", "HOTSPLIT_TOPK", "HOTSPLIT_LIVE_DECODE_MAX",
                 "HOTSPLIT_DYN_MAX_PLAN_ERRORS", "HOTSPLIT_DYN_MAX_COMMIT_ERRORS",
                 "HOTSPLIT_VRAM_RESERVE_GIB", "HOTSPLIT_LIVE_SECS", "HOTSPLIT_HOST_FLOOR_GIB"):
        assert knob in covered, f"{knob} must be validated on the host before the model load"
    # boolean normalization must exist (round 61): the recipe's normalize_bool
    # contract -- it lives in the LAUNCHER (the shared module builds env args).
    assert "normalize_bool" in launcher, "the launcher must normalize boolean knobs like the recipe"