"""Round 81 regression (two reviewers):
P1.1 port parity: a bare LMCACHE_PORT must reach BOTH the pre-flight and the
    LMCache connector (the old code probed LMCACHE_PORT but the connector
    fell back to 5555 -- the pre-flight could pass while the engine failed).
P1.2 MODEL_DIR auto-resolution: start-all.sh must call the shared
    _kvp_resolve_model_dir when MODEL_DIR is not exported (live/model_dir
    wins; an explicit MODEL_DIR always wins).
P2.4 direct run without KVP_REPO: the pre-flight block must still find
    ~/kv-persistence (the same fallback the identity gate uses).
P2.2 round50 strictness: the MODEL_DIR resolution must be compared INSIDE
    the launcher's own text (not the cross-file union, where the order is an
    artifact of concatenation).
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T
import os
import stat
import subprocess
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
RECIPE = _T.recipe_launcher()


# ---------- P1.1: port parity ----------
def test_port_resolution_is_identical_everywhere():
    """KV_PERSIST_PORT must resolve with the LMCACHE_PORT fallback in BOTH
    the pre-flight probe and the connector assignment."""
    assert RECIPE is not None
    t = RECIPE.read_text()
    fallback = '${KV_PERSIST_PORT:-${LMCACHE_PORT:-5555}}'
    assert t.count(fallback) >= 2, (
        "both the pre-flight and the connector must resolve the port with the "
        f"same LMCACHE_PORT fallback (found {t.count(fallback)} uses)")
    # the bare 5555 fallback (no LMCACHE_PORT) must be gone from the connector
    assert 'KV_PERSIST_PORT="${KV_PERSIST_PORT:-5555}"' not in t, (
        "the connector must not fall back to a hardcoded 5555 while the "
        "pre-flight honours LMCACHE_PORT")


# ---------- P1.2: MODEL_DIR auto-resolution in start-all ----------
def test_start_all_calls_the_shared_model_dir_resolver():
    t = (KV / "scripts" / "start-all.sh").read_text()
    assert '_kvp_resolve_model_dir "$ROOT"' in t, (
        "start-all.sh must call the shared MODEL_DIR resolver on the "
        "delegated path (the wiki promises zero-config MODEL_DIR)")
    # the explicit value must win over the resolution: KVP_MODEL_DIR is
    # exported as MODEL_DIR first (the recipe reads MODEL_DIR only), then the
    # resolver runs only when neither is set
    idx_export = t.index('export MODEL_DIR="$KVP_MODEL_DIR"')
    idx_res = t.index('_kvp_resolve_model_dir "$ROOT"')
    idx_guard = t.index('if [ -z "${MODEL_DIR:-}" ]')
    assert idx_export < idx_guard < idx_res, (
        "KVP_MODEL_DIR export -> resolver guard -> resolver (in that order)")


def test_model_dir_resolution_behaviour(tmp_path):
    """EXECUTE the real resolver: live/model_dir wins over the ~/models scan;
    a missing model dir resolves to nothing (the launch then refuses)."""
    root = tmp_path / "kv"
    (root / "live").mkdir(parents=True)
    model = tmp_path / "my-model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    env = {**os.environ, "KVP_MODELS_ROOT": str(tmp_path)}
    r = subprocess.run(
        ["bash", "-c",
         f'. scripts/env.sh && _kvp_resolve_model_dir "{root}"'],
        capture_output=True, text=True, timeout=30,
        env=env, cwd=str(KV))
    # the single model dir under KVP_MODELS_ROOT is found
    assert r.stdout.strip() == str(model), (r.stdout, r.stderr)
    # live/model_dir WINS over the scan
    (root / "live" / "model_dir").write_text(str(model) + "\n")
    model2 = tmp_path / "other"
    model2.mkdir()
    (model2 / "config.json").write_text("{}")
    r2 = subprocess.run(
        ["bash", "-c",
         f'. scripts/env.sh && _kvp_resolve_model_dir "{root}"'],
        capture_output=True, text=True, timeout=30,
        env=env, cwd=str(KV))
    assert r2.stdout.strip() == str(model), (
        f"live/model_dir must win, got {r2.stdout!r}")
    # an explicit MODEL_DIR wins over everything
    r3 = subprocess.run(
        ["bash", "-c",
         f'export MODEL_DIR={model2}; . scripts/env.sh && _kvp_resolve_model_dir "{root}"'],
        capture_output=True, text=True, timeout=30,
        env=env, cwd=str(KV))
    assert r3.stdout.strip() == str(model2), (
        f"explicit MODEL_DIR must win, got {r3.stdout!r}")


# ---------- P2.4: direct run without KVP_REPO ----------
def test_direct_run_resolves_kvp_repo_before_preflight():
    """The pre-flight block must not depend on an exported KVP_REPO -- the
    same default ($HOME/kv-persistence) the identity gate uses must be
    resolved BEFORE the probe."""
    assert RECIPE is not None
    t = RECIPE.read_text()
    resolve = t.index('KVP_REPO="${KVP_REPO:-${KVP_REPO_ROOT:-$HOME/kv-persistence}}"')
    # the early resolver must come BEFORE the pre-flight probe block
    probe = t.index('if [ "${KV_PERSIST:-0}" = "1" ] && [ "${KVP_PREFLIGHTS_DONE:-0}" != "1" ]')
    assert resolve < probe, (
        "KVP_REPO must be resolved BEFORE the direct-run pre-flight block")
    # and the guard must use the resolved value (no -n test on an unset var)
    seg = t[probe:probe + 400]
    assert 'if [ -f "$KVP_REPO/scripts/env.sh" ]' in seg, (
        "the pre-flight must test the resolved KVP_REPO path")


# ---------- P2.2: round50 strictness (per-file, not the union) ----------
def test_model_dir_resolution_precedes_gate_in_the_launcher():
    """The order check must be done INSIDE the launcher's own text. In the
    cross-file union the order is an artifact of concatenation -- deleting
    the launcher's resolution entirely kept the old test green."""
    assert RECIPE is not None
    t = RECIPE.read_text()
    resolve_idx = t.index('MODEL_DIR="${MODEL_DIR:-')
    gate_idx = t.index('bash "$KVP_REPO/scripts/verify-engine-identity.sh"')
    assert resolve_idx < gate_idx, (
        "inside the launcher: MODEL_DIR resolution must come BEFORE the "
        "identity gate (the gate reads the resolved value)")