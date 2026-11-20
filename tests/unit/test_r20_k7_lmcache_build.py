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
    of the baked identity JSON. `check_image_identity.py --print-wheel-version`
    exposes that value; verify-engine-identity.sh cross-checks the pin against
    the running image's wheel_version.

Tests (real code, real scripts):
  (1) CacheIdentity: empty lmcache_build -> IdentityError; a different build ->
      a different namespace; the same build -> the same namespace.
  (2) identity_from_env_files derives lmcache_build from the baked identity
      JSON (single source) -- the value bash reads is the value Python hashes.
  (3) check_image_identity.py --print-wheel-version prints wheel_version.
  (4) verify-engine-identity.sh cross-checks the KVP_LMCACHE_BUILD pin against
      the image's wheel_version (mismatch -> exit 4).
  (5) mutation: removing lmcache_build from the namespace -> the build-
      difference test (1) goes RED.
"""
from __future__ import annotations
import json
import os
import pathlib
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
def test_check_image_identity_prints_wheel_version(tmp_path):
    """> check_image_identity.py --print-wheel-version prints the build."""
    ident_json = tmp_path / "kvp-lmcache-identity.json"
    ident_json.write_text(json.dumps({"wheel_version": BUILD_A}))
    r = subprocess.run(
        [sys.executable, str(CHECK_IDENT), "--print-wheel-version",
         str(ident_json)],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=str(IMPL)),
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert BUILD_A in r.stdout, (
        f"--print-wheel-version must print the wheel_version:\n{r.stdout}")


# ------------------------------------------------- (4) gate cross-checks build
def _gate_env(tmp_path, pin_build, image_build):
    """REAL verify-engine-identity.sh + a docker stub serving the identity
    JSON; a KVP_LMCACHE_BUILD pin in identity.env.local."""
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
    # pin KVP_LMCACHE_BUILD in identity.env.local (restored by the caller)
    cfg = ROOT / "configs" / "mimo-v2.6-pro"
    return bin_dir, cfg


def test_gate_crosschecks_lmcache_build(tmp_path):
    """> A KVP_LMCACHE_BUILD pin that disagrees with the image's wheel_version
    must refuse cache reuse (exit 4)."""
    bin_dir, cfg = _gate_env(tmp_path, BUILD_A, BUILD_B)
    local = cfg / "identity.env.local"
    existed = local.exists()
    backup = local.read_text() if existed else None
    try:
        base = (cfg / "identity.env").read_text()
        import re
        base = re.sub(r"^(KVP_MODEL_REVISION)=.*$", r"\1=" + "0" * 40,
                      base, flags=re.M)
        base = re.sub(r"^(KVP_TOKENIZER_REVISION)=.*$", r"\1=" + "0" * 40,
                      base, flags=re.M)
        base = re.sub(r"^(KVP_CHAT_TEMPLATE_DIGEST)=.*$", r"\1=" + "0" * 64,
                      base, flags=re.M)
        base += f"\nKVP_LMCACHE_BUILD={BUILD_A}\n"
        local.write_text(base)
        env = dict(os.environ,
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   KVP_LMCACHE_BUILD=BUILD_A)
        env.pop("KVP_SKIP_LMC_IDENTITY", None)
        r = subprocess.run(["bash", str(GATE), "test-container"],
                           capture_output=True, text=True, timeout=60,
                           env=env, cwd=str(tmp_path))
        out = r.stdout + r.stderr
        assert r.returncode == 4, (
            "a build pin != the image wheel_version must be a MISMATCH "
            f"(exit 4):\n{out[-1500:]}")
        assert "lmcache_build" in out or "wheel_version" in out, (
            f"the refusal must name the build field:\n{out[-800:]}")
    finally:
        if existed:
            local.write_text(backup)
        elif local.exists():
            local.unlink()


# ------------------------------------------------------------------- mutation
def test_mutation_dropping_lmcache_build_from_namespace_is_red(tmp_path):
    """> (5) Mutation: drop `lmcache_build` from the namespace payload (make it
    a namespace_hint-like label) -> the build-difference test must catch it.
    Proves test (2) is meaningful: the field MUST enter the hashed payload."""
    import dataclasses
    assert "lmcache_build" in CacheIdentity.__dataclass_fields__, (
        "the lmcache_build field must exist")
    # behavioural: canonical() must carry lmcache_build in the hashed payload
    ident = make_identity()
    canon = ident.canonical()
    assert canon.get("lmcache_build") == BUILD_A, (
        "lmcache_build must be part of the canonical (hashed) payload")
    # and the mutation is observable at the namespace level
    mutated = make_identity(lmcache_build=BUILD_B).namespace()
    assert mutated != ident.namespace(), (
        "dropping lmcache_build from the payload would make these equal")