# SPDX-License-Identifier: Apache-2.0
"""r18 G3.1: provenance.json must not contain duplicated keys.

The heredoc in build-lmcache.sh used to write several keys twice
(torch_version, torch_cuda, python_version, cuda_available, gpu_count,
gpu_compute_capability, tests_skipped). `json.loads` silently takes the LAST
occurrence, but another parser (jq, a human) may take the FIRST -- the file
must be unambiguous.

Test: run the REAL build-lmcache.sh (docker stub, local mini repo) and read
the resulting provenance.json with `object_pairs_hook` that FAILS on a
duplicate key (the standard way to make the strictness explicit).
Mutation: restoring the duplicated block -> this test RED.
"""
from __future__ import annotations
import pathlib
import subprocess

# reuse the r18 G1 harness (real build-lmcache.sh + docker stub + mini repo)
import test_r18_g1_stale_provenance as g1

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _no_dupes(pairs):
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key in provenance.json: {k!r}")
        seen.add(k)
    return dict(pairs)


def test_provenance_json_has_no_duplicate_keys(tmp_path):
    import json
    _repo, _sha, _patches, out_dir, _build_dir, env = g1._setup(tmp_path, docker_rc=0)
    rb = g1._run_build(env, tmp_path)
    assert rb.returncode == 0, (
        f"the build must pass:\nstdout:\n{rb.stdout[-1200:]}\nstderr:\n{rb.stderr[-1200:]}")
    prov = out_dir / "provenance.json"
    assert prov.is_file(), "a successful build must write provenance.json"
    # strict read: object_pairs_hook raises on ANY duplicate key -- this is
    # what a strict parser (jq, a reviewer) would trip over
    data = json.loads(prov.read_text(), object_pairs_hook=_no_dupes)
    # the deduplicated keys must be present exactly once and carry the
    # values the build recorded
    for key in ("torch_version", "torch_cuda", "python_version",
                "cuda_available", "gpu_count", "gpu_compute_capability",
                "tests_skipped"):
        assert key in data, f"{key} missing from provenance.json"
    assert data["tests_skipped"] == "0", data["tests_skipped"]


# ---------------------------------------------------------------------------
# G3.2: prod_hash and filenames with spaces / non-ASCII
# ---------------------------------------------------------------------------
def _hash_of(src: pathlib.Path) -> str:
    """The SHARED prod-hash algorithm on a (staged) tree."""
    r = subprocess.run(
        ["python3", "-m", "kvpersist.gate_common", "prod-hash", str(src)],
        capture_output=True, text=True, check=True,
        env={**__import__("os").environ,
             "PYTHONPATH": str(ROOT / "implementation")})
    return r.stdout.strip()


GIT_ENV = dict(__import__("os").environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=__import__("os").devnull)


def _mini_prod_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tiny git tree with one production file (lmcache/prod.py)."""
    repo = tmp_path / "hash-repo"
    (repo / "lmcache").mkdir(parents=True)
    (repo / "lmcache" / "prod.py").write_text("VALUE = 1\n")
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pin"]):
        r = subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True,
                           text=True, env=GIT_ENV)
        assert r.returncode == 0, r.stderr
    return repo


def _stage(repo: pathlib.Path) -> None:
    r = subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True,
                       text=True, env=GIT_ENV)
    assert r.returncode == 0, r.stderr


def test_prod_hash_sensitive_to_filename_with_space(tmp_path):
    """(a) a file named `a b.py` (space) in a non-tests dir must influence
    the hash: changing its content must change the hash (r18 G3.2 -- the old
    `.split()` broke the name into bogus paths)."""
    repo = _mini_prod_tree(tmp_path)
    spaced = repo / "lmcache" / "a b.py"
    spaced.write_text("X = 1\n")
    _stage(repo)
    h1 = _hash_of(repo)
    spaced.write_text("X = 2\n")  # content change -> hash must change
    _stage(repo)
    h2 = _hash_of(repo)
    assert h1 != h2, (
        "a content change in 'a b.py' must change prod-hash (space filename)")
    # and the file itself is really covered: removing it changes the hash too
    spaced.unlink()
    _stage(repo)
    h3 = _hash_of(repo)
    assert h3 != h2, "removing 'a b.py' must change prod-hash"


def test_prod_hash_sensitive_to_non_ascii_filename(tmp_path):
    """(b) a file named `тест.py` (non-ASCII, git-quoted) must influence the
    hash the same way."""
    repo = _mini_prod_tree(tmp_path)
    cyr = repo / "lmcache" / "тест.py"
    cyr.write_text("Y = 1\n")
    _stage(repo)
    h1 = _hash_of(repo)
    cyr.write_text("Y = 2\n")
    _stage(repo)
    h2 = _hash_of(repo)
    assert h1 != h2, (
        "a content change in 'тест.py' must change prod-hash (non-ASCII filename)")


# ---------------------------------------------------------------------------
# G3.3: the gate rerun must return workspace ownership to the host user
# ---------------------------------------------------------------------------
def test_rerun_passes_host_ids_and_chowns_back():
    """(c) _rerun_incontainer.sh runs as root in the container and used to
    leave /tmp/kvp-lmc-build.* root-owned. It must (1) receive
    KVP_HOST_UID/KVP_HOST_GID from rerun-lmcache-gate.sh and (2) chown the
    work dirs back via a trap on EXIT (so it runs even when the gate FAILS)."""
    rerun_sh = (ROOT / "scripts" / "rerun-lmcache-gate.sh").read_text()
    incontainer = (ROOT / "scripts" / "_rerun_incontainer.sh").read_text()
    # (1) the host ids reach the container
    assert "-e KVP_HOST_UID=" in rerun_sh, (
        "rerun-lmcache-gate.sh must pass KVP_HOST_UID to the container")
    assert "-e KVP_HOST_GID=" in rerun_sh, (
        "rerun-lmcache-gate.sh must pass KVP_HOST_GID to the container")
    assert "$(id -u)" in rerun_sh and "$(id -g)" in rerun_sh, (
        "the host ids must come from the real host user (id -u / id -g)")
    # (2) chown back on EXIT inside the container script
    assert "trap" in incontainer and "EXIT" in incontainer, (
        "_rerun_incontainer.sh needs a trap ... EXIT for the chown-back")
    assert "chown -R" in incontainer, (
        "_rerun_incontainer.sh must chown the work dirs back")
    assert "KVP_HOST_UID" in incontainer and "KVP_HOST_GID" in incontainer, (
        "_rerun_incontainer.sh must use KVP_HOST_UID/KVP_HOST_GID for chown")
    # (3) the chown is fail-soft (must not mask a gate failure exit code) and
    # runs even when die() was called -- i.e. it is wired via trap, not only
    # at the happy-path end
    trap_line = next(ln for ln in incontainer.splitlines()
                     if "trap" in ln and "EXIT" in ln
                     and not ln.lstrip().startswith("#"))
    assert "chown" in trap_line or "_kvp_chown" in trap_line, (
        f"the EXIT trap must invoke the chown-back: {trap_line!r}")


def _run_chown_function(tmp_path: pathlib.Path, chown_rc: int):
    """Extract the REAL _kvp_chown_back function from _rerun_incontainer.sh
    and run it with a stub `chown` in PATH that exits with chown_rc."""
    src = (ROOT / "scripts" / "_rerun_incontainer.sh").read_text()
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("_kvp_chown_back()"))
    end = next(i for i, ln in enumerate(lines) if i > start and ln.strip() == "}")
    block = "\n".join(lines[start:end + 1])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "chown").write_text(
        f"#!/usr/bin/env bash\nexit {chown_rc}\n")
    (bin_dir / "chown").chmod(0o755)
    driver = tmp_path / "chown-driver.sh"
    driver.write_text("".join([
        "set -u\n",
        f'PATH="{bin_dir}:/usr/bin:/bin"\n',
        'KVP_HOST_UID="1000"\nKVP_HOST_GID="1000"\n',
        f'LMC_SRC="{tmp_path / "src"}"\n',
        f'KVP_WORK="{tmp_path / "work"}"\n',
        "mkdir -p \"$LMC_SRC\" \"$KVP_WORK\"\n",
        block + "\n",
        "_kvp_chown_back\n",   # the trap invokes it on EXIT -- call it here
        "echo DRIVER-DONE\n",
    ]))
    return subprocess.run(["bash", str(driver)], capture_output=True,
                          text=True, timeout=30, cwd=str(tmp_path))


def test_chown_failure_prints_warning_not_returned(tmp_path):
    """r19 H4.2: when chown fails (rc != 0) the script must print the
    WARNING 'could not return ownership' and must NOT claim 'ownership
    returned'; the exit code of the script is unchanged (the trap keeps it).
    With a WORKING chown the opposite. (Mutation: unconditional echo ->
    RED.)"""
    r = _run_chown_function(tmp_path, chown_rc=1)
    out = r.stdout + r.stderr
    assert "could not return ownership" in out, (
        f"a failing chown must warn loudly:\n{out}")
    assert "ownership returned" not in out, (
        f"the script must NOT claim success on a failed chown:\n{out}")
    assert "DRIVER-DONE" in r.stdout, (
        "the exit code / flow of the script must be unchanged")
    assert r.returncode == 0, (
        f"the trap must not change the exit code (rc={r.returncode}):\n{out}")

    r2 = _run_chown_function(tmp_path, chown_rc=0)
    out2 = r2.stdout + r2.stderr
    assert "ownership returned to 1000:1000" in out2, (
        f"a working chown must report the return:\n{out2}")
    assert "could not return ownership" not in out2, out2