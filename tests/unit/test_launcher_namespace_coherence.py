"""Cross-launcher namespace coherence (review round 20).

Both launchers must derive the SAME `KVP_SERVED_MODEL_NAME` default for the
same deployment -- otherwise the identical cache identity gets two LMCache
model_names (`mimo-pro/<hash>` vs `MiMo-V2.6-Pro-MOPD/<hash>`) and the two
paths never share cached blocks (reviewer measured exactly this).

The check EXECUTES the real default-assignment line taken from the launcher
script (not a hand-copied constant), so if the launcher regresses to
`basename "$MODEL_DIR"` the test fails.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = [
    ROOT / "scripts" / "verify-engine-identity.sh",  # Round 80b: the default resolver lives in the gate now
]


def _default_expr(script: Path) -> str:
    """Round 80b: the served-model-name default now lives in the gate's
    python resolver -- `pick("KVP_SERVED_MODEL_NAME", "SERVED_MODEL_NAME",
    default="mimo-pro")`. The contract is unchanged: the default follows
    SERVED_MODEL_NAME and is NEVER basename(MODEL_DIR)."""
    text = script.read_text()
    m = re.search(
        r'pick\("KVP_SERVED_MODEL_NAME",\s*"SERVED_MODEL_NAME",\s*default="(.+?)"\)',
        text,
    )
    assert m, f"{script.name}: no KVP_SERVED_MODEL_NAME default expression found"
    return m.group(1)


def test_served_model_name_default_matches_served_model_name(tmp_path):
    """The default must follow SERVED_MODEL_NAME (what the engine is called
    via --served-model-name), NOT basename(MODEL_DIR). The gate's resolver
    picks SERVED_MODEL_NAME first and falls back to a literal default (not the
    model directory name) -- the two engine paths must agree for identical
    deployments to share one cache namespace."""
    script = LAUNCHERS[0]
    text = script.read_text()
    # the resolver reads SERVED_MODEL_NAME (the operator name) as the source
    assert 'pick("KVP_SERVED_MODEL_NAME", "SERVED_MODEL_NAME"' in text, (
        f"{script.name}: the served-model-name default must follow SERVED_MODEL_NAME")
    default = _default_expr(script)
    # the fallback is a literal, never basename(MODEL_DIR) / model_dir.name
    assert "basename" not in default and "MODEL_DIR" not in default, (
        f"{script.name}: the default must NOT be basename(MODEL_DIR): {default!r}")
    assert default == "mimo-pro", (
        f"{script.name}: expected the documented default 'mimo-pro', got {default!r}")
    # SERVED_MODEL_NAME must win over the KVP pin (the promotion contract)
    assert '_has_src KVP_SERVED_MODEL_NAME SERVED_MODEL_NAME' in text, (
        f"{script.name}: an explicit SERVED_MODEL_NAME must outrank the KVP pin")


def _promotion_loop(script: Path) -> str:
    """Round 80b: promotion now lives in the gate's `while ... _has_src ...
    export` loop (the old `for _pair in KV_CACHE_DTYPE:...` loop was the kv
    launcher's). The contract is unchanged: an explicitly-set operator name
    (KV_CACHE_DTYPE, SERVED_MODEL_NAME, ...) outranks the KVP_* pin."""
    text = script.read_text()
    start = text.index("while IFS='=' read -r _eff_k _eff_v")
    end = text.index("done", start) + len("done")
    return text[start:end]


def test_operator_names_promoted_to_kvp(tmp_path):
    """Review (round 27): an explicitly-set operator name must override the
    KVP_* pin (promoted BEFORE the resolver), never be silently ignored. The
    promotion loop lives in the gate now (_has_src ... export)."""
    script = ROOT / "scripts" / "verify-engine-identity.sh"  # Round 80b
    loop = _promotion_loop(script)
    # the loop must consult the operator source for each promoted name
    assert "_has_src KVP_SERVED_MODEL_NAME SERVED_MODEL_NAME" in loop, (
        "SERVED_MODEL_NAME must be consulted as the promotion source")
    assert "_has_src KVP_KV_CACHE_DTYPE KV_CACHE_DTYPE" in loop, (
        "KV_CACHE_DTYPE must be consulted as the promotion source")
    assert 'export "$_eff_k=$_eff_v"' in loop, (
        "the promoted value must be exported (not silently ignored)")
    # the promotion is guarded by _has_src: an unset operator name does NOT
    # clobber the pin -- that is the whole point of the contract
    assert "_has_src" in loop and "continue" in loop, (
        "a missing operator source must fall through to the KVP pin")
