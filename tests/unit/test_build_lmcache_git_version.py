# SPDX-License-Identifier: Apache-2.0
"""r3 P0-2: build-lmcache.sh must not fall over git 'dubious ownership' inside
the container, and must pin the wheel version with a +kvp.<hash> local part.

Inside the pinned vLLM image the build runs as root while /usr/src/lmcache is
mounted from the host and owned by a different uid -- `git` (invoked by
setuptools_scm during `pip wheel`) refuses with "detected dubious ownership".
The r2 build script had no `git config --global --add safe.directory` and no
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE`, so `pip wheel` either failed
("unable to detect version") or produced 0.0.0 / 0.1.dev... -- indistinguishable
from the upstream nightly once baked.

This test:
  * reproduces the dubious-ownership failure with a REAL git repo chowned to
    a foreign uid (skipped with a clear reason unless running as root),
  * asserts `git -c safe.directory=<dir>` is the fix that makes it pass,
  * asserts build-lmcache.sh actually wires those two fixes (safe.directory
    line and SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE carrying +kvp.<hash>).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-lmcache.sh"
FOREIGN_UID = 12345


def _git(*args, cwd=None, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    # keep writes out of the caller's real globals: point GIT_CONFIG_GLOBAL
    # at a throwaway file so `git config --global` stays test-local
    env.setdefault("GIT_CONFIG_GLOBAL", os.devnull)
    return subprocess.run(
        ["git", *[a for a in args]],
        capture_output=True, text=True, timeout=30, cwd=str(cwd) if cwd else None, env=env,
    )


# NOTE: In the Android-side sandbox chown silently no-ops on tmpfs even as root
# (cwd bytes stay uid=0 after chown), so guarding merely on geteuid()==0 is not
# enough -- the test verifies its OWN setup once and skips itself with an honest
# reason if the environment refuses real ownership changes. On a real
# container/host (ps. SUSPENDED-P4D-fix:: testing framework) chown works and the
# bug here reproduces EXACTLY the failure build-lmcache.sh used to hit.
def _can_chown(tmp_check: pathlib.Path) -> bool:
    probe = tmp_check / "probe-file"
    probe.touch()
    try:
        os.chown(probe, FOREIGN_UID, FOREIGN_UID)
    except OSError:
        return False
    return os.stat(probe).st_uid == FOREIGN_UID


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason=(
        "dubious-ownership reproduction needs root (to chown the fixture repo to "
        "a foreign uid); this environment is not root"
    ),
)
def test_git_dubious_ownership_and_safe_directory_fix(tmp_path):
    """Reproduce the exact failure the build hits inside the container."""
    if not _can_chown(tmp_path):
        pytest.skip(
            "SKIP (honest reason): this test environment refuses real chown "
            "ownership -- chown(12345) on tmpfs silently no-ops here, so the "
            "root-vs-foreign-uid ownership mismatch repo condition is "
            "un-reproducible on this sandbox. The shell fix "
            "(`git config --global --add safe.directory /usr/src/lmcache`) is "
            "still asserted by test_build_lmcache_wires_safe_directory_and_pretend_version."
        )
    repo = tmp_path / "lmcache-src"
    repo.mkdir()
    assert _git("init", "-q", str(repo)).returncode == 0
    (repo / "file.txt").write_text("x\n")
    assert _git("-C", str(repo), "add", "file.txt").returncode == 0
    assert _git(
        "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "init",
    ).returncode == 0

    # simulate the mounted-from-host ownership mismatch: chown to somebody else
    for p in (repo, *repo.rglob("*")):
        os.chown(p, FOREIGN_UID, FOREIGN_UID)

    # without safe.directory this must FAIL (the bug)
    bad = _git("-C", str(repo), "describe", "--tags", "--always", "--dirty")
    assert bad.returncode != 0, (
        f"expected dubious-ownership failure when owned by uid {FOREIGN_UID} "
        f"and run as root, got rc=0: {bad.stdout}{bad.stderr}"
    )
    assert "dubious ownership" in (bad.stdout + bad.stderr).lower(), (
        f"expected dubious-ownership error, got: {bad.stderr[:300]}"
    )

    # with safe.directory this must PASS (the fix the script must wire up)
    good = _git(
        "-c", f"safe.directory={repo}",
        "-C", str(repo), "describe", "--tags", "--always", "--dirty",
    )
    assert good.returncode == 0, (
        f"git -c safe.directory must unblock the chowned repo: {good.stderr[:300]}"
    )


def _assert_wiring(src: str) -> None:
    """Single source of truth for the wiring contract: raises AssertionError
    describing the missing fix. Used against the clean script AND the mutants."""
    assert "safe.directory" in src and "/usr/src/lmcache" in src, (
        "in-container phase must run `git config --global --add safe.directory "
        "/usr/src/lmcache` before pip wheel"
    )
    # the fix must appear BEFORE the actual pip wheel command runs, not only in
    # a comment anywhere -- search for the real command line, not prose.
    assertion_line = src.find("git config --global --add safe.directory")
    build_line = src.find("python3 -m pip wheel")
    assert 0 < assertion_line < build_line or build_line == -1, (
        "safe.directory must be set before `python3 -m pip wheel` runs "
        f"(got safe.directory@{assertion_line}, wheel command@{build_line})"
    )
    m = re.search(r"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=([^\s\"']+)", src)
    assert m, "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE must be pinned"
    value = m.group(1)
    assert "+kvp." in value, (
        f"version must carry a '+kvp.<hash>' local part to distinguish our wheel "
        f"from the upstream nightly, got: {value!r}"
    )
    assert "PATCHED_STATE:0:12" in value, (
        f"version's +kvp part must shorten PATCHED_STATE to 12 hex, got {value!r}"
    )


def test_build_lmcache_wires_safe_directory_and_pretend_version():
    """The build script must actually carry both fixes -- a grep of the SKIPPED
    case is not a proof (r3 rule: the docker-run story is exercised by the
    dubious-ownership butterfly above, wiring is asserted here)."""
    _assert_wiring(SCRIPT.read_text())


def test_pretend_version_reaches_wheel_identity():
    """check_wheel_native / lmc_identity must compare the pinned version
    (not just the .so presence) so a wheel built under a different SCM version
    is refused before it can masquerade as our build (r3 P0-2)."""
    import importlib.util
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "implementation"))
    from kvpersist.lmc_identity import compare_wheel_version

    # golden: matching version passes
    ok, msg = compare_wheel_version(
        {"wheel_version": "0.5.7.dev428+kvp.0123456789ab"},
        "0.5.7.dev428+kvp.0123456789ab",
    )
    assert ok, f"identical versions must match: {msg}"

    # mismatched local part is a refusal
    ok, msg = compare_wheel_version(
        {"wheel_version": "0.5.7.dev428+kvp.0123456789ab"},
        "0.5.7.dev428+kvp.fedcba987654",
    )
    assert not ok and "wheel_version" in msg, (
        f"a drifted +kvp hash must be refused, got ok={ok}, msg={msg}"
    )

    # a bare upstream nightly version (no local part) must NOT match our pin
    ok, msg = compare_wheel_version(
        {"wheel_version": "0.5.7.dev428+kvp.0123456789ab"},
        "0.5.7.dev428",
    )
    assert not ok, "plain upstream nightly version must not satisfy our pinned build"


def test_mutation_no_safe_dir_is_red():
    """Mutation detector: stripping the safe.directory line must make the SAME
    wiring check fail (proves the check is not vacuous)."""
    mutant = re.sub(r"^.*safe\.directory.*$\n", "", SCRIPT.read_text(), flags=re.M)
    assert "safe.directory" not in mutant, (
        "mutation did not strip the safe.directory line -- test is not meaningful"
    )
    with pytest.raises(AssertionError):
        _assert_wiring(mutant)


def test_mutation_no_pretend_version_is_red():
    """Mutation detector: stripping the version pin must make the same check fail."""
    mutant = re.sub(
        r"^.*SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=.*$\n",
        "", SCRIPT.read_text(), flags=re.M,
    )
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=" not in mutant, (
        "mutation did not strip the version-pin line -- test is not meaningful"
    )
    with pytest.raises(AssertionError):
        _assert_wiring(mutant)