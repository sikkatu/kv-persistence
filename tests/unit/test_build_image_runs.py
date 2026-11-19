# SPDX-License-Identifier: Apache-2.0
"""r3 P0-1: build-image.sh must generate its Dockerfile without unbound vars.

The Dockerfile heredoc referenced $PR4136_SHA/$PR5972_SHA BEFORE they were
computed, and under `set -Eeuo pipefail` that aborts the script on the very
first run with `unbound variable`. Static tests reading the script text do not
catch it -- only actually executing it does (r3 rule: behavioural test first).

These tests run the real build-image.sh with a stub `docker` on PATH and a
fixture wheel/provenance/patch set in a tmp dir (KVP_PATCH_DIR), asserting:
  * rc == 0 (a first-run invocation must not crash)
  * the generated Dockerfile carries non-empty 64-hex LABELs for both patches
  * the LABELs equal the sha256 of the actual patch files
  * kvp-lmcache-identity.json carries the same sha values
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-image.sh"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_fixture(tmp_path: pathlib.Path):
    """Fixture patch dir + wheel + provenance + live output dir, all under tmp.
    r18 G2: the provenance is written like the REAL build-lmcache.sh writes it
    (wheel_sha256 + both patch SHAs + tests_skipped) so the new integrity
    checks pass on the consistent pair."""
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    p4136 = patch_dir / "4136-mp-retrieve-abort.patch"
    p5972 = patch_dir / "5972-incomplete-kv-objects.patch"
    p4136.write_bytes(b"PATCH4136-CONTENT\n")
    p5972.write_bytes(b"PATCH5972-CONTENT\n")

    build_dir = tmp_path / "build"
    wheels = build_dir / "wheels"
    wheels.mkdir(parents=True)
    whl = wheels / "lmcache-0.5.8rc1.post48-cp312-cp312-linux_x86_64.whl"
    whl.write_bytes(b"PK\x05\x06fake-wheel-content")
    (build_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "wheel": whl.name,
        "wheel_version": "0.5.8rc1.post48+kvp.0123456789ab",
        "wheel_sha256": _sha256(whl),
        "pr4136_patch_sha256": _sha256(p4136),
        "pr5972_patch_sha256": _sha256(p5972),
        "tests_skipped": "0",
    }))

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return patch_dir, build_dir, out_dir, p4136, p5972, whl


def make_docker_stub(bin_dir: pathlib.Path, dockerfile_store: pathlib.Path):
    """Stub docker that records generated artifacts and always succeeds."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_store.mkdir(parents=True, exist_ok=True)

    stub = bin_dir / "docker"
    stub.write_text(f"""#!/usr/bin/env bash
cmd="$1"; shift || true
case "$cmd" in
  build)
    # docker build -t <img> <CONTEXT_DIR>: context dir is the fixture TMPD
    for a in "$@"; do
      case "$a" in
        -t) shift ;;
        *) CTX="$a" ;;
      esac
    done
    mkdir -p "{dockerfile_store}"
    [ -f "$CTX/Dockerfile" ] && cp "$CTX/Dockerfile" "{dockerfile_store}/Dockerfile"
    [ -f "$CTX/kvp-lmcache-identity.json" ] && cp "$CTX/kvp-lmcache-identity.json" "{dockerfile_store}/kvp-lmcache-identity.json"
    exit 0 ;;
  inspect) echo sha256:{"0" * 56}abcdef; exit 0 ;;
  run) echo "KVP_LMC_VERSION=stub
KVP_PY_IMPORTS=OK
KVP_CUDA_OPS_IMPORT=OK\nKVP_NATIVE_IMPORT=OK
KVP_IDENTITY_FILE=OK
KVP_GPU_COUNT=4
KVP_GPU_SMOKE_OK_COUNT=4"; exit 0 ;;
  *) exit 0 ;;
esac
""")
    stub.chmod(0o755)
    return stub


def _run_build_image(tmp_path: pathlib.Path, patch_dir, build_dir, out_dir, extra_env=None):
    bin_dir = tmp_path / "bin"
    store = tmp_path / "docker-store"
    make_docker_stub(bin_dir, store)

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        KVP_PATCH_DIR=str(patch_dir),
        KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"),
        KVP_BUILD_DIR=str(build_dir),
        KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
        KVP_OUT_DIR=str(out_dir),
        IMAGE=f"vllm/vllm-openai:nightly-test@sha256:{'1' * 64}",
    )
    if extra_env:
        env.update(extra_env)

    r = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(tmp_path),
    )
    return r, store


def test_first_run_does_not_crash(tmp_path):
    """P0-1 repro: before the fix, `bash build-image.sh` died with
    'PR4136_SHA: unbound variable' (heredoc used the VAR before computing it)."""
    patch_dir, build_dir, out_dir, *_ = make_fixture(tmp_path)
    r, _store = _run_build_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, (
        f"build-image.sh must not crash on first run -- got rc={r.returncode}:\n"
        f"stdout: {r.stdout[-1200:]}\nstderr: {r.stderr[-1200:]}"
    )


def test_dockerfile_labels_are_64_hex_and_match_patches(tmp_path):
    patch_dir, build_dir, out_dir, p4136, p5972, _ = make_fixture(tmp_path)
    r, store = _run_build_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-1200:]
    df = store / "Dockerfile"
    assert df.is_file(), "Dockerfile was not captured"
    text = df.read_text()
    for key, patch in [
        ("org.kvp.lmc-patch-4136", p4136),
        ("org.kvp.lmc-patch-5972", p5972),
    ]:
        m = re.search(rf'{key}="([0-9a-f]+)"', text)
        assert m, f"LABEL {key} missing in Dockerfile"
        val = m.group(1)
        assert HEX64.match(val), f"{key}={val!r} is not 64 hex"
        assert val == _sha256(patch), (
            f"{key}={val} != sha256 of {patch.name} ({_sha256(patch)})"
        )


def test_identity_json_carries_same_sha(tmp_path):
    patch_dir, build_dir, out_dir, p4136, p5972, _ = make_fixture(tmp_path)
    r, store = _run_build_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-1200:]
    ident = store / "kvp-lmcache-identity.json"
    assert ident.is_file(), "identity JSON was not captured"
    data = json.loads(ident.read_text())
    assert data["pr4136_patch_sha256"] == _sha256(p4136)
    assert data["pr5972_patch_sha256"] == _sha256(p5972)
    assert isinstance(data.get("wheel_sha256"), str) and HEX64.match(data["wheel_sha256"])


def _mutate_revert_order(tmp_path: pathlib.Path) -> str:
    """Write a mutated copy of build-image.sh where the Dockerfile heredoc
    comes BEFORE the SHA computation lines (the pre-fix ordering). Returns
    the path to the mutated script."""
    src = SCRIPT.read_text()
    # The defect heredoc block: extract the "PATCH SHA" section, move it
    # AFTER the Dockerfile cat to recreate the original bug.
    lines = src.splitlines(keepends=True)
    sha_start = sha_end = df_start = df_end = None
    for i, ln in enumerate(lines):
        if "PR4136_SHA=" in ln and "sha256sum" in ln and sha_start is None:
            sha_start = i
        if sha_start is not None and sha_end is None and ln.strip() == "EOF" and i > sha_start + 8:
            sha_end = i + 1
        if ln.startswith("cat > \"$TMPD/Dockerfile\"") and df_start is None:
            df_start = i
    if df_start is not None:
        j = df_start
        while j < len(lines):
            if lines[j].strip() == "EOF" and j > df_start:
                df_end = j + 1
                break
            j += 1
    assert sha_start is not None and sha_end is not None, "SHA block not found"
    assert df_start is not None and df_end is not None, "Dockerfile block not found"
    # Move Dockerfile block BEFORE sha block
    sha_block = lines[sha_start:sha_end]
    df_block = lines[df_start:df_end]
    new_lines = lines[:sha_start] + df_block + sha_block + lines[sha_end:]
    mutant = tmp_path / "build-image-mutant.sh"
    mutant.write_text("".join(new_lines))
    mutant.chmod(0o755)
    return mutant


def test_mutation_detector_reverts_order(tmp_path):
    """Mutation check: restoring the old (broken) order must make the test red.

    We rebuild the mutant, run the SAME assertions, and require rc != 0
    (the unbound-variable abort). The test documents the repro."""
    mutant = _mutate_revert_order(tmp_path)
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    patch_dir, build_dir, out_dir, *_ = make_fixture(fx)
    bin_dir = tmp_path / "bin"
    make_docker_stub(bin_dir, tmp_path / "dst")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        KVP_PATCH_DIR=str(patch_dir),
        KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"),
        KVP_BUILD_DIR=str(build_dir),
        KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
        KVP_OUT_DIR=str(out_dir),
        IMAGE=f"x@sha256:{'a' * 64}",
    )
    r = subprocess.run(
        ["bash", str(mutant)], capture_output=True, text=True, timeout=30,
        env=env, cwd=str(tmp_path),
    )
    assert r.returncode != 0, (
        "mutation detector: restoring the Dockerfile-before-SHA order must crash"
    )
    assert "unbound" in r.stderr or "PR4136_SHA" in r.stderr, (
        f"expected unbound variable error, got: {r.stderr[-400:]}"
    )