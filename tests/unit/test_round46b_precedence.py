"""Round 46b: LMCACHE_L2_SERDE env must NOT outrank engine-identity.env.

The user's log reproduced it: a plain `bash scripts/verify-engine-identity.sh`
reported zstd (file read), but inside `test-recovery.sh` (which sources
env.sh -> exports LMCACHE_L2_SERDE=none by default) the same gate reported
identity=none != engine/config=zstd. The recorded launch identity is the
ground truth of the RUNNING engine; the folded env default is only a
fallback.

This test EXECUTES the extracted precedence block from the embedded
Python of verify-engine-identity.sh under different environments.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "scripts" / "verify-engine-identity.sh"


def _extract_precedence() -> str:
    t = VERIFY.read_text()
    m = re.search(
        r'(_engine_identity = _read_engine_identity\(\)\n.*?\n_l2_expect = _fold_serde\(_l2_switch, _l2_pin\))',
        t, re.S)
    assert m, "precedence block not found in verify-engine-identity.sh"
    reader = re.search(r'(def _read_engine_identity\(\):.*?\n    return out\n)', t, re.S)
    fold = re.search(r'(def _fold_serde.*?\n    return configured if configured not in \("raw", ""\) else "none"\n)',
                     t, re.S)
    assert reader and fold, "helpers not found"
    return reader.group(1) + "\n" + fold.group(1) + "\nimport os\nvals = {}\n" + m.group(1) + '\nprint(_l2_expect)\n'


def _run(tmp_path, env, with_file=True):
    f = tmp_path / "engine-identity.env"
    if with_file:
        f.write_text("KVP_L2_SERDE=zstd\n")
    else:
        f.unlink(missing_ok=True)
    code = _extract_precedence()
    import os
    full_env = dict(os.environ)
    full_env.update(env)
    full_env["KVP_CACHE_ROOT"] = str(tmp_path)
    return subprocess.run(["python3", "-c", code], capture_output=True, text=True,
                          env=full_env)


def test_env_default_none_does_not_outrank_file(tmp_path):
    """EXACT user scenario: env.sh exports LMCACHE_L2_SERDE=none, no switch,
    no pin -> the file's zstd must win."""
    r = _run(tmp_path, {"LMCACHE_L2_SERDE": "none", "USE_LOSSLESS_SERDE": ""})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "zstd", (r.stdout, r.stderr)


def test_switch_still_wins_over_file(tmp_path):
    r = _run(tmp_path, {"LMCACHE_L2_SERDE": "none", "USE_LOSSLESS_SERDE": "1"})
    assert r.stdout.strip() == "zstd", (r.stdout, r.stderr)


def test_explicit_env_zstd_without_file(tmp_path):
    r = _run(tmp_path, {"LMCACHE_L2_SERDE": "zstd", "USE_LOSSLESS_SERDE": ""},
             with_file=False)
    assert r.stdout.strip() == "zstd", (r.stdout, r.stderr)


def test_no_file_and_default_none_stays_none(tmp_path):
    r = _run(tmp_path, {"LMCACHE_L2_SERDE": "none", "USE_LOSSLESS_SERDE": ""},
             with_file=False)
    assert r.stdout.strip() == "none", (r.stdout, r.stderr)