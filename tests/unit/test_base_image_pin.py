# SPDX-License-Identifier: Apache-2.0
"""r6-7 B-1: the base vLLM image comes from ONE pin file
(vendor/pinned-base-image.env) -- a clean clone must build WITHOUT any
`export IMAGE`. IMAGE/KVP_LMC_IMAGE are no longer read by the build scripts
(IMAGE means the CUSTOM runtime image in the launch chain); KVP_BASE_IMAGE
overrides; a floating tag is refused.

Reproduced on the operator machine: first run died 'ERROR(build-lmcache):
IMAGE is not set' -- the pin existed in docs but no script knew it.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
BUILD_IMG = ROOT / "scripts" / "build-image.sh"
PIN_FILE = ROOT / "vendor" / "pinned-base-image.env"

DOCKER_STUB = '''#!/usr/bin/env bash
cmd="${1:-}"; shift || true
LOG_DIR="${KVP_TEST_DOCKER_LOG:-/tmp}"
echo "$cmd $*" >> "$LOG_DIR/args.log"
case "$cmd" in
  run) cat > /dev/null
       echo "KVP_STUB_STDIN_BYTES=0"
       echo "KVP_PY_IMPORTS=OK
KVP_CUDA_OPS_IMPORT=OK
KVP_IDENTITY_FILE=OK
KVP_GPU_COUNT=4
KVP_GPU_SMOKE_OK_COUNT=4"
       exit 0 ;;
  inspect) echo "sha256:$(printf 'a%.0s' $(seq 64))"; exit 0 ;;
  *) exit 0 ;;
esac
'''

GIT_STUB = '''#!/usr/bin/env bash
case "$*" in
  *"rev-parse HEAD"*) echo 03ee5b82418b6bd44e424cdd46932d9346f43eb2 ;;
  *"merge-base --is-ancestor"*) exit 0 ;;
  *"apply --reverse --check"*) exit 1 ;;
  *"apply --check"*|*"apply "*) exit 0 ;;
  *"diff --cached"*) echo stubdiff ;;
  *) exit 0 ;;
esac
'''


def _stub_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "git").write_text(GIT_STUB)
    (bin_dir / "git").chmod(0o755)
    return bin_dir


def _fixture(tmp_path: pathlib.Path) -> dict:
    build_dir = tmp_path / "build"
    (build_dir / "LMCache" / ".git").mkdir(parents=True)
    (build_dir / "LMCache" / "requirements").mkdir()
    pdir = tmp_path / "patches"
    pdir.mkdir()
    (pdir / "4136-mp-retrieve-abort.patch").write_text("diff --git a/x b/x\n")
    (pdir / "5972-incomplete-kv-objects.patch").write_text("diff --git a/y b/y\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    whl = build_dir / "wheels"
    whl.mkdir(parents=True)
    (whl / "lmcache-0.5.7.dev428+kvp.abc.whl").write_bytes(b"PK\x03\x04fake")
    (build_dir / "provenance.json").write_text(json.dumps(
        {"wheel_version": "0.5.7.dev428+kvp.deadbeefcafe",
         "module": "custom-lmcache-build"}))
    return {"KVP_BUILD_DIR": str(build_dir), "KVP_PATCH_DIR": str(pdir),
            "KVP_OUT_DIR": str(out_dir),
            "KVP_IDENTITY_OUT": str(out_dir / "image-identity.env")}


def _run(script: pathlib.Path, tmp_path: pathlib.Path, extra_env=None):
    bin_dir = _stub_bin(tmp_path)
    log_dir = tmp_path / "docker-log"
    log_dir.mkdir()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_TEST_DOCKER_LOG=str(log_dir),
               KVP_TEST_SCRIPTS_DIR=str(ROOT / "scripts"))
    # clean-clone situation: no image variable at all
    for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
        env.pop(var, None)
    env.update(_fixture(tmp_path))
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(script)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    return r, log_dir


def _pin_value() -> str:
    m = re.search(r'^KVP_BASE_IMAGE_DEFAULT="([^"]+)"', PIN_FILE.read_text(), re.M)
    assert m, "vendor/pinned-base-image.env must define KVP_BASE_IMAGE_DEFAULT"
    return m.group(1)


# ---------------------------------------------------------------------------
def test_build_lmcache_uses_pin_file_without_export(tmp_path):
    """Clean env: build-lmcache.sh must use EXACTLY the pin-file image."""
    r, log_dir = _run(BUILD_LMC, tmp_path)
    out = r.stdout + r.stderr
    assert "IMAGE is not set" not in out, "clean clone must not die on IMAGE"
    args = (log_dir / "args.log").read_text()
    assert _pin_value() in args or _pin_value() in out, (
        f"the pin file value must be the docker image:\n{out[-600:]}\n{args[-600:]}"
    )


def test_build_image_uses_pin_file_without_export(tmp_path):
    r, log_dir = _run(BUILD_IMG, tmp_path)
    out = r.stdout + r.stderr
    assert "is not set" not in out, f"must resolve the base image itself:\n{out[-600:]}"
    args = (log_dir / "args.log").read_text()
    assert _pin_value() in out or _pin_value() in args, (
        f"base image pin must come from the file:\n{out[-600:]}"
    )


def test_kvp_base_image_overrides(tmp_path):
    other = f"example/other:tag@sha256:{'b' * 64}"
    r, log_dir = _run(BUILD_LMC, tmp_path, {"KVP_BASE_IMAGE": other})
    args = (log_dir / "args.log").read_text()
    assert other in args, f"KVP_BASE_IMAGE must override the file default:\n{args[-600:]}"


def test_floating_tag_in_kvp_base_image_refused(tmp_path):
    r, _ = _run(BUILD_LMC, tmp_path, {"KVP_BASE_IMAGE": "vllm/vllm-openai:nightly"})
    assert r.returncode != 0, "a floating tag must be refused"
    assert "digest" in (r.stdout + r.stderr).lower()


def test_bad_digest_refused_naming_file(tmp_path):
    bad = tmp_path / "broken-pin.env"
    bad.write_text('KVP_BASE_IMAGE_DEFAULT="repo:tag@sha256:abc123"\n')
    r, _ = _run(BUILD_LMC, tmp_path, {"KVP_PIN_FILE": str(bad)})
    assert r.returncode != 0, "a non-64-hex digest must be refused"
    out = r.stdout + r.stderr
    assert str(bad) in out, f"the refusal must name the pin file:\n{out[-600:]}"


def test_image_env_is_warned_and_ignored(tmp_path):
    """IMAGE=<custom runtime> in the build chain: warn and do NOT use it."""
    trap = f"kvp-lmcache:latest@sha256:{'c' * 64}"
    r, log_dir = _run(BUILD_LMC, tmp_path, {"IMAGE": trap})
    out = r.stdout + r.stderr
    assert "KVP_BASE_IMAGE" in out, (
        f"the warning must point at KVP_BASE_IMAGE:\n{out[-600:]}"
    )
    args = (log_dir / "args.log").read_text()
    assert trap not in args, "IMAGE must not be used as the build base image"
    assert _pin_value() in args, "the pin file must win over IMAGE"