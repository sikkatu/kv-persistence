# SPDX-License-Identifier: Apache-2.0
"""r20 K6: the lossless L2 codecs (zstandard, lz4) must ship IN the image.

Hardware defect (operator log 20 Nov 2026, ~12:40): start-cache.sh died on
step 2 with `ModuleNotFoundError: No module named 'zstandard'` from
check_codec_available -- 'lossless codec zstd is not available inside the
docker image'. zstd is the DEFAULT codec, so the default docker path could
never have worked: the codecs were described in requirements-lmcache.txt
for the host-venv only and were never in the image (the Dockerfile installs
the wheel with --no-deps).

Fixes under test:
  1. build-image.sh generates requirements-image-codecs.txt into the build
     context (exact == pins + --hash=sha256 for cp312 manylinux x86_64
     wheels from PyPI, ranges in requirements-lmcache.txt respected) and the
     Dockerfile COPYs it + installs with --no-deps --only-binary=:all:
     --require-hashes in the same RUN;
  2. _verify_image.py emits strict markers KVP_ZSTD_IMPORT=OK and
     KVP_LZ4_IMPORT=OK (import + version); without them build-image.sh
     refuses and live/image is not updated;
  3. the identity (kvp-lmcache-identity.json + image-identity.env) carries
     image_codecs_sha256 / KVP_IMAGE_CODECS_SHA256 and the pinned versions.

Honest test limit (stated in the report): that `pip` REALLY installs the
packages into the image is only proven by hardware (the markers of point 2
and the server smoke); here we verify the Dockerfile wiring, the hash
format and the strict markers.

Tests (REAL build-image.sh with the shared docker stub):
  - the Dockerfile: COPY requirements-image-codecs.txt, the RUN installs it
    with --require-hashes; every line in the file is `==` + `--hash=sha256:`
    of 64 hex;
  - mutations: drop --require-hashes / drop the `==` pin / drop a package
    from the file / drop the COPY / cut the codec markers from the verify
    output -> each RED;
  - start-cache.sh with a docker stub whose codec probe fails with
    ModuleNotFoundError -> rc != 0, the text names build-image.sh and does
    NOT say 'install the codec into the image'.
"""
from __future__ import annotations
import os
import pathlib
import re
import shlex
import subprocess

import pytest

import test_build_image_runs as base

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-image.sh"
START_CACHE = ROOT / "scripts" / "start-cache.sh"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: pathlib.Path):
    """A consistent wheel+provenance pair (r18 G2 format)."""
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
    (build_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "wheel": whl.name,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
        "wheel_sha256": _sha256(whl),
        "pr4136_patch_sha256": _sha256(p4136),
        "pr5972_patch_sha256": _sha256(p5972),
        "tests_skipped": "0",
    }))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return patch_dir, build_dir, out_dir, whl


def _run_image(tmp_path: pathlib.Path, patch_dir, build_dir, out_dir,
               script: pathlib.Path = SCRIPT,
               verify_extra: str = ("KVP_ZSTD_IMPORT=OK\nKVP_ZSTD_VERSION=0.25.0\n"
                                    "KVP_LZ4_IMPORT=OK\nKVP_LZ4_VERSION=4.4.5\n"),
               smoke_extra: str | None = None):
    bin_dir = tmp_path / "bin"
    store = tmp_path / "docker-store"
    base.make_docker_stub(bin_dir, store)
    # r20 K6: the verify run must carry the codec markers -- the strict gate
    # in build-image.sh refuses without them (a realistic verify output, with
    # the r19 H2 noise-free KEY=value lines only)
    (bin_dir / "docker").write_text(f'''#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect) echo '{{"org.kvp.lmc-base":"x@sha256:{"1" * 64}","org.kvp.lmc-patch-4136":"{"2" * 64}","org.kvp.lmc-patch-5972":"{"3" * 64}"}}' ;;
    esac ;;
  run)
    _entry=""
    _script=""
    _codec=""
    while [ "$#" -gt 0 ]; do case "$1" in
      --entrypoint) _entry="$2"; shift 2 ;;
      *verify_image*) _script="$1"; shift ;;
      zstd|none) _codec="$1"; shift ;;
      *) shift ;;
    esac; done
    case "$_entry" in
      cat) echo '{{"module":"kvp-custom-lmcache-identity","lmcache_source_commit":"07f1c6bb642f4772606a36eab730ae009ddce08f","pr4136_patch_sha256":"{"2" * 64}","pr5972_patch_sha256":"{"3" * 64}","wheel":"lmcache-0.5.8rc1.post48+kvp.60dce759ea93-cp312-cp312-linux_x86_64.whl","wheel_sha256":"{"4" * 64}","wheel_version":"0.5.8rc1.post48+kvp.60dce759ea93"}}' ;;
      bash)
        # r20 K6.3: the server smoke (bash _smoke_cache_server.sh <codec> ...)
        [ -n "$_codec" ] || _codec=none
        if [ "$KVP_SMOKE_STUB_OUTPUT" = "USE_DEFAULT" ]; then
          case "$_codec" in
            zstd) echo "KVP_SMOKE_ZSTD_READY=OK"; echo "KVP_SMOKE_ZSTD_EXIT=0" ;;
            *)    echo "KVP_SMOKE_NONE_READY=OK"; echo "KVP_SMOKE_NONE_EXIT=0" ;;
          esac
        else
          printf '%s\n' "$KVP_SMOKE_STUB_OUTPUT"
        fi ;;
      python3)
        if [ -n "$_script" ]; then
          # the verify run: the markers the strict gate checks (r19 H2 noise
          # format: clean KEY=value lines only)
          cat <<'VERIFYEOF'
KVP_LMC_VERSION=0.5.8rc1.post48+kvp.60dce759ea93
KVP_PY_IMPORTS=OK
KVP_CUDA_OPS_IMPORT=OK
KVP_NATIVE_IMPORT=OK
VERIFYEOF
          printf '%s\n' "$KVP_VERIFY_STUB_OUTPUT"
          cat <<'VERIFYEOF2'
KVP_IDENTITY_FILE=OK
KVP_IDENTITY_BASE=07f1c6bb642f4772606a36eab730ae009ddce08f
KVP_GPU_COUNT=4
KVP_GPU_SMOKE_OK_COUNT=4
KVP_GPU_CC=12.0
VERIFYEOF2
        else
          echo "0.5.8rc1.post48+kvp.60dce759ea93"
        fi ;;
    esac ;;
  build)
    for a in "$@"; do case "$a" in -t) shift ;; *) CTX="$a" ;; esac; done
    mkdir -p "{store}"
    [ -f "$CTX/Dockerfile" ] && cp "$CTX/Dockerfile" "{store}/Dockerfile"
    [ -f "$CTX/kvp-lmcache-identity.json" ] && cp "$CTX/kvp-lmcache-identity.json" "{store}/kvp-lmcache-identity.json"
    [ -f "$CTX/requirements-image-codecs.txt" ] && cp "$CTX/requirements-image-codecs.txt" "{store}/requirements-image-codecs.txt"
    exit 0 ;;
  inspect) echo "sha256:{"0" * 56}abcdef" ;;
  *) exit 0 ;;
esac
''')
    (bin_dir / "docker").chmod(0o755)
    # the verify run must print the markers the gate checks -- simulate the
    # in-image _verify_image.py output (r20 K6 codec markers included)
    (bin_dir / "verify-stub").write_text("true\n")
    (bin_dir / "verify-stub").chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        KVP_PATCH_DIR=str(patch_dir),
        KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"),
        KVP_BUILD_DIR=str(build_dir),
        KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
        KVP_LIVE_DIR=str(out_dir / "live"),
        IMAGE=f"vllm/vllm-openai:nightly-test@sha256:{'1' * 64}",
        KVP_VERIFY_STUB_OUTPUT=verify_extra,
        KVP_SMOKE_STUB_OUTPUT=("USE_DEFAULT" if smoke_extra is None else smoke_extra),
    )
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    return r, store


def _logical_lines(dockerfile: str) -> list[str]:
    out, buf = [], ""
    for ln in dockerfile.splitlines():
        s = ln.rstrip()
        if s.endswith("\\"):
            buf += s[:-1] + " "
        else:
            buf += s
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def check_codecs(store: pathlib.Path) -> list[str]:
    """Structural check of the codec wiring in the generated Dockerfile:
    COPY of the requirements file; the install RUN carries --require-hashes
    and -r on that path; every requirements line is `==` + `--hash=sha256:`
    of 64 hex. Returns violations (empty = clean)."""
    df = (store / "Dockerfile").read_text()
    viol = []
    lines = _logical_lines(df)
    req_name = "requirements-image-codecs.txt"

    # -- COPY of the requirements file must exist
    copy_lines = [ln for ln in lines if ln.startswith("COPY ")]
    req_copy = [ln for ln in copy_lines if req_name in ln]
    if not req_copy:
        viol.append(f"no COPY of {req_name} in the Dockerfile")

    # -- the install RUN must use --require-hashes -r <that path>
    install_parts = []
    for ln in lines:
        if ln.startswith("RUN "):
            for part in ln[4:].split("&&"):
                part = part.strip()
                if part:
                    install_parts.append(shlex.split(part))
    codec_install = [a for a in install_parts
                     if "pip" in a and "install" in a and req_name in " ".join(a)]
    if not codec_install:
        viol.append(f"no pip install of {req_name} in the Dockerfile RUN block")
    else:
        argv = codec_install[0]
        if "--require-hashes" not in argv:
            viol.append("codec install lacks --require-hashes (pip would "
                        "accept unpinned files)")
        if "--only-binary=:all:" not in argv and "--only-binary" not in argv:
            viol.append("codec install lacks --only-binary=:all: (an sdist "
                        "could be built at image-build time)")
        if "-r" not in argv:
            viol.append("codec install lacks -r (requirements file not used)")

    # -- the generated requirements file (captured from the build context)
    req = store / req_name
    if not req.is_file():
        # the docker stub saves Dockerfile + identity; the requirements file
        # lives in the same build context (TMPD) -- fall back to the repo's
        # tracked copy (identical content, r20 K6)
        req = ROOT / req_name
    if not req.is_file():
        viol.append(f"{req_name} not found (build context or repo)")
        return viol
    pkgs = []
    for ln in req.read_text().splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        pkgs.append(s)
        if "==" not in s.split()[0]:
            viol.append(f"line not pinned with ==: {s!r}")
        m = re.search(r"--hash=sha256:([0-9a-f]+)", s)
        if not m or not HEX64.match(m.group(1)):
            viol.append(f"line lacks a valid --hash=sha256:<64 hex>: {s!r}")
    names = " ".join(pkgs)
    if "zstandard==" not in names:
        viol.append("zstandard is not pinned in the codec requirements")
    if "lz4==" not in names:
        viol.append("lz4 is not pinned in the codec requirements")
    return viol


def test_dockerfile_installs_codecs_with_require_hashes(tmp_path):
    """(1) the generated Dockerfile must COPY the codec requirements and
    install them with --require-hashes; the file itself carries == pins with
    valid sha256 hashes."""
    patch_dir, build_dir, out_dir, whl = _fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, (
        f"the build must reach docker build:\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    viol = check_codecs(store)
    assert not viol, "codec wiring violations:\n" + "\n".join(viol)


def test_verify_markers_require_codec_imports():
    """(2) _verify_image.py must emit the strict codec markers and
    build-image.sh must gate on them."""
    verify = (ROOT / "scripts" / "_verify_image.py").read_text()
    assert "KVP_ZSTD_IMPORT=OK" in verify, (
        "_verify_image.py must emit the zstd import marker")
    assert "KVP_LZ4_IMPORT=OK" in verify, (
        "_verify_image.py must emit the lz4 import marker")
    assert "import zstandard" in verify, verify[:500]
    assert "import lz4" in verify, verify[:500]
    bi = SCRIPT.read_text()
    assert "KVP_ZSTD_IMPORT=OK" in bi and "KVP_LZ4_IMPORT=OK" in bi, (
        "build-image.sh must gate on the codec markers")
    # the identity carries the codec pins
    assert "image_codecs_sha256" in bi, (
        "the baked identity must record the codec requirements sha")
    assert "KVP_IMAGE_CODECS_SHA256" in bi, (
        "image-identity.env must record the codec requirements sha")


# ---------------------------------------------------------------------------
# mutations -- each makes check_codecs / the marker gate report the problem
# ---------------------------------------------------------------------------
def _mutant_store(tmp_path: pathlib.Path, mutate) -> tuple:
    patch_dir, build_dir, out_dir, whl = _fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-800:]
    df = store / "Dockerfile"
    df.write_text(mutate(df.read_text()))
    return store, whl.name


def test_mutation_drop_require_hashes(tmp_path):
    """Mutation: drop --require-hashes from the codec install -> reported."""
    def mutate(text: str) -> str:
        return text.replace("--require-hashes ", "")
    store, _ = _mutant_store(tmp_path, mutate)
    viol = check_codecs(store)
    assert any("--require-hashes" in v for v in viol), viol


def test_mutation_unpinned_line(tmp_path):
    """Mutation: drop the `==` pin from zstandard in the requirements file ->
    reported (the pin would float)."""
    req = ROOT / "requirements-image-codecs.txt"
    orig = req.read_text()
    try:
        req.write_text(orig.replace("zstandard==0.25.0", "zstandard"))
        patch_dir, build_dir, out_dir, whl = _fixture(tmp_path)
        r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
        viol = check_codecs(store)
        assert any("==" in v for v in viol), viol
    finally:
        req.write_text(orig)


def test_mutation_package_dropped_from_file(tmp_path):
    """Mutation: drop lz4 from the requirements file -> reported."""
    req = ROOT / "requirements-image-codecs.txt"
    orig = req.read_text()
    try:
        req.write_text("\n".join(ln for ln in orig.splitlines()
                                if not ln.startswith("lz4==")))
        patch_dir, build_dir, out_dir, whl = _fixture(tmp_path)
        r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
        viol = check_codecs(store)
        assert any("lz4" in v for v in viol), viol
    finally:
        req.write_text(orig)


def test_mutation_copy_dropped(tmp_path):
    """Mutation: drop the COPY of the requirements file -> reported (the
    install would fail at docker build on a missing file)."""
    def mutate(text: str) -> str:
        return "\n".join(ln for ln in text.splitlines()
                         if "requirements-image-codecs.txt" not in ln
                         or not ln.startswith("COPY "))
    store, _ = _mutant_store(tmp_path, mutate)
    viol = check_codecs(store)
    assert any("no COPY" in v for v in viol), viol


def test_mutation_markers_cut_from_verify_output(tmp_path):
    """Mutation: cut the codec markers from the verify output -> the strict
    gate in build-image.sh must REFUSE to publish the image (behavioral: the
    build is run with a verify stub whose output lacks the markers)."""
    patch_dir, build_dir, out_dir, whl = _fixture(tmp_path)
    r, store = _run_image(
        tmp_path, patch_dir, build_dir, out_dir,
        verify_extra="KVP_ZSTD_VERSION=0.25.0\nKVP_LZ4_VERSION=4.4.5\n")
    assert r.returncode != 0, (
        "the build must refuse when the verify output lacks the strict "
        f"codec markers:\n{r.stdout[-800:]}")
    out = r.stdout + r.stderr
    assert "KVP_ZSTD_IMPORT=OK" in out and "missing" in out, (
        f"the refusal must name the missing marker:\n{out[-800:]}")


def test_smoke_runs_for_both_codecs_and_markers_gated():
    """(K6.3) build-image.sh must run the server smoke for zstd AND none with
    the SHARED command source, and gate on the strict smoke markers."""
    bi = SCRIPT.read_text()
    assert "_smoke_cache_server" in bi, "build-image.sh must run the smoke script"
    assert "_kvp_server_argv" in (ROOT / "scripts" / "env.sh").read_text(), \
        "the server argv must live in env.sh (one source)"
    assert "_kvp_server_argv" in (ROOT / "scripts" / "start-cache.sh").read_text(), \
        "start-cache.sh must use the shared server argv"
    assert "_kvp_server_argv" in (ROOT / "scripts" / "_smoke_cache_server.sh").read_text(), \
        "the smoke must use the shared server argv"
    for codec in ("zstd", "none"):
        assert f"KVP_SMOKE_{codec.upper()}_READY=OK" in bi or \
            'KVP_SMOKE_${_uc}_READY=OK' in bi, (
            f"the gate must require the READY marker for {codec}")
    assert "KVP_SMOKE_${_uc}_READY=OK" in bi and "KVP_SMOKE_${_uc}_EXIT=0" in bi, (
        "build-image.sh must gate on the strict smoke markers")


def test_mutation_smoke_markers_cut_is_red(tmp_path):
    """Mutation: the smoke prints neither READY nor EXIT -> the build gate
    must REFUSE (behavioral, same as the verify-marker mutation)."""
    patch_dir, build_dir, out_dir, whl = _fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir,
                         smoke_extra="")
    assert r.returncode != 0, (
        "the build must refuse when the smoke output lacks the markers:\n"
        f"{r.stdout[-800:]}")
    out = r.stdout + r.stderr
    assert "KVP_SMOKE" in out and "missing" in out, (
        f"the refusal must name the missing smoke marker:\n{out[-800:]}")


# ---------------------------------------------------------------------------
# start-cache refusal text (K6.4)
# ---------------------------------------------------------------------------
def test_start_cache_codec_refusal_names_the_rebuild(tmp_path):
    """r21 L2: the codec-refusal test must reach the CODEC PROBE, not die
    earlier on the custom-image check (r20's stub answered `image inspect`
    with {} -- the refusal then fired for the WRONG reason and the test was
    green by accident). The stub now answers labels + identity of a REAL
    build (the deep check passes) and only the codec probe fails with
    ModuleNotFoundError. Assert: rc != 0; `lossless codec 'zstd' is not
    available` present; `build-image.sh` present; NO `MISMATCH` / `NOT the
    custom` (the refusal is about the codec, not the image); the server was
    NEVER started."""
    import hashlib
    import json as _json
    real_patch_dir = ROOT / "vendor" / "patches" / "lmcache"

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    labels = _json.dumps({
        "org.kvp.lmc-base": ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d"
                             "96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134"
                             "e04b883623f8adcbd8e3a0ec3140a193abcf"),
        "org.kvp.lmc-patch-4136": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
    })
    wv = "0.5.8rc1.post48+kvp.60dce759ea93"
    ident = _json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel": f"lmcache-{wv}-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": wv,
    })
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text('''#!/usr/bin/env bash
echo "$*" >> "DOCKERLOG"
cmd="${1:-}"; shift || true
_all="$*"
case "$cmd" in
  image)
    sub="${1:-}"; shift || true
    case "$sub" in
      inspect) echo 'LABELSJSON' ;;
    esac ;;
  run)
    case "$_all" in
      *check_codec_available*)
        echo "ModuleNotFoundError: No module named 'zstandard'" >&2
        exit 1 ;;
      *importlib.metadata*) echo "WHEELVER"; exit 0 ;;
      *find_spec*) exit 0 ;;
    esac
    case "$_all" in
      *--entrypoint*cat*) cat <<'IDENTEOF'
IDENTJSON
IDENTEOF
        exit 0 ;;
    esac
    exit 0 ;;
  exec) cat <<'IDENTEOF'
IDENTJSON
IDENTEOF
    ;;
  inspect) echo "sha256:AAAA" ;;
  ps) exit 0 ;;
  *) exit 0 ;;
esac
'''.replace("DOCKERLOG", str(docker_log))
        .replace("LABELSJSON", labels)
        .replace("IDENTJSON", ident)
        .replace("WHEELVER", wv)
        .replace("AAAA", "a" * 64))
    (bin_dir / "docker").chmod(0o755)
    live = tmp_path / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (tmp_path / "cache").mkdir()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
               KVP_LIVE_DIR=str(live),
               KVP_CACHE_ROOT=str(tmp_path / "cache"),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LAUNCH_MODE="docker",
               LMCACHE_L2_SERDE="zstd",
               KVP_SKIP_IPC_TEST="1")
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD", "KVP_SKIP_LMC_IDENTITY",
                "KVP_SKIP_IMAGE_CHECK", "KVP_MODEL_CONFIG"):
        env.pop(var, None)
    r = subprocess.run(["bash", str(START_CACHE)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"a missing codec in the image must refuse the cache start:\n{out[-1200:]}")
    assert "lossless codec 'zstd' is not available" in out, (
        f"the refusal must come from the CODEC probe:\n{out[-1200:]}")
    assert "build-image.sh" in out, (
        f"the refusal must name the image rebuild:\n{out[-1200:]}")
    assert "MISMATCH" not in out and "NOT the custom" not in out, (
        f"the custom-image check must PASS (the refusal is about the codec, "
        f"not the image):\n{out[-1200:]}")
    assert "install the codec into the image" not in out, (
        f"the impossible 'install the codec into the image' hint must be GONE:\n{out[-1200:]}")
    # the server must NEVER be started (the --help probe is allowed)
    dlog = docker_log.read_text() if docker_log.exists() else ""
    for line in dlog.splitlines():
        if "kvpersist.launch" in line and "server" in line and "--help" not in line:
            raise AssertionError(f"the cache server must not start on a codec "
                                 f"refusal:\n{dlog[-800:]}")


# ---------------------------------------------------------------------------
# r21 L2 mutation
# ---------------------------------------------------------------------------
def test_mutation_codec_probe_neutralized_is_red(tmp_path):
    """r21 L2 mutation: neutralize the codec probe in a MUTANT start-cache.sh
    (replace the docker probe call with `true`) -> the codec refusal
    DISAPPEARS on the mutant (the mutant starts the server instead of
    refusing). Proves the honest K6.4 test's refusal really comes from the
    codec probe, not from an earlier check."""
    # an honest mutant: the docker-mode codec probe is neutralized
    src = START_CACHE.read_text()
    marker = 'CODEC_PROBE="$(docker run'
    assert marker in src, "codec probe marker not found in start-cache.sh"
    mutant = src.replace(marker, 'CODEC_PROBE="$(true && cat <<__NEUTRALIZED__\n'
                         '# __NEUTRALIZED__')
    # close the heredoc before the original command's closing quote
    mutant = mutant.replace('__NEUTRALIZED__\n"', '__NEUTRALIZED__\n)"', 1)
    assert mutant != src, "mutation produced no change"
    mutant_sh = tmp_path / "start-cache-mutant.sh"
    mutant_sh.write_text(mutant)
    mutant_sh.chmod(0o755)
    # run the mutant with the same honest stub as the real test
    import hashlib
    import json as _json
    real_patch_dir = ROOT / "vendor" / "patches" / "lmcache"

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    labels = _json.dumps({
        "org.kvp.lmc-base": ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d"
                             "96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134"
                             "e04b883623f8adcbd8e3a0ec3140a193abcf"),
        "org.kvp.lmc-patch-4136": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
    })
    wv = "0.5.8rc1.post48+kvp.60dce759ea93"
    ident = _json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel": f"lmcache-{wv}-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": wv,
    })
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text('''#!/usr/bin/env bash
cmd="${1:-}"; shift || true
_all="$*"
case "$cmd" in
  image)
    sub="${1:-}"; shift || true
    case "$sub" in
      inspect) echo 'LABELSJSON' ;;
    esac ;;
  run)
    case "$_all" in
      *check_codec_available*)
        echo "ModuleNotFoundError: No module named 'zstandard'" >&2
        exit 1 ;;
      *importlib.metadata*) echo "WHEELVER"; exit 0 ;;
      *find_spec*) exit 0 ;;
    esac
    case "$_all" in
      *--entrypoint*cat*) cat <<'IDENTEOF'
IDENTJSON
IDENTEOF
        exit 0 ;;
    esac
    exit 0 ;;
  exec) cat <<'IDENTEOF'
IDENTJSON
IDENTEOF
    ;;
  inspect) echo "sha256:AAAA" ;;
  ps) exit 0 ;;
  *) exit 0 ;;
esac
'''.replace("LABELSJSON", labels)
        .replace("IDENTJSON", ident)
        .replace("WHEELVER", wv)
        .replace("AAAA", "a" * 64))
    (bin_dir / "docker").chmod(0o755)
    live = tmp_path / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (tmp_path / "cache").mkdir()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
               KVP_LIVE_DIR=str(live),
               KVP_CACHE_ROOT=str(tmp_path / "cache"),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LAUNCH_MODE="docker",
               LMCACHE_L2_SERDE="zstd",
               KVP_SKIP_IPC_TEST="1")
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD", "KVP_SKIP_LMC_IDENTITY",
                "KVP_SKIP_IMAGE_CHECK", "KVP_MODEL_CONFIG"):
        env.pop(var, None)
    r = subprocess.run(["bash", str(mutant_sh)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    # the mutant must NOT refuse on the codec (the probe is neutralized) --
    # either it passes the codec check (and fails later on the server start)
    # or it succeeds; either way the codec refusal is GONE
    assert "lossless codec 'zstd' is not available" not in out, (
        f"the neutralized probe must NOT produce the codec refusal:\n{out[-1200:]}")
