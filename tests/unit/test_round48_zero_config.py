"""Round 48: zero-config launch (user: why exports and flags in every terminal?).

Three things must now work from a bare `bash scripts/run-vllm-with-persist.sh`
in a fresh terminal:
* IMAGE resolves itself (recipe digest / live/image), remembered in live/;
* MODEL_DIR resolves itself (live/model_dir / identity pin / ~/models auto);
* KVP_SAME_UID=1 is the DEFAULT (no root anywhere);
* the codec defaults to lossless zstd -- and the default must be IDENTICAL
  in bash (env.sh) and Python (identity.py) and the gate (_fold_serde), or
  the namespace diverges and cache reuse is refused.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- defaults: no exports needed
def test_same_uid_is_default():
    t = _read("run-vllm-with-persist.sh")
    assert 'KVP_SAME_UID="${KVP_SAME_UID:-1}"' in t, (
        "same-UID must be the default so a fresh terminal needs no flags")


def test_image_resolves_without_export():
    t = _read("run-vllm-with-persist.sh")
    assert 'print-recipe-image.sh' in t, "IMAGE must resolve from the recipe"
    assert "live/image" in t, "IMAGE must be remembered in live/image"
    # the explicit IMAGE=? check must come AFTER the auto-resolution
    auto_idx = t.index('print-recipe-image.sh')
    req_idx = t.index('IMAGE="${IMAGE:?')
    assert auto_idx < req_idx, "auto-resolve must run before the hard requirement"


def test_model_dir_resolves_without_export():
    t = _read("run-vllm-with-persist.sh")
    assert "live/model_dir" in t, "MODEL_DIR must be remembered in live/model_dir"
    auto_idx = t.index("live/model_dir")
    req_idx = t.index('MODEL_DIR not found')
    assert auto_idx < req_idx, "auto-resolve must run before the hard check"


# ---------------------------------------------------------------- codec default consistency
def _extract_fold() -> str:
    t = _read("verify-engine-identity.sh")
    start = t.index("def _fold_serde")
    end = t.index("\n_l2_missing_adapter", start)
    return t[start:end]


def test_codec_default_identical_in_bash_and_python():
    """The namespace must be the SAME everywhere or cache reuse breaks."""
    env = _read("env.sh")
    assert 'LMCACHE_L2_SERDE="${LMCACHE_L2_SERDE:-zstd}"' in env, (
        "env.sh must default to lossless zstd")
    ident = (ROOT / "implementation" / "kvpersist" / "identity.py").read_text()
    assert 'or "zstd").strip().lower()' in ident, (
        "identity.py must default to lossless zstd")
    fold = _extract_fold()
    assert "return configured or \"zstd\"" in fold, (
        "the gate _fold_serde must default to lossless zstd")


def test_fold_semantics_executed(tmp_path):
    """EXECUTE the gate's real _fold_serde: the semantic table must be
    zstd (default) / lz4 / explicit-none, and 'raw' == 'none'."""
    code = _extract_fold() + '''
cases = [
    (("", ""), "zstd"),            # nothing set -> lossless default
    (("1", ""), "zstd"),
    (("lz4", ""), "lz4"),
    (("0", ""), "none"),           # explicit raw opt-out
    (("none", ""), "none"),
    (("", "zstd"), "zstd"),
    (("", "lz4"), "lz4"),
    (("", "none"), "none"),
    (("", "raw"), "none"),         # raw normalizes to none
    (("1", "none"), "zstd"),       # switch beats configured
]
for (sw, cfg), want in cases:
    got = _fold_serde(sw, cfg)
    assert got == want, f"_fold_serde({sw!r},{cfg!r}) = {got!r}, want {want!r}"
print("FOLD_OK")
'''
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "FOLD_OK" in r.stdout, (r.stdout, r.stderr)


def test_explicit_raw_optout_is_separate_from_unset():
    """USE_LOSSLESS_SERDE=0 must be an EXPLICIT raw opt-out (-> none), while
    an UNSET switch falls through to the configured/default codec."""
    env = _read("env.sh")
    assert "0|no|false|none) _kvp_switch_serde=none ;;" in env, (
        "explicit raw opt-out must map to none")
    assert '"")' in env and "_kvp_switch_serde=\"\" ;;" in env, (
        "an unset switch must fall through (empty)")