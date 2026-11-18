# SPDX-License-Identifier: Apache-2.0
"""r6-7 B-2: build-image.sh records the custom image id in live/image AFTER
the verify gate; start-all.sh picks it up when IMAGE is empty; a failed smoke
must NEVER create or overwrite live/image.

Tests run the REAL scripts with the real-docker stub; all writes go through
KVP_LIVE_DIR (never the repo's live/).
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_IMG = ROOT / "scripts" / "build-image.sh"
START_ALL = ROOT / "scripts" / "start-all.sh"
PIN_FILE = ROOT / "vendor" / "pinned-base-image.env"

GOOD_VERIFY = """KVP_LMC_VERSION=0.5.7.dev428+kvp.deadbeefcafe
KVP_PY_IMPORTS=OK
KVP_CUDA_OPS_IMPORT=OK
KVP_IDENTITY_FILE=OK
KVP_GPU_COUNT=4
KVP_GPU_SMOKE_OK_COUNT=4
"""


def _run_build_image(tmp_path: pathlib.Path, verify_body: str, live_dir=None):
    live_dir = live_dir or (tmp_path / "live")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    vbody = tmp_path / "verify-body.txt"
    vbody.write_text(verify_body)
    (bin_dir / "docker").write_text(f'''#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
case "$cmd" in
  run) cat > /dev/null; cat "{vbody}"; exit 0 ;;
  inspect) echo "sha256:$(printf 'a%.0s' $(seq 64))"; exit 0 ;;
  build) exit 0 ;;
  *) exit 0 ;;
esac
''')
    (bin_dir / "docker").chmod(0o755)
    build_dir = tmp_path / "build"
    (build_dir / "wheels").mkdir(parents=True, exist_ok=True)
    (build_dir / "wheels" / "lmcache-0.5.7.dev428+kvp.abc.whl").write_bytes(b"PK\x03\x04fake")
    (build_dir / "provenance.json").write_text(json.dumps(
        {"wheel_version": "0.5.7.dev428+kvp.deadbeefcafe", "module": "x"}))
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir(exist_ok=True)
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text("a\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text("b\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_BUILD_DIR=str(build_dir), KVP_PATCH_DIR=str(patch_dir),
               KVP_IDENTITY_OUT=str(tmp_path / "image-identity.env"),
               KVP_OUT_DIR=str(tmp_path),
               KVP_LIVE_DIR=str(live_dir),
               KVP_PIN_FILE=str(PIN_FILE))
    for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
        env.pop(var, None)
    r = subprocess.run(["bash", str(BUILD_IMG)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    return r


def test_live_image_recorded_after_success(tmp_path):
    r = _run_build_image(tmp_path, GOOD_VERIFY)
    assert r.returncode == 0, r.stderr[-600:]
    img = tmp_path / "live" / "image"
    assert img.is_file(), "live/image must be recorded after a successful build"
    val = img.read_text().strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", val), (
        f"live/image must hold sha256:<64 hex>, got {val!r}"
    )


def test_failed_smoke_never_touches_live_image(tmp_path):
    """Empty verify (failed smoke): live/image must NOT be created or
    overwritten -- an old record stays byte-identical."""
    live_dir = tmp_path / "live"
    live_dir.mkdir(exist_ok=True)
    old = f"sha256:{'d' * 64}\n"
    (live_dir / "image").write_text(old)
    r = _run_build_image(tmp_path, "", live_dir=live_dir)
    assert r.returncode != 0, "an empty verify must refuse"
    assert (live_dir / "image").read_text() == old, (
        "a failed smoke must never overwrite live/image"
    )
    # and no record at all when there was none
    live2 = tmp_path / "live2"
    r2 = _run_build_image(tmp_path, "", live_dir=live2)
    assert r2.returncode != 0
    assert not (live2 / "image").exists(), (
        "a failed smoke must never create live/image"
    )


def test_start_all_uses_live_image_without_env(tmp_path):
    """start-all.sh with empty IMAGE must resolve $KVP_LIVE_DIR/image."""
    live_dir = tmp_path / "live"
    live_dir.mkdir(exist_ok=True)
    want = f"sha256:{'e' * 64}"
    (live_dir / "image").write_text(want + "\n")
    # extract just the resolution behaviour: run the script to the first die
    # (no docker/engines available) and require the resolved IMAGE in output
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/usr/bin/env bash\ncase \"$1\" in ps) exit 0 ;; *) exit 1 ;; esac\n")
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_LIVE_DIR=str(live_dir),
               KVP_PIN_FILE=str(PIN_FILE))
    env.pop("IMAGE", None)
    r = subprocess.run(["bash", str(START_ALL)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    # either the run proceeds with the resolved image, or it refuses for an
    # unrelated reason -- but never with 'no IMAGE set'
    assert "no IMAGE set and no" not in out, (
        f"start-all must pick up live/image without manual IMAGE:\n{out[-800:]}"
    )