# SPDX-License-Identifier: Apache-2.0
"""r16 P3: fast CPU-gate rerun without rebuilding the wheel.

`scripts/rerun-lmcache-gate.sh [WORK_DIR]` re-applies the patches to a fresh
pinned checkout, runs the r15 P1 import gate, takes the ALREADY BUILT wheel
from a previous work dir, and runs C8-C12 in the pinned image. Conditions:
  (a) the PRODUCTION part of the patched tree (everything except tests/ and
      docs/) must match the tree the wheel was built from (hash recorded in
      the work dir) -- mismatch -> "Need a full build";
  (b) the end banner `NOT production-acceptable: no provenance/identity
      written` -- nothing written to live/lmcache-build;
  (c) the same gate logic as _incontainer_build.sh (shared module
      kvpersist.gate_common) -- never a copy.

Tests run the REAL rerun-lmcache-gate.sh end to end against a LOCAL mini git
repo (the script's LMCACHE_REPO_URL is honoured) with REAL patches made by
git diff; only `docker run` is stubbed:
  (1) production-hash mismatch -> refusal with "Need a full build"
      (first run records the hash; changing the production patch -> RED);
  (2) no wheel in the work dir -> refusal;
  (3) success path prints the NOT-production banner;
  (4) nothing is written to live/lmcache-build;
  (5) both in-container scripts share kvpersist.gate_common (static).
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
RERUN = ROOT / "scripts" / "rerun-lmcache-gate.sh"
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
RERUN_INCONTAINER = ROOT / "scripts" / "_rerun_incontainer.sh"

GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _git(args, cwd) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       env=GIT_ENV, cwd=str(cwd))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _git_raw(args, cwd) -> str:
    """Raw stdout (no .strip() -- patch files need their trailing newline)."""
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       env=GIT_ENV, cwd=str(cwd))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


def _mini_repo(tmp_path: pathlib.Path):
    """A local mini LMCache repo (the "pin") + a real patch dir made with
    git diff. Returns (repo, pinned_sha, patch_dir)."""
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
    # real patch 1: a PRODUCTION change (lmcache/prod.py)
    (repo / "lmcache" / "prod.py").write_text("# production code\nVALUE = 2\n")
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text(
        _git_raw(["diff", "HEAD", "--", "lmcache/prod.py"], repo))
    _git(["checkout", "-q", "--", "lmcache/prod.py"], repo)
    # real patch 2: a TEST change only
    tfile = "tests/v1/multiprocess/test_native_state_lookup.py"
    (repo / tfile).write_text("def test_ok():\n    assert True\n\ndef test_extra():\n    assert 1 == 1\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text(
        _git_raw(["diff", "HEAD", "--", tfile], repo))
    _git(["checkout", "-q", "--", tfile], repo)
    return repo, sha, patch_dir


def _make_work_dir(tmp_path: pathlib.Path, prod_hash: str | None = None) -> pathlib.Path:
    wd = tmp_path / "work"
    (wd / "wheels").mkdir(parents=True)
    with zipfile.ZipFile(wd / "wheels" / "lmcache-0.5.8rc1.post48+kvp.abc.whl", "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
    if prod_hash is not None:
        (wd / "rerun-prod-hash.txt").write_text(prod_hash)
    return wd


def _stub_docker(tmp_path: pathlib.Path, rc: int = 0) -> pathlib.Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / "docker"
    p.write_text("#!/usr/bin/env bash\ncase \"$1\" in\n"
                 f"  run) echo 'docker-stub: run'; exit {rc} ;;\n"
                 "  inspect) echo \"sha256:$(printf 'a%.0s' $(seq 64))\"; exit 0 ;;\n"
                 "  info) exit 0 ;;\n"
                 "  *) exit 0 ;;\nesac\n")
    p.chmod(0o755)
    return bin_dir


def _run_rerun(tmp_path: pathlib.Path, work_dir: pathlib.Path,
               repo: pathlib.Path, sha: str, patch_dir: pathlib.Path,
               docker_rc: int = 0) -> subprocess.CompletedProcess:
    bin_dir = _stub_docker(tmp_path, rc=docker_rc)
    env = dict(os.environ,
               PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               LMCACHE_REPO_URL=str(repo),
               LMCACHE_SOURCE_COMMIT=sha,
               KVP_PATCH_DIR=str(patch_dir),
               KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"))
    return subprocess.run(
        ["bash", str(RERUN), str(work_dir)],
        capture_output=True, text=True, timeout=120,
        env=env, cwd=str(tmp_path))


def test_production_hash_mismatch_refused(tmp_path):
    """(1) first run records the production hash; after CHANGING the
    production patch the next run must refuse with "Need a full build"
    (mutation: a changed production tree -> this test RED)."""
    repo, sha, patch_dir = _mini_repo(tmp_path)
    wd = _make_work_dir(tmp_path)
    r1 = _run_rerun(tmp_path, wd, repo, sha, patch_dir)
    assert (wd / "rerun-prod-hash.txt").is_file(), (
        f"the first run must record the production hash:\n{r1.stdout}\n{r1.stderr}")
    assert r1.returncode == 0, (
        f"first run must succeed:\n{r1.stdout}\n{r1.stderr}")
    # change the PRODUCTION part of the patch -> hash mismatch
    prod = repo / "lmcache" / "prod.py"
    prod.write_text("# production code\nVALUE = 3\n")
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text(
        _git_raw(["diff", "HEAD", "--", "lmcache/prod.py"], repo))
    _git(["checkout", "-q", "--", "lmcache/prod.py"], repo)
    r2 = _run_rerun(tmp_path, wd, repo, sha, patch_dir)
    assert r2.returncode != 0, (
        f"a changed production part must be refused:\n{r2.stdout}\n{r2.stderr}")
    out = r2.stdout + r2.stderr
    assert "Need a full build" in out or "need a full build" in out, (
        f"the refusal must say a full build is needed:\n{out}")


def test_no_wheel_refused(tmp_path):
    """(2) a work dir without *.whl -> refusal (the rerun needs the wheel)."""
    repo, sha, patch_dir = _mini_repo(tmp_path)
    wd = tmp_path / "emptywork"
    (wd / "wheels").mkdir(parents=True)
    r = _run_rerun(tmp_path, wd, repo, sha, patch_dir)
    assert r.returncode != 0, "a missing wheel must be refused"
    out = r.stdout + r.stderr
    assert "no *.whl" in out or "no wheel" in out, (
        f"the refusal must name the wheel problem:\n{out}")


def test_success_path_prints_not_production_banner(tmp_path):
    """(3) the success path must end with the honest banner."""
    repo, sha, patch_dir = _mini_repo(tmp_path)
    wd = _make_work_dir(tmp_path)
    r = _run_rerun(tmp_path, wd, repo, sha, patch_dir)
    assert r.returncode == 0, (
        f"success path must pass:\n{r.stdout}\n{r.stderr}")
    out = r.stdout + r.stderr
    assert "NOT production-acceptable" in out, (
        f"the banner must be printed:\n{out}")
    assert "no provenance/identity written" in out, (
        f"the banner must say no provenance/identity written:\n{out}")


def test_nothing_written_to_live(tmp_path):
    """(4) the rerun must NOT write to live/lmcache-build (no provenance)."""
    repo, sha, patch_dir = _mini_repo(tmp_path)
    wd = _make_work_dir(tmp_path)
    r = _run_rerun(tmp_path, wd, repo, sha, patch_dir)
    assert r.returncode == 0, f"run must pass:\n{r.stdout}\n{r.stderr}"
    live = tmp_path / "live" / "lmcache-build"
    assert not live.exists() or not any(live.iterdir()), (
        f"the rerun must not write to live/lmcache-build: "
        f"{list(live.iterdir()) if live.exists() else 'absent'}")


def test_shared_gate_module_used_everywhere():
    """(5) the shared module kvpersist.gate_common is used by BOTH
    _incontainer_build.sh and _rerun_incontainer.sh -- no copied gate
    logic."""
    incontainer = INCONTAINER.read_text()
    rerun_incontainer = RERUN_INCONTAINER.read_text()
    assert "kvpersist.gate_common" in incontainer, (
        "_incontainer_build.sh must use the shared gate_common module")
    assert "kvpersist.gate_common" in rerun_incontainer, (
        "_rerun_incontainer.sh must use the shared gate_common module")
    assert "check-imports" in rerun_incontainer, (
        "_rerun_incontainer.sh must run the C8 import-check via gate_common")
    assert "junit-counts" in rerun_incontainer, (
        "_rerun_incontainer.sh must run the C12 junit counting via gate_common")