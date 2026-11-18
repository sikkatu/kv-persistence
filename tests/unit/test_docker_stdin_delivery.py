# SPDX-License-Identifier: Apache-2.0
"""r6-7 A-3: docker stub must behave like REAL docker -- stdin attached only
with -i/--interactive; build scripts must fail LOUDLY on an empty phase.

Reproduced on the operator machine: `docker run` (no -i) leaves `bash -s` an
EMPTY stdin, the in-container script never runs, docker exits 0 and
build-lmcache.sh dies 'no wheel produced -- see output above' into EMPTY
output. build-image.sh is WORSE: empty verify.txt, rc=0, fake success.

Rule: a stub must NOT forgive what the real command does not forgive.
"""
from __future__ import annotations
import os
import pathlib
import subprocess
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
BUILD_IMG = ROOT / "scripts" / "build-image.sh"

DOCKER_STUB = r'''#!/usr/bin/env bash
cmd="${1:-}"; shift || true
INTERACTIVE=0
for a in "$@"; do
  case "$a" in -i|--interactive|-it|-ti) INTERACTIVE=1 ;; esac
done
LOG_DIR="${KVP_TEST_DOCKER_LOG:-/tmp}"
case "$cmd" in
  run)
    # real-docker behaviour: stdin attached only with -i; a mounted FILE in
    # the argv (A-1 delivery) is what the container actually executes. The
    # stub resolves the container mount point back to the host dir via
    # KVP_TEST_SCRIPTS_DIR (that IS the -v "$HERE":/kvp-scripts:ro mapping).
    FILE_SCRIPT=""
    for a in "$@"; do
      case "$a" in
        /kvp-scripts/*) FILE_SCRIPT="${KVP_TEST_SCRIPTS_DIR:-/nonexistent}/${a#/kvp-scripts/}" ;;
        *_incontainer_build.sh|*/verify.py) FILE_SCRIPT="$a" ;;
      esac
    done
    if [ -n "$FILE_SCRIPT" ] && [ -f "$FILE_SCRIPT" ]; then
      cat "$FILE_SCRIPT" > "$LOG_DIR/last-run-script.sh"
    elif [ "$INTERACTIVE" = "1" ]; then
      cat > "$LOG_DIR/last-run-script.sh"
    else
      cat > /dev/null; : > "$LOG_DIR/last-run-script.sh"
    fi
    echo "KVP_STUB_STDIN_BYTES=$(wc -c < "$LOG_DIR/last-run-script.sh")"
    exit 0 ;;
  inspect) echo "sha256:$(printf 'a%.0s' $(seq 64))"; exit 0 ;;
  build) exit 0 ;;
  info) exit 0 ;;
  *) exit 0 ;;
esac
'''


def _stub_bin(tmp_path: pathlib.Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / "docker"
    p.write_text(DOCKER_STUB)
    p.chmod(0o755)
    g = bin_dir / "git"
    # git stub with the answers build-lmcache.sh relies on
    g.write_text("#!/usr/bin/env bash\ncase \"$*\" in\n"
                 "  *'rev-parse HEAD'*) echo 03ee5b82418b6bd44e424cdd46932d9346f43eb2 ;;\n"
                 "  *'merge-base --is-ancestor'*) exit 0 ;;\n"
                 "  *'apply --reverse --check'*) exit 1 ;;\n"
                 "  *'apply --check'*|*'apply '*) exit 0 ;;\n"
                 "  *'diff --cached'*) echo stubdiff ;;\n"
                 "  *) exit 0 ;;\nesac\n")
    g.chmod(0o755)
    return bin_dir


def _run_build_lmcache(tmp_path: pathlib.Path):
    bin_dir = _stub_bin(tmp_path)
    log_dir = tmp_path / "docker-log"; log_dir.mkdir()
    out_dir = tmp_path / "out"; out_dir.mkdir()
    build_dir = tmp_path / "build"; build_dir.mkdir()
    work = tmp_path / "work"; work.mkdir()
    src = tmp_path / "LMCache-src"
    (src / ".git").mkdir(parents=True)
    (src / "requirements").mkdir()
    (src / "requirements" / "test.txt").write_text("pytest\n")
    (src / "requirements" / "build.txt").write_text("ninja\n")
    # the script resolves SRC under WORK_DIR -- pre-create it so the clone is skipped
    src2 = build_dir / "LMCache"
    (src2 / ".git").mkdir(parents=True)
    (src2 / "requirements").mkdir()
    pdir = tmp_path / "patches"; pdir.mkdir()
    (pdir / "4136-mp-retrieve-abort.patch").write_text("diff --git a/x b/x\n")
    (pdir / "5972-incomplete-kv-objects.patch").write_text("diff --git a/y b/y\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_TEST_DOCKER_LOG=str(log_dir),
               KVP_TEST_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_OUT_DIR=str(out_dir),
               KVP_BUILD_DIR=str(build_dir),
               KVP_PATCH_DIR=str(pdir),
               IMAGE="vllm/vllm-openai:nightly-test@sha256:" + "1" * 64,
               KVP_BUILD_SKIP_TESTS="1")
    r = subprocess.run(["bash", str(BUILD_LMC)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    return r, log_dir


def _captured(log_dir: pathlib.Path) -> str:
    f = log_dir / "last-run-script.sh"
    return f.read_text() if f.is_file() else ""


# ---------------------------------------------------------------- A-3 red
def test_no_i_build_lmcache_fails_loudly(tmp_path):
    """Without a delivered script the container phase is empty: the failure
    must name the empty in-container phase, not a bare 'no wheel produced'.
    (Red before A-1: the old script died 'no wheel produced -- see output
    above' into empty output.)"""
    r, log_dir = _run_build_lmcache(tmp_path)
    assert r.returncode != 0, "empty in-container phase must not silently pass"
    out = r.stdout + r.stderr
    assert "in-container" in out, (
        f"failure must diagnose the empty in-container phase:\n{out[-800:]}"
    )


def test_build_lmcache_delivers_script(tmp_path):
    """After A-1 the real build script must reach the container (stdin with
    -i, or a mounted file) and contain the build steps."""
    r, log_dir = _run_build_lmcache(tmp_path)
    delivered = _captured(log_dir)
    alt = list((ROOT / "scripts").glob("_incontainer_build.sh"))
    if not delivered and not alt:
        pytest.fail("no in-container script reached the container at all")
    body = delivered or alt[0].read_text()
    assert "pip wheel" in body, "delivered script must be the real build phase"


# ------------------------------------------------------- build-image (A-2)
GOOD_VERIFY = """KVP_LMC_VERSION=0.5.7.dev428+kvp.deadbeefcafe
KVP_TORCH_VERSION=2.4.0
KVP_TORCH_CUDA=13.0.2
KVP_PYTHON_VERSION=3.12.3
KVP_CUDA_AVAILABLE=True
KVP_GPU_COUNT=4
KVP_PY_IMPORTS=OK
KVP_CONN_MODULE=/usr/lib/x.py
KVP_TRANSFER_MODULE=/usr/lib/y.py
KVP_CUDA_OPS_IMPORT=OK
KVP_GPU_SMOKE[0] dev=0000:01:00.0 cc=12.0 OK
KVP_GPU_SMOKE[1] dev=0000:41:00.0 cc=12.0 OK
KVP_GPU_SMOKE[2] dev=0000:81:00.0 cc=12.0 OK
KVP_GPU_SMOKE[3] dev=0000:c1:00.0 cc=12.0 OK
KVP_GPU_SMOKE_OK_COUNT=4
KVP_GPU_CC=12.0
KVP_IDENTITY_FILE=OK
KVP_IDENTITY_BASE=03ee5b82418b6bd44e424cdd46932d9346f43eb2
KVP_IDENTITY_P4136=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
KVP_IDENTITY_P5972=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
"""


def _run_build_image(tmp_path: pathlib.Path, run_mode: str = "good"):
    """run_mode: good | empty | partial."""
    bin_dir = _stub_bin(tmp_path)
    log_dir = tmp_path / "docker-log"; log_dir.mkdir()
    out_dir = tmp_path / "out"; out_dir.mkdir()
    build_dir = tmp_path / "build"; build_dir.mkdir()
    patch_dir = tmp_path / "patches"; patch_dir.mkdir()
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text("diff --git a/x b/x\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text("diff --git a/y b/y\n")
    whl = build_dir / "wheels"; whl.mkdir(parents=True)
    (whl / "lmcache-0.5.7.dev428+kvp.abc.whl").write_bytes(b"PK\x03\x04fake")
    import json as _json
    (build_dir / "provenance.json").write_text(_json.dumps(
        {"wheel_version": "0.5.7.dev428+kvp.deadbeefcafe", "module": "custom-lmcache-build"}))
    # stub variant: what `docker run` (python3 -) prints to verify.txt
    body = GOOD_VERIFY if run_mode == "good" else ""
    verify_body = tmp_path / "verify-body.txt"
    verify_body.write_text(body)
    (bin_dir / "docker").write_text(
        DOCKER_STUB.replace(
            'echo "KVP_STUB_STDIN_BYTES=$(wc -c < "$LOG_DIR/last-run-script.sh")"',
            'echo "KVP_STUB_STDIN_BYTES=$(wc -c < "$LOG_DIR/last-run-script.sh")"; '
            f'cat "{verify_body}"'),
    )
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_TEST_DOCKER_LOG=str(log_dir),
               KVP_BUILD_DIR=str(build_dir),
               KVP_PATCH_DIR=str(patch_dir),
               KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
               KVP_OUT_DIR=str(out_dir),
               IMAGE="vllm/vllm-openai:nightly-test@sha256:" + "1" * 64)
    r = subprocess.run(["bash", str(BUILD_IMG)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    return r, out_dir


def test_build_image_empty_verify_refuses(tmp_path):
    """A-2: empty verify.txt (the r6-7 bug) must REFUSE -- no live/image-identity,
    no live/image, clear message."""
    r, out_dir = _run_build_image(tmp_path, run_mode="empty")
    assert r.returncode != 0, (
        f"empty verify output must refuse the build, got rc={r.returncode}:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}"
    )
    assert not (out_dir / "image-identity.env").exists(), (
        "image-identity.env must NOT be written on an empty verify"
    )
    assert not (out_dir / "image").exists(), (
        "live/image must NOT be written on an empty verify"
    )


def test_build_image_good_verify_passes_and_writes_identity(tmp_path):
    """With a complete verify output the build passes and records the
    identity; live/image (the custom image id) is written AFTER success."""
    r, out_dir = _run_build_image(tmp_path, run_mode="good")
    assert r.returncode == 0, (
        f"complete verify must pass: rc={r.returncode}\n{r.stderr[-800:]}"
    )
    assert (out_dir / "image-identity.env").is_file()
    # live/image is written by the B-2 step (next commit)