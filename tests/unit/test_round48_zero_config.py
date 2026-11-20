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
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- defaults: no exports needed
def test_same_uid_is_default():
    t = _T.contract_text()
    assert 'KVP_SAME_UID="${KVP_SAME_UID:-1}"' in t, (
        "same-UID must be the default so a fresh terminal needs no flags")


def test_image_resolves_without_export():
    t = _T.contract_text()
    # print-recipe-image.sh is the shared resolver (used by ipc-test.sh)
    assert 'print-recipe-image.sh' in t, "IMAGE must resolve from the recipe"
    assert "live/image" in t, "IMAGE must be remembered in live/image"
    # r20 K4.5: the old test compared source-text positions -- that is not
    # what matters; the BEHAVIOURAL contract is covered by the K1 matrix
    # (tests/unit/test_r20_k1_image_resolution.py) and the K2 write rules
    # (recipes/tests/test_r20_k2_live_image_write.py). Assert the real
    # resolver is wired where the recipe launcher decides the image.
    rl = _T.recipe_launcher()
    assert rl is not None, "the recipe launcher must exist"
    rt = rl.read_text()
    assert "_kvp_resolve_runtime_image" in rt, (
        "the recipe launcher must call the shared runtime-image resolver "
        "before the pin case (r20 K1)")
    assert rt.index("_kvp_resolve_runtime_image") < rt.index('case "$IMAGE"'), (
        "the resolver must run BEFORE the pin-format case (r20 K1)")


def test_image_resolution_behaviour_matches_the_k1_matrix():
    """r20 K4.5 (behavioural, replaces the old source-text test): the real
    _kvp_resolve_runtime_image must resolve the K1 table -- stock IMAGE +
    live/image -> the CUSTOM id (case a of the matrix)."""
    import subprocess as _sp
    live = ROOT  # this repo IS kv-persistence
    if not live.is_dir():
        import pytest
        pytest.skip("SKIP (honest reason): kv-persistence checkout not found")
    # sandbox live/image + a docker stub answering the labels of a real build
    import hashlib, json, tempfile
    tmp = __import__("pathlib").Path(tempfile.mkdtemp())
    try:
        live_dir = tmp / "live"
        live_dir.mkdir()
        custom = "sha256:" + "c" * 64
        (live_dir / "image").write_text(custom + "\n")
        pd = live / "vendor" / "patches" / "lmcache"

        def _sha(p):
            return hashlib.sha256(p.read_bytes()).hexdigest()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        labels = json.dumps({
            "org.kvp.lmc-base": "vllm/vllm-openai:nightly-test@sha256:" + "b" * 64,
            "org.kvp.lmc-patch-4136": _sha(pd / "4136-mp-retrieve-abort.patch"),
            "org.kvp.lmc-patch-5972": _sha(pd / "5972-incomplete-kv-objects.patch"),
        })
        ident = json.dumps({
            "module": "kvp-custom-lmcache-identity",
            "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
            "pr4136_patch_sha256": _sha(pd / "4136-mp-retrieve-abort.patch"),
            "pr5972_patch_sha256": _sha(pd / "5972-incomplete-kv-objects.patch"),
            "wheel": "lmcache-0.5.8rc1.post48+kvp.60dce759ea93-cp312-cp312-linux_x86_64.whl",
            "wheel_sha256": "a" * 64,
            "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
        })
        (bin_dir / "docker").write_text(f'''#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect) echo '{labels}' ;;
    esac ;;
  run)
    _entry=""
    _probe=""
    while [ "$#" -gt 0 ]; do case "$1" in
      --entrypoint) _entry="$2"; shift 2 ;;
      -c) _probe="$2"; shift 2 ;;
      *) shift ;;
    esac; done
    case "$_entry" in
      cat) echo '{ident}' ;;
      python3)
        case "$_probe" in
          *importlib.metadata*) echo "0.5.8rc1.post48+kvp.60dce759ea93" ;;
          *) exit 0 ;;
        esac ;;
    esac ;;
  inspect) echo "{custom}" ;;
  *) exit 0 ;;
esac
''')
        (bin_dir / "docker").chmod(0o755)
        stock = "vllm/vllm-openai:nightly-test@sha256:" + "b" * 64
        r = _sp.run(
            ["bash", "-c",
             f'. "{live}/scripts/env.sh"; IMAGE="{stock}" '
             f'KVP_LIVE_DIR="{live_dir}" _kvp_resolve_runtime_image "{live}" 0 && '
             f'echo "RESOLVED=$IMAGE"'],
            capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ,
                 "PATH": f"{bin_dir}:{__import__('os').environ.get('PATH', '')}"},
            cwd=str(live))
        out = r.stdout + r.stderr
        assert r.returncode == 0, f"case (a) must resolve:\n{out[-1200:]}"
        assert f"RESOLVED={custom}" in out, (
            f"stock base-pin must resolve to the CUSTOM id {custom}:\n{out[-1200:]}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_model_dir_resolves_without_export():
    t = _T.contract_text()
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