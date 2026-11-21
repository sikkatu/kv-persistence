# SPDX-License-Identifier: Apache-2.0
"""r22 M1: the operator pin KVP_LMCACHE_BUILD must actually WORK.

Facts (review, HEAD 7a9e615): with pin D in identity.env.local and image B,
the resolver exported KVP_LMCACHE_BUILD=<B> -- overwriting the operator pin
("env wins files" applied to the SAME name). The namespace ignored D and the
gate printed no mismatch. Before r21 such a pin gave exit 4.

Contract (r22 M1):
- the resolver exports the running image's build under its OWN name
  KVP_RUNNING_LMCACHE_BUILD; KVP_LMCACHE_BUILD stays the OPERATOR PIN.
- read_lmcache_build: KVP_RUNNING_LMCACHE_BUILD > baked JSON > live/.
- identity_from_env_files: pin first, then read_lmcache_build.
- verify-engine-identity.sh: pin > KVP_RUNNING > read_lmcache_build; cross-
  check vs the container identity ALWAYS; pin != image -> exit 4, both values.

Golden (acceptance item 3): without a pin the namespace is byte-identical to
the one computed on 7a9e615 with the same fixture -- the cache from that
commit stays valid. GOLDEN_NS was computed on 7a9e615.
"""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))

BUILD_A = "0.5.8rc1.post48+kvp.aaaaaaaaaaaa"   # the operator pin (D)
BUILD_B = "0.5.8rc1.post48+kvp.bbbbbbbbbbbb"   # the image build (B)
GOLDEN_NS = "kvp-ef44c5ce3016e00c3f450ccdaac0cf9c"  # computed on 7a9e615


def _full_identity_env() -> str:
    return "\n".join([
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
        "KVP_L2_SERDE=zstd",
    ])


def _sha(p):
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _namespace_of(env_file: pathlib.Path, pin: str | None, running: str | None = None) -> str:
    """namespace from a subprocess with the given pin (or none)."""
    env = dict(os.environ)
    for var in ("KVP_LMCACHE_IDENTITY", "KVP_IMAGE_IDENTITY",
                "KVP_LIVE_DIR", "LIVE_DIR", "KVP_LMCACHE_BUILD",
                "KVP_RUNNING_LMCACHE_BUILD", "KVP_MODEL_CONFIG"):
        env.pop(var, None)
    if pin is not None:
        env["KVP_LMCACHE_BUILD"] = pin
    if running is not None:
        env["KVP_RUNNING_LMCACHE_BUILD"] = running
    code = (
        "import sys; sys.path.insert(0, {impl!r})\n"
        "from kvpersist.identity import identity_from_env_files\n"
        "i = identity_from_env_files({f!r})\n"
        "print(i.namespace())\n"
    ).format(impl=str(IMPL), f=str(env_file))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    return out.stdout.strip()


def test_m1_pin_changes_namespace(tmp_path):
    """> Pin D vs no pin: different namespaces; pin == build B gives the
    B-computed namespace."""
    f = tmp_path / "identity.env"
    f.write_text(_full_identity_env())
    # "no pin" == the resolver exported the running image's build (B) under
    # its own name -- the namespace follows the RUNNING image
    ns_nopin = _namespace_of(f, None, running=BUILD_B)
    ns_D = _namespace_of(f, BUILD_A, running=BUILD_B)
    ns_B = _namespace_of(f, BUILD_B, running=BUILD_B)
    assert ns_D != ns_nopin, "the pin must change the namespace"
    assert ns_B != ns_D, "different builds -> different namespaces"
    # without a pin the namespace is the GOLDEN (running image / B value)
    assert ns_nopin == GOLDEN_NS, (
        f"the no-pin namespace must stay byte-identical to 7a9e615:\n"
        f"  got      {ns_nopin}\n  expected {GOLDEN_NS}")


def _gate_repo(tmp_path, pin: str | None, image_build: str):
    """MINI REPO in tmp (the real configs/ is NEVER touched -- r21 L5)."""
    import re as _re
    patch_dir = ROOT / "vendor" / "patches" / "lmcache"
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
    if pin is not None:
        text += f"\nKVP_LMCACHE_BUILD={pin}\n"
    ident_env.write_text(text)
    live = repo / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    return repo, bin_dir


def _run_gate(tmp_path, pin, image_build, extra_env=None):
    repo, bin_dir = _gate_repo(tmp_path, pin, image_build)
    (tmp_path / "cache").mkdir()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_REPO_ROOT=str(repo),
               KVP_LIVE_DIR=str(repo / "live"),
               KVP_CACHE_ROOT=str(tmp_path / "cache"))
    for var in ("KVP_LMCACHE_BUILD", "KVP_RUNNING_LMCACHE_BUILD",
                "KVP_SKIP_LMC_IDENTITY", "KVP_SKIP_IMAGE_CHECK",
                "MODEL_DIR", "KVP_MODEL_CONFIG"):
        env.pop(var, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(repo / "scripts" / "verify-engine-identity.sh"),
                           "test-container"],
                          capture_output=True, text=True, timeout=60,
                          env=env, cwd=str(tmp_path))


def test_m1_pin_vs_image_mismatch_shows_both(tmp_path):
    """> Pin D != image B -> exit 4, lmcache_build named, BOTH builds printed."""
    r = _run_gate(tmp_path, BUILD_A, BUILD_B)
    out = r.stdout + r.stderr
    assert r.returncode == 4, f"pin != image must be MISMATCH:\n{out[-1500:]}"
    assert "lmcache_build" in out, out[-800:]
    assert BUILD_A in out and BUILD_B in out, (
        f"both builds must be printed:\n{out[-800:]}")


def test_m1_pin_equal_to_image_no_mismatch(tmp_path):
    """> Pin == image build -> no lmcache_build mismatch."""
    r = _run_gate(tmp_path, BUILD_B, BUILD_B)
    out = r.stdout + r.stderr
    assert "lmcache_build" not in out or "!=" not in out, (
        f"pin == image must not mismatch on lmcache_build:\n{out[-800:]}")
    assert r.returncode != 4 or "lmcache_build" not in out, (
        f"no lmcache_build MISMATCH expected:\n{out[-800:]}")


def test_m1_no_pin_uses_running_export(tmp_path):
    """> No pin: the gate expects KVP_RUNNING_LMCACHE_BUILD (the resolver's
    export) -- with the export == image build there is no mismatch."""
    r = _run_gate(tmp_path, None, BUILD_B,
                  extra_env={"KVP_RUNNING_LMCACHE_BUILD": BUILD_B})
    out = r.stdout + r.stderr
    assert "lmcache_build" not in out or "!=" not in out, (
        f"export == image must not mismatch:\n{out[-800:]}")