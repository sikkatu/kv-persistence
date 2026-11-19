# SPDX-License-Identifier: Apache-2.0
"""r17 B3: the fast rerun must find the wheel WHERE THE REAL BUILD PUTS IT
and must be tied to the build by a recorded production hash.

Hardware-blocking defect (r17 review):
  1. `build-lmcache.sh` writes the wheel to WHEEL_OUT="$OUT_DIR/wheels"
     (live/lmcache-build/wheels, mounted at /kvp-wheels), while
     rerun-lmcache-gate.sh searched $WORK_DIR/wheels and auto-detected
     /tmp/kvp-lmc-build.* -- after a REAL build there is never a wheel
     there ("no previous work dir with a wheel").
  2. Nobody recorded `rerun-prod-hash.txt` at build time -- the first rerun
     recorded the CURRENT hash and passed, so a production edit after the
     build slipped through the gate (condition (a) only worked from the
     second run).

This test drives the REAL build-lmcache.sh with a docker stub (the stub
drops the wheel into the MOUNTED /kvp-wheels dir -- it never pre-creates a
work dir) against a LOCAL mini git repo (LMCACHE_REPO_URL honoured, real
patches made with git diff), then runs the REAL rerun-lmcache-gate.sh with
NO arguments (OUT_DIR via KVP_OUT_DIR, the same variable the build uses):
  (a) end to end: build -> rerun (no args) finds the wheel and the hash
      and reaches the NOT-production banner (the work dir is NOT created
      by the test by hand);
  (b) editing the PRODUCTION hunk of a patch after the build -> refusal on
      the FIRST rerun run;
  (c) editing only the TEST hunk -> passes;
  (d) no hash file -> refusal "Need a full build";
  (e) --trust-wheel -> records the hash and prints the loud ASSUMING
      warning;
  (f) two wheels -> refusal.
Mutations: searching $WORK_DIR/wheels again -> (a) RED; the old
"record on first run" behaviour -> (d) RED.
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
RERUN = ROOT / "scripts" / "rerun-lmcache-gate.sh"
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
    """A local mini LMCache repo (the "pin") + a real patch dir made with
    git diff. Patch 4136 = PRODUCTION change; patch 5972 = TEST change."""
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


def _make_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tool dir: real tools + a docker stub that drops the wheel into the
    MOUNTED /kvp-wheels dir (like the real in-container phase does)."""
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
        # the in-container phase writes the wheel to the MOUNTED dir
        "  run)\n",
        "    _wheels=\"\"\n",
        '    while [ "$#" -gt 0 ]; do case "$1" in -v) case "$2" in */kvp-wheels:*) _wheels=\"${2%%:*}\" ;; esac; shift 2 ;; *) shift ;; esac; done\n',
        '    /usr/bin/python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], \'w\'); z.writestr(\'lmcache/__init__.py\', b\'# stub\'); z.writestr(\'lmcache/cuda_ops_stub.so\', b\'\\x00\'); z.writestr(\'lmcache/lmcache_native_stub.so\', b\'\\x00\'); z.close()" "$_wheels/lmcache-0.5.8rc1.post48+kvp.abc.whl"\n',
        "    exit 0 ;;\n",
        '  inspect) echo "sha256:$(printf \'a%.0s\' $(seq 64))"; exit 0 ;;\n',
        "  info) exit 0 ;;\n",
        "  *) exit 0 ;;\n",
        "esac\n",
    ]))
    (bin_dir / "docker").chmod(0o755)
    # NOTE: git stays REAL -- the build/rerun clone a mini repo, apply real
    # patches and compute the production hash with real git (only `docker`
    # is stubbed).
    (bin_dir / "python3").write_text("".join([
        "#!/usr/bin/env bash\n",
        'if [ "${1:-}" = "-P" ]; then shift; fi\n',
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then exit 0; fi\n',
        'exec /usr/bin/python3 "$@"\n',
    ]))
    (bin_dir / "python3").chmod(0o755)
    return bin_dir


def _env(bin_dir: pathlib.Path, repo: pathlib.Path, sha: str,
         patch_dir: pathlib.Path, out_dir: pathlib.Path,
         build_dir: pathlib.Path) -> dict:
    env = dict(os.environ,
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
               LMCACHE_REPO_URL=str(repo),
               LMCACHE_SOURCE_COMMIT=sha,
               # the mini repo carries no real PR merges -- point the
               # ancestor gates at its own pin commit (trivially an
               # ancestor; the gate logic itself is covered by
               # test_r16_shell_gates.py)
               PR6551_MERGE=sha,
               PR6608_MERGE=sha,
               KVP_PATCH_DIR=str(patch_dir),
               KVP_OUT_DIR=str(out_dir),
               KVP_BUILD_DIR=str(build_dir),
               KVP_PIN_FILE=str(PIN_FILE))
    for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
        env.pop(var, None)
    return env


def _run_build(env: dict, tmp_path: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BUILD_LMC)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(tmp_path))


def _run_rerun(env: dict, tmp_path: pathlib.Path,
               *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(RERUN), *args], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(tmp_path))


def _setup(tmp_path: pathlib.Path):
    repo, sha, patch_dir = _mini_repo(tmp_path)
    out_dir = tmp_path / "out"   # KVP_OUT_DIR -- what the build uses
    build_dir = tmp_path / "build"
    bin_dir = _make_bin(tmp_path)
    env = _env(bin_dir, repo, sha, patch_dir, out_dir, build_dir)
    return repo, sha, patch_dir, out_dir, build_dir, env


def test_build_then_rerun_without_arguments_reaches_banner(tmp_path):
    """(a) THE hardware case: real build (wheel lands in $OUT_DIR/wheels by
    the docker stub) -> real rerun WITHOUT arguments finds that wheel and
    the recorded hash and reaches the banner. No work dir / wheel dir is
    created by the test by hand."""
    _repo, _sha, _patches, out_dir, _build_dir, env = _setup(tmp_path)
    rb = _run_build(env, tmp_path)
    assert rb.returncode == 0, (
        f"the build must pass:\nstdout:\n{rb.stdout[-1500:]}\nstderr:\n{rb.stderr[-1500:]}")
    # the build really wrote the hash file and the wheel dir
    assert (out_dir / "rerun-prod-hash.txt").is_file(), (
        f"build-lmcache.sh must record rerun-prod-hash.txt in $OUT_DIR:\n{rb.stdout}")
    assert (out_dir / "wheels").is_dir() and list((out_dir / "wheels").glob("*.whl")), (
        "the docker stub must have dropped the wheel into the mounted dir")
    rr = _run_rerun(env, tmp_path)  # NO arguments
    assert rr.returncode == 0, (
        f"rerun without arguments must find the wheel and pass:\n"
        f"stdout:\n{rr.stdout[-1500:]}\nstderr:\n{rr.stderr[-1500:]}")
    out = rr.stdout + rr.stderr
    assert "NOT production-acceptable" in out, out[-800:]
    assert "no provenance/identity written" in out, out[-800:]


def test_production_edit_after_build_refused_on_first_run(tmp_path):
    """(b) editing the PRODUCTION hunk of a patch AFTER the build must be
    refused by the FIRST rerun run (the old code recorded the current hash
    on the first run and let it pass)."""
    repo, _sha, patch_dir, out_dir, _build_dir, env = _setup(tmp_path)
    rb = _run_build(env, tmp_path)
    assert rb.returncode == 0, rb.stdout[-800:] + rb.stderr[-800:]
    # now edit the PRODUCTION part of the patch (lmcache/prod.py hunk)
    prod = repo / "lmcache" / "prod.py"
    prod.write_text("# production code\nVALUE = 3\n")
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text(
        _git_raw(["diff", "HEAD", "--", "lmcache/prod.py"], repo))
    _git(["checkout", "-q", "--", "lmcache/prod.py"], repo)
    rr = _run_rerun(env, tmp_path)
    assert rr.returncode != 0, (
        f"a production edit must be refused on the FIRST run:\n{rr.stdout}")
    out = rr.stdout + rr.stderr
    assert "Need a full build" in out, out[-800:]
    assert "PRODUCTION part" in out, out[-800:]


def test_test_only_edit_after_build_passes(tmp_path):
    """(c) editing ONLY the test hunk after the build -> the rerun passes
    (tests come from the source tree, the wheel is untouched)."""
    repo, _sha, patch_dir, _out_dir, _build_dir, env = _setup(tmp_path)
    rb = _run_build(env, tmp_path)
    assert rb.returncode == 0, rb.stdout[-800:] + rb.stderr[-800:]
    tfile = "tests/v1/multiprocess/test_native_state_lookup.py"
    (repo / tfile).write_text(
        "def test_ok():\n    assert True\n\ndef test_extra():\n    assert 2 == 2\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text(
        _git_raw(["diff", "HEAD", "--", tfile], repo))
    _git(["checkout", "-q", "--", tfile], repo)
    rr = _run_rerun(env, tmp_path)
    assert rr.returncode == 0, (
        f"a test-only edit must pass:\nstdout:\n{rr.stdout[-1000:]}\n"
        f"stderr:\n{rr.stderr[-1000:]}")


def test_no_hash_file_refused_without_trust(tmp_path):
    """(d) no rerun-prod-hash.txt -> refusal with "Need a full build" (the
    old 'record on the first run' mode must be gone; mutation: restoring it
    -> this test RED)."""
    repo, sha, patch_dir, out_dir, build_dir, env = _setup(tmp_path)
    # a wheel from "a previous build" exists, but no hash file
    wheels = out_dir / "wheels"
    wheels.mkdir(parents=True)
    with zipfile.ZipFile(wheels / "lmcache-0.5.8rc1.post48+kvp.abc.whl", "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
    rr = _run_rerun(env, tmp_path)
    assert rr.returncode != 0, (
        f"no hash file must be refused:\n{rr.stdout}")
    out = rr.stdout + rr.stderr
    assert "Need a full build" in out, out[-800:]
    assert not (out_dir / "rerun-prod-hash.txt").exists(), (
        "without --trust-wheel nothing may be recorded")


def test_trust_wheel_records_hash_with_warning(tmp_path):
    """(e) --trust-wheel records the CURRENT hash and prints the loud
    ASSUMING warning."""
    repo, sha, patch_dir, out_dir, build_dir, env = _setup(tmp_path)
    wheels = out_dir / "wheels"
    wheels.mkdir(parents=True)
    with zipfile.ZipFile(wheels / "lmcache-0.5.8rc1.post48+kvp.abc.whl", "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
    rr = _run_rerun(env, tmp_path, "--trust-wheel")
    assert rr.returncode == 0, (
        f"--trust-wheel must pass:\nstdout:\n{rr.stdout[-1000:]}\n"
        f"stderr:\n{rr.stderr[-1000:]}")
    out = rr.stdout + rr.stderr
    assert "ASSUMING" in out and "this production tree" in out, out[-800:]
    assert (out_dir / "rerun-prod-hash.txt").is_file(), (
        "--trust-wheel must record the current hash")
    assert (out_dir / "rerun-prod-hash.txt").read_text().strip(), (
        "the recorded hash must not be empty")
    # a second run WITHOUT the flag now passes the hash check
    rr2 = _run_rerun(env, tmp_path)
    assert rr2.returncode == 0, rr2.stdout[-800:] + rr2.stderr[-800:]


def test_two_wheels_refused(tmp_path):
    """(f) more than one *.whl in the wheel dir -> refusal (no arbitrary
    pick)."""
    repo, sha, patch_dir, out_dir, build_dir, env = _setup(tmp_path)
    wheels = out_dir / "wheels"
    wheels.mkdir(parents=True)
    for name in ("lmcache-0.5.8rc1.post48+kvp.aaa.whl",
                 "lmcache-0.5.8rc1.post48+kvp.bbb.whl"):
        with zipfile.ZipFile(wheels / name, "w") as z:
            z.writestr("lmcache/__init__.py", b"# stub\n")
    rr = _run_rerun(env, tmp_path, "--trust-wheel")
    assert rr.returncode != 0, (
        f"two wheels must be refused:\n{rr.stdout}")
    out = rr.stdout + rr.stderr
    assert "exactly one wheel" in out, out[-800:]


def test_hash_recorded_before_docker_run(tmp_path):
    """The build records rerun-prod-hash.txt BEFORE the docker run: even a
    build that dies in the container phase leaves the hash (C5 keeps the
    wheel of the last build -- the hash must describe that tree)."""
    repo, sha, patch_dir, out_dir, build_dir, env = _setup(tmp_path)
    # docker stub that FAILS the run (build aborts in the container phase)
    bin_dir = pathlib.Path(env["PATH"].split(os.pathsep)[0])
    (bin_dir / "docker").write_text(
        '#!/usr/bin/env bash\ncase "$1" in run) exit 1 ;; '
        'inspect) echo "sha256:' + "a" * 64 + '"; exit 0 ;; '
        'info) exit 0 ;; *) exit 0 ;; esac\n')
    (bin_dir / "docker").chmod(0o755)
    rb = _run_build(env, tmp_path)
    assert rb.returncode != 0, "a failing container phase must abort the build"
    assert (out_dir / "rerun-prod-hash.txt").is_file(), (
        f"the hash must be recorded BEFORE the docker run:\n{rb.stdout[-800:]}")
    assert (out_dir / "rerun-prod-hash.txt").read_text().strip(), (
        "the recorded hash must not be empty")