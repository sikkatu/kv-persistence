"""Round 50: MODEL_DIR must reach the gate + codec default in config files.

User log: the post-launch gate reported `model_dir=unset`, could not read
config.json and refused to verify num_kv_heads / head_dim (INCONCLUSIVE)
-- the auto-resolved MODEL_DIR was never EXPORTED, so the gate subprocess
did not see it. And `L2 codec: none` (instead of zstd) came from
lmcache-server.env still pinning `LMCACHE_L2_SERDE=none`, which outranked
the round-48 default.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = _T.recipe_launcher()  # Round 80b: recipe owns the engine launch


def test_model_dir_is_exported_for_the_gate():
    """P2.2 (round 81): compare INSIDE the launcher's own text -- in the
    cross-file union the order is an artifact of concatenation (the resolver
    lives in env.sh, the gate call in the launcher), so deleting the
    launcher's resolution entirely kept the old union-based check green."""
    assert LAUNCHER is not None
    t = LAUNCHER.read_text()
    # the resolution must exist and come BEFORE the gate INVOCATION
    resolve_idx = t.index('MODEL_DIR="${MODEL_DIR:-')
    gate_idx = t.index('bash "$KVP_REPO/scripts/verify-engine-identity.sh"')
    assert resolve_idx < gate_idx, (
        "inside the launcher: MODEL_DIR resolution must come BEFORE the "
        "identity gate runs (the gate is a subprocess and reads the resolved "
        "value from the environment)")


def test_model_dir_export_follows_resolution():
    """The export must sit after the resolution block, near the directory
    check -- inside the launcher's own text (see P2.2)."""
    assert LAUNCHER is not None
    t = LAUNCHER.read_text()
    check_idx = t.index('MODEL_DIR not found')
    resolve_idx = t.index('MODEL_DIR="${MODEL_DIR:-')
    assert resolve_idx < check_idx, (
        "inside the launcher: MODEL_DIR resolution must come BEFORE the "
        "existence check")


def test_config_codec_default_is_zstd_in_all_locales():
    for name in ("lmcache-server.env", "lmcache-server.env.ru",
                 "lmcache-server.env.zh"):
        p = ROOT / "configs" / "generic" / name
        t = p.read_text()
        m = re.search(r"^LMCACHE_L2_SERDE=(\S+)", t, re.M)
        assert m, "%s: LMCACHE_L2_SERDE line missing" % name
        assert m.group(1) == "zstd", (
            "%s: codec default must be zstd (round 48/50), got %r" % (name, m.group(1)))


def test_no_locale_divergence():
    """All three locales must agree on LMCACHE_L2_SERDE."""
    vals = set()
    for name in ("lmcache-server.env", "lmcache-server.env.ru",
                 "lmcache-server.env.zh"):
        t = (ROOT / "configs" / "generic" / name).read_text()
        m = re.search(r"^LMCACHE_L2_SERDE=(\S+)", t, re.M)
        vals.add(m.group(1))
    assert len(vals) == 1, "locales diverge on LMCACHE_L2_SERDE: %s" % vals