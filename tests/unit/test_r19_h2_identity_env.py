# SPDX-License-Identifier: Apache-2.0
"""r19 H2: live/image-identity.env must be a REAL env-file (sourcable under
`set -e`).

The verify output is `docker run ... > verify.txt 2>&1` -- together with the
markers it carries log lines (`[2026-11-19 20:45:32,146] LMCache INFO: ...`,
`W1119 ... deprecated`, vLLM output) and rows like
`KVP_GPU_SMOKE[0] dev=0000:01:00.0 cc=12.0 OK` -- invalid shell variable
names. The old code appended the WHOLE verify.txt to image-identity.env, so
the first `. live/image-identity.env` in a `set -e` script would die.

Fix: image-identity.env carries only lines matching `^KVP_[A-Z0-9_]+=`
(KEY=value, no quotes -- the marker values contain no spaces). The raw
verify output is saved next to it as live/image-verify.txt for diagnostics;
`KVP_GPU_SMOKE[N]` rows go ONLY there, the .env gets the aggregates
KVP_GPU_COUNT, KVP_GPU_SMOKE_OK_COUNT, KVP_GPU_CC.

Test (REAL build-image.sh): the docker stub prints a REALISTIC verify.txt
with noise between the markers. Afterwards `bash -c 'set -e; .
<image-identity.env>; echo ok'` must exit 0 and print ok, and
KVP_CUSTOM_IMAGE_ID must equal the expected value. RED on the pre-fix HEAD.
Mutation: restoring `cat "$TMPD/verify.txt"` into the .env -> RED.
"""
from __future__ import annotations
import os
import pathlib
import subprocess

import test_build_image_runs as base  # shared fixture/stub helpers (r3)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-image.sh"

# a REALISTIC verify.txt: markers + log noise + per-GPU smoke rows (invalid
# shell names) -- exactly what `docker run ... > verify.txt 2>&1` produces
NOISY_VERIFY = """\
[2026-11-19 20:45:32,146] LMCache INFO: torch_dev=cu130 version=0.5.8
W1119 20:45:32 torch/distributed/... is deprecated
KVP_LMC_VERSION=0.5.8rc1.post48+kvp.60dce759ea93
KVP_PY_IMPORTS=OK
KVP_CUDA_OPS_IMPORT=OK
KVP_NATIVE_IMPORT=OK
KVP_ZSTD_IMPORT=OK
KVP_ZSTD_VERSION=0.25.0
KVP_LZ4_IMPORT=OK
KVP_LZ4_VERSION=4.4.5
KVP_IDENTITY_FILE=OK
KVP_IDENTITY_BASE=07f1c6bb642f4772606a36eab730ae009ddce08f
KVP_GPU_SMOKE[0] dev=0000:01:00.0 cc=12.0 OK
KVP_GPU_SMOKE[1] dev=0000:41:00.0 cc=12.0 OK
KVP_GPU_SMOKE[2] dev=0000:81:00.0 cc=12.0 OK
KVP_GPU_SMOKE[3] dev=0000:c1:00.0 cc=12.0 OK
KVP_GPU_SMOKE_OK_COUNT=4
KVP_GPU_COUNT=4
KVP_GPU_CC=12.0
INFO 20:45:33 engine loop ready
"""


def make_fixture(tmp_path: pathlib.Path):
    """A consistent wheel+provenance pair (r18 G2 format)."""
    import hashlib
    import json
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    p4136 = patch_dir / "4136-mp-retrieve-abort.patch"
    p5972 = patch_dir / "5972-incomplete-kv-objects.patch"
    p4136.write_bytes(b"PATCH4136-CONTENT\n")
    p5972.write_bytes(b"PATCH5972-CONTENT\n")
    build_dir = tmp_path / "build"
    wheels = build_dir / "wheels"
    wheels.mkdir(parents=True)
    whl = wheels / ("lmcache-0.5.8rc1.post48+kvp.60dce759ea93"
                    "-cp312-cp312-linux_x86_64.whl")
    whl.write_bytes(b"PK\x05\x06fake-wheel-content")
    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    (build_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "wheel": whl.name,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
        "wheel_sha256": _sha(whl),
        "pr4136_patch_sha256": _sha(p4136),
        "pr5972_patch_sha256": _sha(p5972),
        "tests_skipped": "0",
    }))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return patch_dir, build_dir, out_dir


def _run_image(tmp_path: pathlib.Path, patch_dir, build_dir, out_dir,
               script: pathlib.Path = SCRIPT):
    """REAL build-image.sh; the docker stub emits the NOISY verify output."""
    bin_dir = tmp_path / "bin"
    store = tmp_path / "docker-store"
    base.make_docker_stub(bin_dir, store)
    vbody = tmp_path / "verify-body.txt"
    vbody.write_text(NOISY_VERIFY)
    # make `docker run` print the noisy verify body (the shared stub already
    # prints clean markers -- replace its run case)
    stub = bin_dir / "docker"
    stub.write_text(f"""#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
case "$cmd" in
  run)
    case "$*" in
      *_smoke_cache_server*)
        echo "KVP_SMOKE_ZSTD_READY=OK"; echo "KVP_SMOKE_ZSTD_EXIT=0"
        echo "KVP_SMOKE_NONE_READY=OK"; echo "KVP_SMOKE_NONE_EXIT=0"
        exit 0 ;;
    esac
    cat > /dev/null; cat "{vbody}"; exit 0 ;;
  build) for a in "$@"; do case "$a" in -t) shift ;; *) CTX="$a" ;; esac; done
          mkdir -p "{store}"
          [ -f "$CTX/Dockerfile" ] && cp "$CTX/Dockerfile" "{store}/Dockerfile"
          [ -f "$CTX/kvp-lmcache-identity.json" ] && cp "$CTX/kvp-lmcache-identity.json" "{store}/kvp-lmcache-identity.json"
          exit 0 ;;
  inspect) echo "sha256:{"0" * 56}abcdef"; exit 0 ;;
  *) exit 0 ;;
esac
""")
    stub.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        KVP_PATCH_DIR=str(patch_dir),
        KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"),
        KVP_BUILD_DIR=str(build_dir),
        KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
        KVP_LIVE_DIR=str(out_dir / "live"),
        IMAGE=f"vllm/vllm-openai:nightly-test@sha256:{'1' * 64}",
    )
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    return r, store


def test_identity_env_is_sourcable_with_set_e(tmp_path):
    """THE r19 H2 case: after a realistic (noisy) verify run,
    `. live/image-identity.env` under `set -e` must exit 0 and print ok,
    and KVP_CUSTOM_IMAGE_ID must equal the stub's image id."""
    patch_dir, build_dir, out_dir = make_fixture(tmp_path)
    r, _store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, (
        f"the build must pass:\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    envf = out_dir / "image-identity.env"
    assert envf.is_file(), "image-identity.env must be written"
    src = subprocess.run(
        ["bash", "-c", f'set -e; . "{envf}"; echo ok; echo "ID=$KVP_CUSTOM_IMAGE_ID"'],
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert src.returncode == 0, (
        f"sourcing image-identity.env under set -e must succeed:\n"
        f"stdout:\n{src.stdout}\nstderr:\n{src.stderr}\n"
        f"file:\n{envf.read_text()}")
    assert src.stdout.startswith("ok\n"), src.stdout
    # the id value must survive the source round-trip
    assert "ID=sha256:" + "0" * 56 + "abcdef" in src.stdout, src.stdout
    # every line is a valid KEY=value (no log noise, no KVP_GPU_SMOKE[N])
    for ln in envf.read_text().splitlines():
        assert ln.startswith("KVP_"), f"non-KVP line in .env: {ln!r}"
        key = ln.split("=", 1)[0]
        assert key.replace("_", "").isalnum() and key == key.upper(), (
            f"invalid shell variable name in .env: {key!r}")
        assert "[" not in ln and "]" not in ln, (
            f"per-GPU smoke rows must not land in .env: {ln!r}")


def test_raw_verify_saved_separately(tmp_path):
    """(2) the raw verify output is kept for diagnostics as
    live/image-verify.txt; the .env carries the aggregates."""
    patch_dir, build_dir, out_dir = make_fixture(tmp_path)
    r, _store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-800:]
    verifyf = out_dir / "live" / "image-verify.txt"
    assert verifyf.is_file(), (
        "the raw verify output must be saved as live/image-verify.txt")
    raw = verifyf.read_text()
    assert "LMCache INFO" in raw, "the raw log must be preserved"
    assert "KVP_GPU_SMOKE[0]" in raw, "per-GPU rows belong in the raw log"
    envf = out_dir / "image-identity.env"
    env = envf.read_text()
    assert "KVP_GPU_COUNT=4" in env, env
    assert "KVP_GPU_SMOKE_OK_COUNT=4" in env, env
    assert "KVP_GPU_CC=12.0" in env, env
    assert "LMCache INFO" not in env, "log noise must not be in the .env"


def test_mutation_cat_verify_into_env_is_red(tmp_path):
    """Mutation: restoring `cat "$TMPD/verify.txt"` into the .env must make
    the sourcable test fail (proves the test is meaningful)."""
    src = SCRIPT.read_text()
    marker = '} > "$IDENTITY"'
    assert marker in src, "mutation marker not found"
    mutant_src = src.replace(
        marker,
        '  cat "$TMPD/verify.txt"\n} > "$IDENTITY"', 1)
    assert mutant_src != src
    # r20 K6: the mutant must be RUNNABLE -- build-image.sh reads
    # $ROOT/requirements-image-codecs.txt (repo layout: scripts/ + root)
    mdir = tmp_path / "scripts"
    mdir.mkdir(exist_ok=True)
    (tmp_path / "requirements-image-codecs.txt").write_text(
        (ROOT / "requirements-image-codecs.txt").read_text())
    mutant = mdir / "build-image-mutant.sh"
    mutant.write_text(mutant_src)
    mutant.chmod(0o755)

    patch_dir, build_dir, out_dir = make_fixture(tmp_path)
    r, _store = _run_image(tmp_path, patch_dir, build_dir, out_dir,
                          script=mutant)
    assert r.returncode == 0, r.stderr[-600:]
    envf = out_dir / "image-identity.env"
    srcr = subprocess.run(
        ["bash", "-c", f'set -e; . "{envf}"; echo ok'],
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert srcr.returncode != 0, (
        "mutation detector: with the raw verify.txt in the .env, sourcing "
        f"under set -e must FAIL (the noise breaks it):\n{srcr.stdout}")
    assert "ok" not in srcr.stdout, srcr.stdout