# SPDX-License-Identifier: Apache-2.0
"""r18 G1: a FAILED build must not leave a stale provenance.json next to the
new untested wheel.

The wheel lands in $OUT_DIR/wheels at C5 (rm -f *.whl + pip wheel), LONG
before the gates C8-C14 run. provenance.json is written at the very END of a
successful build. So after any build that fails at a gate, $OUT_DIR holds a
NEW untested wheel next to the OLD provenance.json -- and build-image.sh
(required: only the two files exist) would bake an image from that pair.

Fix: build-lmcache.sh removes provenance.json at the START of the build
(next to rerun-prod-hash.txt); provenance exists only for a build that
passed every gate.

Tests drive the REAL build-lmcache.sh with a docker stub (the stub drops the
wheel into the MOUNTED /kvp-wheels dir and exits with a chosen rc) against a
LOCAL mini git repo, then check $OUT_DIR and the REAL build-image.sh:
  (a) pre-placed provenance.json + old wheel, docker rc=1 (failed build)
      -> provenance.json is GONE and build-image.sh refuses
      'provenance.json not found';
  (b) positive control: docker rc=0 -> a NEW provenance.json is written.
Mutation: dropping the `rm -f ... provenance.json` line -> (a) RED.
"""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
BUILD_IMAGE = ROOT / "scripts" / "build-image.sh"
PIN_FILE = ROOT / "vendor" / "pinned-base-image.env"

GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=os.devnull)


def _git(args, cwd) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       env=GIT_ENV, cwd=str(cwd))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _git_raw(args, cwd) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       env=GIT_ENV, cwd=str(cwd))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


def _mini_repo(tmp_path: pathlib.Path):
    """A local mini LMCache repo (the "pin") + real patches made by git diff."""
    repo = tmp_path / "mini-repo"
    (repo / "lmcache").mkdir(parents=True)
    (repo / "lmcache" / "prod.py").write_text("# production code\nVALUE = 1\n")
    for rel in ("tests/v1/test_lmcache_mp_connector.py",
                "tests/v1/test_vllm_mp_adapter.py",
                "tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py",
                "tests/v1/multiprocess/test_native_state_lookup.py",
                "tests/v1/multiprocess/test_l1_owner_completion.py"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def test_ok():\n    assert True\n")
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "pin"], repo)
    sha = _git(["rev-parse", "HEAD"], repo)

    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    (repo / "lmcache" / "prod.py").write_text("# production code\nVALUE = 2\n")
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text(
        _git_raw(["diff", "HEAD", "--", "lmcache/prod.py"], repo))
    _git(["checkout", "-q", "--", "lmcache/prod.py"], repo)
    tfile = "tests/v1/multiprocess/test_native_state_lookup.py"
    (repo / tfile).write_text(
        "def test_ok():\n    assert True\n\ndef test_extra():\n    assert 1 == 1\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text(
        _git_raw(["diff", "HEAD", "--", tfile], repo))
    _git(["checkout", "-q", "--", tfile], repo)
    return repo, sha, patch_dir


def _make_bin(tmp_path: pathlib.Path, docker_rc: int) -> pathlib.Path:
    """Tool dir: real tools + docker stub. The stub drops the wheel into the
    MOUNTED /kvp-wheels dir and exits with docker_rc (rc=1 simulates a build
    that fails at a gate AFTER the wheel was built)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "sort", "sha256sum", "id", "df", "seq"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    (bin_dir / "docker").write_text("".join([
        "#!/usr/bin/env bash\n",
        'cmd="$1"; shift || true\n',
        'case "$cmd" in\n',
        "  run)\n",
        "    _wheels=\"\"\n",
        '    while [ "$#" -gt 0 ]; do case "$1" in -v) case "$2" in */kvp-wheels:*) _wheels="${2%%:*}" ;; esac; shift 2 ;; *) shift ;; esac; done\n',
        '    /usr/bin/python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], \'w\'); z.writestr(\'lmcache/__init__.py\', b\'# stub\'); z.writestr(\'lmcache/cuda_ops_stub.so\', b\'\\x00\'); z.writestr(\'lmcache/lmcache_native_stub.so\', b\'\\x00\'); z.close()" "$_wheels/lmcache-0.5.8rc1.post48+kvp.abc.whl"\n',
        f"    exit {docker_rc} ;;\n",
        '  inspect) echo "sha256:$(printf \'a%.0s\' $(seq 64))"; exit 0 ;;\n',
        "  info) exit 0 ;;\n",
        "  *) exit 0 ;;\n",
        "esac\n",
    ]))
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "python3").write_text("".join([
        "#!/usr/bin/env bash\n",
        'if [ "${1:-}" = "-P" ]; then shift; fi\n',
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then exit 0; fi\n',
        'exec /usr/bin/python3 "$@"\n',
    ]))
    (bin_dir / "python3").chmod(0o755)
    return bin_dir


def _env(bin_dir, repo, sha, patch_dir, out_dir, build_dir) -> dict:
    env = dict(os.environ,
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
               LMCACHE_REPO_URL=str(repo),
               LMCACHE_SOURCE_COMMIT=sha,
               PR6551_MERGE=sha,
               PR6608_MERGE=sha,
               KVP_PATCH_DIR=str(patch_dir),
               KVP_OUT_DIR=str(out_dir),
               KVP_BUILD_DIR=str(build_dir),
               KVP_PIN_FILE=str(PIN_FILE))
    for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
        env.pop(var, None)
    return env


def _run_build(env, tmp_path):
    return subprocess.run(["bash", str(BUILD_LMC)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(tmp_path))


def _run_build_image(env, tmp_path, out_dir=None):
    """The REAL build-image.sh. In production BOTH scripts work with the SAME
    dir (live/lmcache-build) -- r19 H4.1: pass the build's OUT_DIR as
    KVP_BUILD_DIR (for build-lmcache.sh KVP_BUILD_DIR means the WORK dir and
    must NOT be reused -- a second run would hit 'patch ALREADY applied')."""
    env = dict(env)
    if out_dir is not None:
        env["KVP_BUILD_DIR"] = str(out_dir)
    return subprocess.run(["bash", str(BUILD_IMAGE)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(tmp_path))


def _setup(tmp_path: pathlib.Path, docker_rc: int):
    repo, sha, patch_dir = _mini_repo(tmp_path)
    out_dir = tmp_path / "out"
    build_dir = tmp_path / "build"
    bin_dir = _make_bin(tmp_path, docker_rc)
    env = _env(bin_dir, repo, sha, patch_dir, out_dir, build_dir)
    return repo, sha, patch_dir, out_dir, build_dir, env


def _preplace_old_artifacts(out_dir: pathlib.Path, with_old_wheel: bool = True):
    """A previous SUCCESSFUL build left provenance.json (+ an old wheel)."""
    wheels = out_dir / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    if with_old_wheel:
        with zipfile.ZipFile(wheels / "lmcache-OLD.whl", "w") as z:
            z.writestr("lmcache/__init__.py", b"# old\n")
    (out_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "wheel": "lmcache-OLD.whl",
        "wheel_sha256": "0" * 64,
    }))


def test_failed_build_leaves_no_stale_provenance(tmp_path):
    """(a) THE r18 G1 case: a build that fails at a gate (docker rc=1 AFTER
    dropping the new wheel) must leave NO provenance.json -- the real
    build-image.sh must then refuse 'provenance.json not found'."""
    _repo, _sha, _patches, out_dir, _build_dir, env = _setup(tmp_path, docker_rc=1)
    _preplace_old_artifacts(out_dir)
    rb = _run_build(env, tmp_path)
    assert rb.returncode != 0, (
        f"a failing container phase must abort the build:\n{rb.stdout[-800:]}")
    # the docker stub DID drop the new wheel (the r18 defect scenario)
    assert list((out_dir / "wheels").glob("*.whl")), "the stub must drop a wheel"
    assert not (out_dir / "provenance.json").exists(), (
        "stale provenance.json must be removed at build start (r18 G1):\n"
        f"{rb.stdout[-800:]}")
    # the REAL build-image.sh refuses the missing provenance -- run it on the
    # SAME dir the build writes to (production: live/lmcache-build)
    ri = _run_build_image(env, tmp_path, out_dir=out_dir)
    assert ri.returncode != 0, "build-image must refuse without provenance"
    assert "provenance.json not found" in (ri.stdout + ri.stderr), (
        f"the refusal must name provenance.json:\n{ri.stdout}\n{ri.stderr}")


def test_end_to_end_build_then_image_and_failed_rebuild(tmp_path):
    """r19 H4.1: the full production chain on ONE dir (like
    live/lmcache-build): real build (docker rc=0) -> its OWN provenance.json
    -> real build-image.sh reaches docker build and writes identity; then a
    FAILING rebuild -> build-image.sh refuses 'provenance.json not found'.
    (Mutation: drop the rm -f provenance.json -> both controls RED.)"""
    repo, sha, patch_dir, out_dir, build_dir, env = _setup(tmp_path, docker_rc=0)
    rb = _run_build(env, tmp_path)
    assert rb.returncode == 0, (
        f"the successful build must pass:\n{rb.stdout[-1200:]}")
    assert (out_dir / "provenance.json").is_file(), (
        "the build must write its OWN provenance.json")
    # before build-image: a verify-capable docker stub (build-image runs
    # `docker run` for the in-image check and needs the success markers)
    bin_dir = pathlib.Path(env["PATH"].split(os.pathsep)[0])
    (bin_dir / "docker").write_text("".join([
        "#!/usr/bin/env bash\n",
        'cmd="$1"; shift || true\n',
        'case "$cmd" in\n',
        "  run)\n",
        '    echo "KVP_PY_IMPORTS=OK"\n',
        '    echo "KVP_CUDA_OPS_IMPORT=OK"\n',
        '    echo "KVP_NATIVE_IMPORT=OK"\n',
        '    echo "KVP_ZSTD_IMPORT=OK"\n',
        '    echo "KVP_LZ4_IMPORT=OK"\n',
        '    echo "KVP_IDENTITY_FILE=OK"\n',
        '    echo "KVP_GPU_COUNT=4"\n',
        '    echo "KVP_GPU_SMOKE_OK_COUNT=4"\n',
        "    exit 0 ;;\n",
        '  inspect) echo "sha256:$(printf \'a%.0s\' $(seq 64))"; exit 0 ;;\n',
        "  build) exit 0 ;;\n",
        "  info) exit 0 ;;\n",
        "  *) exit 0 ;;\n",
        "esac\n",
    ]))
    (bin_dir / "docker").chmod(0o755)
    # the REAL build-image.sh on the SAME out dir must reach docker build
    ri = _run_build_image(env, tmp_path, out_dir=out_dir)
    assert ri.returncode == 0, (
        f"build-image on the consistent pair must reach docker build:\n"
        f"stdout:\n{ri.stdout[-1200:]}\nstderr:\n{ri.stderr[-1200:]}")
    assert "BUILD REFUSED" not in (ri.stdout + ri.stderr), (
        f"no refusal on the consistent pair:\n{ri.stdout}\n{ri.stderr}")
    # now a FAILING rebuild (docker rc=1) on the same dir
    bin_dir = pathlib.Path(env["PATH"].split(os.pathsep)[0])
    (bin_dir / "docker").write_text("".join([
        "#!/usr/bin/env bash\n",
        'cmd="$1"; shift || true\n',
        'case "$cmd" in\n',
        "  run)\n",
        "    _wheels=\"\"\n",
        '    while [ "$#" -gt 0 ]; do case "$1" in -v) case "$2" in */kvp-wheels:*) _wheels="${2%%:*}" ;; esac; shift 2 ;; *) shift ;; esac; done\n',
        '    /usr/bin/python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], \'w\'); z.writestr(\'lmcache/__init__.py\', b\'# stub\'); z.close()" "$_wheels/lmcache-NEW.whl"\n',
        "    exit 1 ;;\n",
        '  inspect) echo "sha256:$(printf \'a%.0s\' $(seq 64))"; exit 0 ;;\n',
        "  info) exit 0 ;;\n",
        "  *) exit 0 ;;\n",
        "esac\n",
    ]))
    (bin_dir / "docker").chmod(0o755)
    rb2 = _run_build(env, tmp_path)
    assert rb2.returncode != 0, "the failing rebuild must abort"
    assert not (out_dir / "provenance.json").exists(), (
        "the failing rebuild must remove the stale provenance")
    ri2 = _run_build_image(env, tmp_path, out_dir=out_dir)
    assert ri2.returncode != 0, (
        "build-image after the failed rebuild must refuse")
    assert "provenance.json not found" in (ri2.stdout + ri2.stderr), (
        f"the refusal must name provenance.json:\n{ri2.stdout}\n{ri2.stderr}")


def test_successful_build_writes_new_provenance(tmp_path):
    """(b) positive control: a fully successful build writes a NEW
    provenance.json (and the stale one is replaced, not kept)."""
    _repo, _sha, _patches, out_dir, _build_dir, env = _setup(tmp_path, docker_rc=0)
    # only the STALE provenance is pre-placed: the build replaces it with one
    # describing the NEW wheel the docker stub drops (exactly one wheel)
    _preplace_old_artifacts(out_dir, with_old_wheel=False)
    rb = _run_build(env, tmp_path)
    assert rb.returncode == 0, (
        f"the build must pass:\nstdout:\n{rb.stdout[-1200:]}\nstderr:\n{rb.stderr[-1200:]}")
    prov = out_dir / "provenance.json"
    assert prov.is_file(), "a successful build must write provenance.json"
    data = json.loads(prov.read_text())
    assert data.get("wheel") != "lmcache-OLD.whl", (
        "the provenance must describe the NEW wheel, not the stale one")
    assert data.get("module") == "custom-lmcache-build", data.get("module")