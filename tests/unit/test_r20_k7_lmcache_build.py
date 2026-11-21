# SPDX-License-Identifier: Apache-2.0
"""
r20 K7 (P0): the cache namespace must distinguish LMCache BUILDS.

Hardware/code fact: CacheIdentity had NO field depending on the LMCache build
(wheel / patches). `connector_version` is a hand-typed tag ("vllm-8e2beaa3bff@
lmcache-mp"). So the stock build 0.5.6 and our custom 0.5.8rc1.post48+
kvp.60dce759ea93 derived the SAME namespace -- a NEW server read the OLD
cache (and vice versa).

Contract (r20 K7):
  * CacheIdentity gains a MANDATORY `lmcache_build` field = the wheel_version
    ("0.5.8rc1.post48+kvp.<12hex>") recorded in /etc/kvp-lmcache-identity.json.
    Empty / unreadable -> IdentityError (uncertain -> cache miss).
  * It is hashed into the namespace, so two different builds never share.
  * ONE source for every consumer (bash + Python must agree): a shared reader
    of the baked identity JSON. the resolver exports KVP_RUNNING_LMCACHE_BUILD; verify-engine-identity.sh cross-checks the pin against
    the running image's wheel_version.

Tests (real code, real scripts):
  (1) CacheIdentity: empty lmcache_build -> IdentityError; a different build ->
      a different namespace; the same build -> the same namespace.
  (2) identity_from_env_files derives lmcache_build from the baked identity
      JSON (single source) -- the value bash reads is the value Python hashes.
    (4) verify-engine-identity.sh cross-checks the KVP_LMCACHE_BUILD pin against
      the image's wheel_version (mismatch -> exit 4).
  (5) mutation: removing lmcache_build from the namespace -> the build-
      difference test (1) goes RED.
"""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))

from kvpersist.identity import (  # noqa: E402
    CacheIdentity, IdentityError, identity_from_env_files)

GATE = ROOT / "scripts" / "verify-engine-identity.sh"
CHECK_IDENT = IMPL / "kvpersist" / "check_image_identity.py"

BUILD_A = "0.5.8rc1.post48+kvp.60dce759ea93"
BUILD_B = "0.5.8rc1.post48+kvp.deadbeefcafe"


def make_identity(**over):
    base = dict(
        model_id="XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        model_revision="a" * 40,
        tokenizer_name="XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        tokenizer_revision="b" * 40,
        kv_cache_dtype="fp16",
        block_size=16,
        num_kv_heads=8,
        head_dim=128,
        tp_size=4,
        connector="LMCacheMPConnector",
        connector_version="lmcache-0.5.x@pin",
        max_model_len=1048576,
        chat_template_digest="c" * 64,
        lmcache_build=BUILD_A,
    )
    base.update(over)
    return CacheIdentity(**base)


# ---------------------------------------------------------------- (1) identity
def test_lmcache_build_is_mandatory_and_empty_raises():
    """> An identity without a build is UNCERTAIN -> IdentityError."""
    ident = make_identity(lmcache_build="")
    with pytest.raises(IdentityError):
        ident.namespace()


def test_namespace_changes_with_lmcache_build():
    """> Two different LMCache builds must never share a namespace."""
    a = make_identity(lmcache_build=BUILD_A).namespace()
    b = make_identity(lmcache_build=BUILD_B).namespace()
    same = make_identity(lmcache_build=BUILD_A).namespace()
    assert a != b, "a different lmcache_build must change the namespace"
    assert a == same, "the same lmcache_build must keep the namespace stable"


# ------------------------------------------------- (2) single source for build
def test_identity_from_env_files_reads_build_from_identity_json(tmp_path):
    """> The value Python hashes == the value in the baked identity JSON."""
    ident_json = tmp_path / "kvp-lmcache-identity.json"
    ident_json.write_text(json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "wheel_version": BUILD_A,
    }))
    env = tmp_path / "identity.env"
    env.write_text("\n".join([
        "KVP_MODEL_ID=XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        f"KVP_MODEL_REVISION={'a' * 40}",
        "KVP_TOKENIZER_NAME=XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        f"KVP_TOKENIZER_REVISION={'b' * 40}",
        "KVP_KV_CACHE_DTYPE=fp16",
        "KVP_BLOCK_SIZE=16",
        "KVP_NUM_KV_HEADS=8",
        "KVP_HEAD_DIM=128",
        "KVP_TP_SIZE=4",
        "KVP_CONNECTOR=LMCacheMPConnector",
        "KVP_CONNECTOR_VERSION=lmcache-0.5.x@pin",
        "KVP_MAX_MODEL_LEN=1048576",
        f"KVP_CHAT_TEMPLATE_DIGEST={'c' * 64}",
    ]))
    old = dict(os.environ)
    try:
        for k in ("KVP_LMCACHE_BUILD", "KVP_LMCACHE_IDENTITY"):
            os.environ.pop(k, None)
        os.environ["KVP_LMCACHE_IDENTITY"] = str(ident_json)
        os.environ["KVP_MODEL_CONFIG"] = ""
        ident = identity_from_env_files(str(env))
        assert ident.lmcache_build == BUILD_A, (
            "lmcache_build must come from the baked identity JSON "
            f"(got {ident.lmcache_build!r})")
    finally:
        os.environ.clear()
        os.environ.update(old)


# --------------------------------------------------- (3) check-image print mode
def _gate_repo(tmp_path, pin_build: str, image_build: str):
    """r21 L5: a MINI REPO in tmp -- the real configs/ is NEVER written.
    Real verify-engine-identity.sh (copied), implementation/ + vendor/
    symlinked, configs/ COPIED with the pins filled, live/ present."""
    import re as _re
    import hashlib
    patch_dir = ROOT / "vendor" / "patches" / "lmcache"

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    ident = {
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel_sha256": "a" * 64,
        "wheel_version": image_build,
    }
    body = tmp_path / "ident.json"
    body.write_text(json.dumps(ident))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
case "$cmd" in
  exec) cat "{body}" ;;
  inspect) echo "[]" ;;
  *) exit 0 ;;
esac
""")
    (bin_dir / "docker").chmod(0o755)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "verify-engine-identity.sh",
                repo / "scripts" / "verify-engine-identity.sh")
    for d in ("implementation", "vendor"):
        (repo / d).symlink_to(ROOT / d)
    shutil.copytree(ROOT / "configs", repo / "configs")
    ident_env = repo / "configs" / "mimo-v2.6-pro" / "identity.env"
    text = ident_env.read_text()
    for name, value in (("KVP_MODEL_REVISION", "0" * 40),
                        ("KVP_TOKENIZER_REVISION", "0" * 40),
                        ("KVP_CHAT_TEMPLATE_DIGEST", "0" * 64)):
        text = _re.sub(r"^(" + name + r")=.*$", r"\1=" + value, text, flags=_re.M)
    text += f"\nKVP_LMCACHE_BUILD={pin_build}\n"
    ident_env.write_text(text)
    live = repo / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    return repo, bin_dir


def test_gate_crosschecks_lmcache_build(tmp_path):
    """> A KVP_LMCACHE_BUILD pin that disagrees with the image's wheel_version
    must refuse cache reuse (exit 4). r21 L5: works on a COPY of configs/ in
    a tmp repo -- the real repository is never touched."""
    repo, bin_dir = _gate_repo(tmp_path, BUILD_A, BUILD_B)
    (tmp_path / "cache").mkdir()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_REPO_ROOT=str(repo),
               KVP_LIVE_DIR=str(repo / "live"),
               KVP_CACHE_ROOT=str(tmp_path / "cache"))
    env.pop("KVP_SKIP_LMC_IDENTITY", None)
    r = subprocess.run(["bash", str(repo / "scripts" / "verify-engine-identity.sh"),
                        "test-container"],
                       capture_output=True, text=True, timeout=60,
                       env=env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 4, (
        "a build pin != the image wheel_version must be a MISMATCH "
        f"(exit 4):\n{out[-1500:]}")
    assert "lmcache_build" in out or "wheel_version" in out, (
        f"the refusal must name the build field:\n{out[-800:]}")


# ------------------------------------------------------------------- mutation
def test_mutation_dropping_lmcache_build_from_namespace_is_red(tmp_path):
    """> (5) r21 L5 HONEST mutation: a COPY of the identity module with
    lmcache_build REMOVED from the hashed payload (canonical) -> builds A and
    B give the SAME namespace on the mutant. The real module's assertion
    (A's namespace != B's) would be RED against this mutant -- proves the
    namespace test is meaningful."""
    import importlib.util as _ilu
    import sys as _sys
    real_src = (IMPL / "kvpersist" / "identity.py").read_text()
    # drop lmcache_build from the canonical payload (like namespace_hint)
    marker = '            if f.name == "namespace_hint":\n                continue\n'
    assert marker in real_src, "canonical() marker not found"
    mutant_src = real_src.replace(
        marker,
        '            if f.name in ("namespace_hint", "lmcache_build"):\n'
        '                continue\n', 1)
    assert mutant_src != real_src
    mutant_py = tmp_path / "identity_mutant.py"
    mutant_py.write_text(mutant_src)
    spec = _ilu.spec_from_file_location("identity_ns_mutant", mutant_py)
    mutant_mod = _ilu.module_from_spec(spec)
    _sys.modules[spec.name] = mutant_mod
    spec.loader.exec_module(mutant_mod)
    base = dict(
        model_id="XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        model_revision="a" * 40,
        tokenizer_name="XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        tokenizer_revision="b" * 40,
        kv_cache_dtype="fp16",
        block_size=16, num_kv_heads=8, head_dim=128, tp_size=4,
        connector="LMCacheMPConnector",
        connector_version="lmcache-0.5.x@pin",
        max_model_len=1048576,
        chat_template_digest="c" * 64,
    )
    ident_a = mutant_mod.CacheIdentity(**{**base, "lmcache_build": BUILD_A})
    ident_b = mutant_mod.CacheIdentity(**{**base, "lmcache_build": BUILD_B})
    assert ident_a.namespace() == ident_b.namespace(), (
        "the mutant must collapse A and B to ONE namespace (the defect) -- "
        "proves the real A!=B namespace test is meaningful")
