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

GOOD_VERIFY = """KVP_LMC_VERSION=0.5.8rc1.post48+kvp.deadbeefcafe
KVP_PY_IMPORTS=OK
KVP_CUDA_OPS_IMPORT=OK\nKVP_NATIVE_IMPORT=OK\nKVP_ZSTD_IMPORT=OK\nKVP_LZ4_IMPORT=OK
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
  run)
    case "$*" in
      *_smoke_cache_server*)
        echo "KVP_SMOKE_ZSTD_READY=OK"; echo "KVP_SMOKE_ZSTD_EXIT=0"
        echo "KVP_SMOKE_NONE_READY=OK"; echo "KVP_SMOKE_NONE_EXIT=0"
        exit 0 ;;
    esac
    cat > /dev/null; cat "{vbody}"; exit 0 ;;
  inspect) echo "sha256:$(printf 'a%.0s' $(seq 64))"; exit 0 ;;
  build) exit 0 ;;
  *) exit 0 ;;
esac
''')
    (bin_dir / "docker").chmod(0o755)
    build_dir = tmp_path / "build"
    (build_dir / "wheels").mkdir(parents=True, exist_ok=True)
    whl = build_dir / "wheels" / "lmcache-0.5.8rc1.post48+kvp.abc.whl"
    whl.write_bytes(b"PK\x03\x04fake")
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir(exist_ok=True)
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text("a\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text("b\n")
    import hashlib
    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    # r18 G2: provenance is written like the REAL build (wheel_sha256 + both
    # patch SHAs + tests_skipped) so the integrity checks pass
    (build_dir / "provenance.json").write_text(json.dumps({
        "wheel_version": "0.5.8rc1.post48+kvp.deadbeefcafe",
        "module": "custom-lmcache-build",
        "wheel": whl.name,
        "wheel_sha256": _sha(whl),
        "pr4136_patch_sha256": _sha(patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(patch_dir / "5972-incomplete-kv-objects.patch"),
        "tests_skipped": "0",
    }))
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
    """start-all.sh with empty IMAGE must resolve $KVP_LIVE_DIR/image AND
    hand that EXACT id to the recipe launcher (r20 K4.5: the old test
    accepted ANY outcome except one string -- a wrong-image launch passed
    green; the K1 defect (stock IMAGE shadowing live/image) slipped through
    exactly that hole). The stub recipe prints RECIPE_SAW_IMAGE=$IMAGE."""
    live_dir = tmp_path / "live"
    live_dir.mkdir(exist_ok=True)
    want = f"sha256:{'e' * 64}"
    (live_dir / "image").write_text(want + "\n")
    # the stub recipe launcher printing what IMAGE it received
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    recipe_dir = home / "sikkatu-recipes" / "recipes" / "rtx-pro-6000" / \
        "mimo-v2.6-pro-hotsplit"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "scripts").mkdir(exist_ok=True)
    (recipe_dir / "scripts" / "run-tp4-hotsplit.sh").write_text(
        '#!/usr/bin/env bash\necho "RECIPE_SAW_IMAGE=$IMAGE"\nexit 0\n')
    (recipe_dir / "scripts" / "run-tp4-hotsplit.sh").chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # r20 K4.5: the docker stub must answer the DEEP custom-image probes
    # (labels of a real build + the baked identity + the python probes) --
    # otherwise the resolver refuses the image and the launcher never runs
    import hashlib
    import json as _json
    kv = tmp_path / "kv"
    (kv / "vendor" / "patches" / "lmcache").mkdir(parents=True)
    p4136 = kv / "vendor" / "patches" / "lmcache" / "4136-mp-retrieve-abort.patch"
    p5972 = kv / "vendor" / "patches" / "lmcache" / "5972-incomplete-kv-objects.patch"
    p4136.write_bytes(b"PATCH4136-CONTENT\n")
    p5972.write_bytes(b"PATCH5972-CONTENT\n")

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    # the resolver verifies the labels against $ROOT/vendor/patches/lmcache
    # (the REAL repo patches) -- use THOSE shas in the labels
    real_pd = ROOT / "vendor" / "patches" / "lmcache"
    labels = _json.dumps({
        "org.kvp.lmc-base": f"x@sha256:{'b' * 64}",
        "org.kvp.lmc-patch-4136": _sha(real_pd / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(real_pd / "5972-incomplete-kv-objects.patch"),
    })
    ident = _json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(real_pd / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(real_pd / "5972-incomplete-kv-objects.patch"),
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
    _codec=""
    while [ "$#" -gt 0 ]; do case "$1" in
      --entrypoint) _entry="$2"; shift 2 ;;
      -c) _probe="$2"; shift 2 ;;
      zstd|none) _codec="$1"; shift ;;
      *) shift ;;
    esac; done
    case "$_entry" in
      cat) echo '{ident}' ;;
      bash)
        [ -n "$_codec" ] || _codec=none
        case "$_codec" in
          zstd) echo "KVP_SMOKE_ZSTD_READY=OK"; echo "KVP_SMOKE_ZSTD_EXIT=0" ;;
          *)    echo "KVP_SMOKE_NONE_READY=OK"; echo "KVP_SMOKE_NONE_EXIT=0" ;;
        esac ;;
      python3)
        case "$_probe" in
          *importlib.metadata*) echo "0.5.8rc1.post48+kvp.60dce759ea93" ;;
          *find_spec*) exit 0 ;;
          *) exit 0 ;;
        esac ;;
    esac ;;
  inspect) echo "{want}" ;;
  ps) echo "obs_otel_collector"; echo "obs_tempo"; echo "obs_prometheus"; echo "obs_grafana" ;;
  compose) exit 0 ;;
  *) exit 0 ;;
esac
''')
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               HOME=str(home),
               KVP_LIVE_DIR=str(live_dir),
               RECIPE_DIR=str(recipe_dir),
               KVP_PIN_FILE=str(PIN_FILE),
               KVP_PREFLIGHTS_DONE="1",
               KVP_SKIP_IPC_TEST="1",
               # the IMAGE resolution is the subject -- the service steps
               # must not be: report obs already running (docker ps) and
               # disable the extras deploy
               KVP_OBS_EXTRAS="0")
    # the subject is the IMAGE resolution, not the service steps: make
    # step 2/3 quick -- an already-running cache (live pid) and a live TCP
    # port for the pre-flight probe
    import socket
    _srv = socket.socket()
    _srv.bind(("127.0.0.1", 0))
    _srv.listen(1)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "lmcache-server.pid").write_text(str(os.getpid()))
    env["KVP_CACHE_ROOT"] = str(cache_root)
    env["LMCACHE_HOST"] = "127.0.0.1"
    env["LMCACHE_PORT"] = str(_srv.getsockname()[1])
    env["KV_PERSIST_HOST"] = "127.0.0.1"
    env["KV_PERSIST_PORT"] = str(_srv.getsockname()[1])
    # MODEL_DIR resolution is not the subject -- give the chain one
    _model_dir = tmp_path / "models" / "MiMo"
    _model_dir.mkdir(parents=True, exist_ok=True)
    (_model_dir / "config.json").write_text('{"hidden_size": 8}\n')
    env["MODEL_DIR"] = str(_model_dir)
    env.pop("IMAGE", None)
    try:
        r = subprocess.run(["bash", str(START_ALL)], capture_output=True,
                           text=True, timeout=120, env=env, cwd=str(tmp_path))
    finally:
        _srv.close()
    out = r.stdout + r.stderr
    assert "no IMAGE set and no" not in out, (
        f"start-all must pick up live/image without manual IMAGE:\n{out[-800:]}")
    # r20 K4.5: the RESOLVED id must reach the recipe launcher exactly
    assert f"RECIPE_SAW_IMAGE={want}" in out, (
        f"the recipe must see the live/image id {want}:\n{out[-1200:]}")